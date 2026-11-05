import sys

import pytest

from monitor_switcher import devices
from monitor_switcher.devices import (
    HidTransport,
    LogiDevice,
    _collection_key,
    pick,
    receiver_pairings,
)


class FakeReceiver:
    """Answers register 0xB5 reads like the Unifying receiver with a K375s paired in slot 1."""

    def __init__(self, paired=None):
        self.paired = paired if paired is not None else {1: (0x01, b"K370s/K375s")}
        self.pending = []

    def write(self, report):
        assert report[:4] == bytes([0x10, 0xFF, 0x83, 0xB5])
        sub = report[4]
        slot = (sub & 0x0F) + 1
        if slot not in self.paired:
            self.pending.append(bytes([0x10, 0xFF, 0x8F, 0x83, 0xB5, 0x03, 0x00]))
            return
        kind, name = self.paired[slot]
        if sub & 0xF0 == 0x20:
            params = bytes([sub, 0x0B, 0x14, 0x40, 0x61, 0x04, 0x03, kind, 0x07])
        else:
            params = bytes([sub, len(name)]) + name
        self.pending.append((bytes([0x11, 0xFF, 0x83, 0xB5]) + params).ljust(20, b"\0"))

    def read(self, timeout_ms):
        return self.pending.pop(0) if self.pending else None


def test_receiver_pairings_reads_names_and_kinds():
    pairings = receiver_pairings(FakeReceiver({1: (0x01, b"K370s/K375s"), 3: (0x02, b"M720")}))
    assert pairings == {1: ("K370s/K375s", "keyboard"), 3: ("M720", "mouse")}


def test_receiver_pairings_none_when_receiver_never_answers():
    class Silent:
        def write(self, report):
            pass

        def read(self, timeout_ms):
            return None

    assert receiver_pairings(FakeReceiver({})) is None
    assert receiver_pairings(Silent()) is None


def device(name, role, online=True):
    return LogiDevice(name, role, "receiver slot 1", 3 if online else 0, 0, None, online)


def test_pick_single_device():
    kb = device("K375s", "keyboard")
    assert pick([kb, device("M720", "mouse")], "keyboard", None) is kb


def test_pick_explains_offline_device():
    with pytest.raises(LookupError, match="K375s .* asleep, or switched"):
        pick([device("K375s", "keyboard", online=False)], "keyboard", None)


def test_pick_prefers_online_over_offline():
    online = device("K375s", "keyboard")
    assert pick([device("K780", "keyboard", online=False), online], "keyboard", None) is online


def test_pick_ambiguous_and_filter():
    k1, k2 = device("K375s", "keyboard"), device("MX Keys", "keyboard")
    with pytest.raises(LookupError, match="several keyboards"):
        pick([k1, k2], "keyboard", None)
    assert pick([k1, k2], "keyboard", "mx") is k2
    with pytest.raises(LookupError, match="matching 'k780'"):
        pick([k1, k2], "keyboard", "k780")


def test_offline_device_cannot_be_switched():
    with pytest.raises(OSError, match="not responding"):
        device("K375s", "keyboard", online=False).set_host(0)


class Handle:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)
        return len(data)

    def read(self, size, timeout_ms):
        return []


def test_transport_routes_reports_to_matching_collection():
    long_h, short_h = Handle(), Handle()
    transport = HidTransport(long_h, short_h)
    transport.write(bytes([0x10, 0xFF, 0x83, 0xB5, 0x20, 0, 0]))
    transport.write(bytes([0x11, 0x01]).ljust(20, b"\0"))
    assert [r[0] for r in short_h.written] == [0x10]
    assert [r[0] for r in long_h.written] == [0x11]
    assert transport.read(1) is None


def test_open_devices_retries_idle_bluetooth_device(monkeypatch):
    class FakeHid:
        class device:
            def open_path(self, path):
                pass

            def close(self):
                pass

        @staticmethod
        def enumerate(vid):
            return [{"path": b"bt-kb", "usage_page": 0xFF43, "usage": 0x0202}]

    kb = device("K375s", "keyboard")
    answers = [None, kb]  # asleep on the first request, awake on the second
    monkeypatch.setitem(sys.modules, "hid", FakeHid)
    monkeypatch.setattr(devices, "WAKE_RETRY_DELAY", 0)
    monkeypatch.setattr(devices, "_probe", lambda transport, index, link: answers.pop(0))
    with devices.open_devices() as found:
        assert found == [kb]


def test_collection_key_pairs_short_and_long_collections():
    long_path = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col02#8&1a2b3c4d&0&0001#{4d1e55b2}"
    short_path = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col01#8&1a2b3c4d&0&0000#{4d1e55b2}"
    other = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col01#8&5e6f7a8b&0&0000#{4d1e55b2}"
    assert _collection_key(long_path) == _collection_key(short_path)
    assert _collection_key(long_path) != _collection_key(other)
