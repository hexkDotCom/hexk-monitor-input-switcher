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


class FakeLink:
    """One HID endpoint as hidapi sees it: a Bluetooth K375s by default.

    `drop` requests go unanswered (an idle link); `dead` fails every write
    (a device paired to Windows but connected elsewhere).
    """

    def __init__(self, features=None, drop=0, dead=False):
        self.features = {0x0005: 0x03, 0x1814: 0x08} if features is None else features
        self.drop, self.dead = drop, dead
        self.requests: list[bytes] = []
        self.pending: list[bytes] = []

    def write(self, data):
        if self.dead:
            return -1
        data = bytes(data)
        self.requests.append(data)
        if self.drop:
            self.drop -= 1
            return len(data)
        feature_index, function = data[2], data[3] >> 4
        if feature_index == 0x00:
            params = bytes([self.features.get(int.from_bytes(data[4:6], "big"), 0)])
        elif (feature_index, function) == (0x03, 0):
            params = bytes([5])
        elif (feature_index, function) == (0x03, 1):
            params = b"K375s"[data[4] :]
        elif (feature_index, function) == (0x03, 2):
            params = bytes([0])  # keyboard
        elif (feature_index, function) == (0x08, 0):
            params = bytes([3, 0])  # 3 channels, on channel 1
        else:
            return len(data)
        self.pending.append((data[:4] + params).ljust(20, b"\0"))
        return len(data)

    def read(self, size, timeout_ms):
        return list(self.pending.pop(0)) if self.pending else []


def fake_hid(endpoints):
    """A stand-in for the `hid` module serving {path: (usage_page, usage, FakeLink)}."""

    class Handle:
        def open_path(self, path):
            self.link = endpoints[path][2]

        def write(self, data):
            return self.link.write(data)

        def read(self, size, timeout_ms):
            return self.link.read(size, timeout_ms)

        def close(self):
            pass

    class Hid:
        device = Handle

        @staticmethod
        def enumerate(vid):
            return [{"path": p, "usage_page": e[0], "usage": e[1]} for p, e in endpoints.items()]

    return Hid


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(devices, "WAKE_RETRY_DELAY", 0)
    monkeypatch.setattr(devices, "REQUEST_TIMEOUT", 0.05)


def discover(monkeypatch, endpoints):
    monkeypatch.setitem(sys.modules, "hid", fake_hid(endpoints))
    with devices.open_devices() as found:
        return found


def feature_lookups(link):
    """How often the CHANGE_HOST feature was looked up, i.e. probe attempts."""
    return sum(1 for r in link.requests if r[2] == 0x00 and r[4:6] == b"\x18\x14")


def test_idle_bluetooth_keyboard_is_found_on_the_second_try(monkeypatch, quick):
    link = FakeLink(drop=1)  # misses the first request, as the K375s does when idle
    found = discover(monkeypatch, {b"bt-kb": (0xFF43, 0x0202, link)})
    assert [(d.name, d.role, d.link, d.current) for d in found] == [
        ("K375s", "keyboard", "bluetooth", 0)
    ]
    assert feature_lookups(link) == 2


def test_bluetooth_device_without_easy_switch_is_not_retried(monkeypatch, quick):
    link = FakeLink(features={0x0005: 0x03})
    assert discover(monkeypatch, {b"bt-other": (0xFF43, 0x0202, link)}) == []
    assert feature_lookups(link) == 1


def test_unreachable_bluetooth_device_does_not_stop_discovery(monkeypatch, quick):
    found = discover(
        monkeypatch,
        {
            b"bt-gone": (0xFF43, 0x0202, FakeLink(dead=True)),
            b"bt-kb": (0xFF43, 0x0202, FakeLink()),
        },
    )
    assert [d.name for d in found] == ["K375s"]


def test_unreachable_receiver_does_not_stop_discovery(monkeypatch, quick):
    dead = FakeLink(dead=True)
    found = discover(
        monkeypatch,
        {
            rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col01#8&1a2b3c4d&0&0000#{x}": (0xFF00, 1, dead),
            rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col02#8&1a2b3c4d&0&0001#{x}": (0xFF00, 2, dead),
            b"bt-kb": (0xFF43, 0x0202, FakeLink()),
        },
    )
    assert [d.name for d in found] == ["K375s"]


def test_collection_key_pairs_short_and_long_collections():
    long_path = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col02#8&1a2b3c4d&0&0001#{4d1e55b2}"
    short_path = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col01#8&1a2b3c4d&0&0000#{4d1e55b2}"
    other = rb"\\?\HID#VID_046D&PID_C52B&MI_02&Col01#8&5e6f7a8b&0&0000#{4d1e55b2}"
    assert _collection_key(long_path) == _collection_key(short_path)
    assert _collection_key(long_path) != _collection_key(other)
