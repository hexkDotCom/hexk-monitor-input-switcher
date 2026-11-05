"""Find Logitech HID++ keyboards and mice reachable from this computer (via hidapi)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from monitor_switcher.hidpp import (
    FEATURE_CHANGE_HOST,
    HIDPP10_ERROR,
    KIND_KEYBOARD,
    POINTER_KINDS,
    SHORT_REPORT,
    FeatureMissing,
    HidppDevice,
    HidppError,
    HidppTimeout,
)

log = logging.getLogger(__name__)

LOGITECH_VID = 0x046D
RECEIVER_USAGE_PAGE = 0xFF00  # usage 1 = short reports, usage 2 = long reports
BLE_USAGE_PAGE = 0xFF43  # Bluetooth LE devices: long reports only
BLE_USAGE = 0x0202
RECEIVER_SLOTS = range(1, 7)
RECEIVER_INDEX = 0xFF  # also the device index of a direct Bluetooth link
DIRECT_INDEX = 0xFF

# Unifying receiver register 0xB5: pairing info (0x20 + slot) and name (0x40 + slot).
READ_LONG_REGISTER = 0x83
PAIRING_REGISTER = 0xB5
UNIFYING_KINDS = {1: "keyboard", 2: "mouse", 8: "mouse", 9: "mouse"}

OFFLINE_NOTE = "not responding: asleep, or switched to another computer"
WAKE_RETRY_DELAY = 0.3


@dataclass
class LogiDevice:
    name: str
    role: str  # "keyboard", "mouse" or "other"
    link: str  # "receiver slot N" or "bluetooth"
    hosts: int  # number of Easy-Switch channels (0 when offline)
    current: int  # current channel, 0-based (-1 when offline)
    hidpp: HidppDevice | None
    online: bool = True

    def set_host(self, host: int) -> None:
        if self.hidpp is None:
            raise OSError(f"{self.name} is {OFFLINE_NOTE}")
        self.hidpp.set_host(host)


class HidTransport:
    """The hidapi handles of one HID++ endpoint.

    Windows splits a receiver into separate HID collections for short and
    long reports. Each report must be written to its own collection, and
    replies can arrive on either, so both are read.
    """

    def __init__(self, long_handle, short_handle=None):
        self._long = long_handle
        self._short = short_handle
        self._reads = [h for h in (long_handle, short_handle) if h is not None]

    def write(self, report: bytes) -> None:
        handle = self._short if report[0] == SHORT_REPORT and self._short else self._long
        if handle.write(report) < 0:
            raise OSError("HID write failed")

    def read(self, timeout_ms: int) -> bytes | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for handle in self._reads:
                data = handle.read(64, 5)
                if data:
                    return bytes(data)
            if time.monotonic() >= deadline:
                return None


def read_receiver_register(transport, sub: int, timeout: float = 0.3) -> bytes | None:
    """Read HID++ 1.0 long register 0xB5/<sub> from a receiver; None on error or timeout."""
    request = bytes([SHORT_REPORT, RECEIVER_INDEX, READ_LONG_REGISTER, PAIRING_REGISTER, sub, 0, 0])
    transport.write(request)
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        r = transport.read(max(1, int(remaining * 1000)))
        if not r or len(r) < 7 or r[1] != RECEIVER_INDEX:
            continue
        if r[2:5] == bytes([READ_LONG_REGISTER, PAIRING_REGISTER, sub]):
            return bytes(r[4:])
        if r[2:5] == bytes([HIDPP10_ERROR, READ_LONG_REGISTER, PAIRING_REGISTER]):
            return None
    return None


def receiver_pairings(transport) -> dict[int, tuple[str, str]] | None:
    """Paired slots of a Unifying receiver as {slot: (name, role)}.

    Returns None when the receiver doesn't answer (e.g. a Bolt receiver),
    so the caller can fall back to probing every slot.
    """
    pairings: dict[int, tuple[str, str]] = {}
    answered = False
    for slot in RECEIVER_SLOTS:
        info = read_receiver_register(transport, 0x20 + slot - 1)
        if info is None:
            continue
        answered = True
        role = UNIFYING_KINDS.get(info[7] & 0x0F, "other")
        name_reg = read_receiver_register(transport, 0x40 + slot - 1)
        name = ""
        if name_reg:
            name = name_reg[2 : 2 + name_reg[1]].decode("utf-8", errors="replace")
        pairings[slot] = (name or f"device in slot {slot}", role)
    return pairings if answered else None


def _collection_key(path: bytes) -> str:
    """Path with the collection number removed, to pair Col01/Col02 of one interface."""
    text = path.decode(errors="replace").lower()
    return re.sub(r"&col\d+#([^#]*)&\d+#", r"#\1#", text)


def _endpoints(hid) -> list[tuple[str, bytes, bytes | None]]:
    """(kind, long-report path, short-report path or None) for every HID++ endpoint."""
    infos = hid.enumerate(LOGITECH_VID)
    short_paths = {
        _collection_key(d["path"]): d["path"]
        for d in infos
        if d["usage_page"] == RECEIVER_USAGE_PAGE and d["usage"] == 0x0001
    }
    found = []
    for d in infos:
        if d["usage_page"] == RECEIVER_USAGE_PAGE and d["usage"] == 0x0002:
            found.append(("receiver", d["path"], short_paths.get(_collection_key(d["path"]))))
        elif d["usage_page"] == BLE_USAGE_PAGE and d["usage"] == BLE_USAGE:
            found.append(("bluetooth", d["path"], None))
    return found


def _classify(kind: int) -> str:
    if kind == KIND_KEYBOARD:
        return "keyboard"
    if kind in POINTER_KINDS:
        return "mouse"
    return "other"


@contextmanager
def open_devices() -> Iterator[list[LogiDevice]]:
    """Yield every HID++ device with Easy-Switch (CHANGE_HOST) known to this computer.

    Devices paired to a receiver but not responding are included with
    online=False, so callers can say why a device can't be switched.
    """
    import hid  # imported lazily so the rest of the package works without hidapi

    devices: list[LogiDevice] = []
    with ExitStack() as stack:

        def open_path(path):
            handle = hid.device()
            handle.open_path(path)
            stack.callback(handle.close)
            return handle

        for kind, long_path, short_path in _endpoints(hid):
            try:
                long_handle = open_path(long_path)
                short_handle = open_path(short_path) if short_path else None
            except OSError as e:
                log.debug("cannot open %s: %s", long_path, e)
                continue
            transport = HidTransport(long_handle, short_handle)
            if kind == "bluetooth":
                device = _probe(transport, DIRECT_INDEX, "bluetooth")
                if device:
                    devices.append(device)
                continue
            pairings = receiver_pairings(transport) if short_handle else None
            for slot in sorted(pairings) if pairings is not None else RECEIVER_SLOTS:
                link = f"receiver slot {slot}"
                device = _probe(transport, slot, link)
                if device is None and pairings and slot in pairings:
                    # A sleeping device often answers once the first request woke it.
                    time.sleep(WAKE_RETRY_DELAY)
                    device = _probe(transport, slot, link)
                if device:
                    devices.append(device)
                elif pairings and slot in pairings:
                    name, role = pairings[slot]
                    devices.append(LogiDevice(name, role, link, 0, -1, None, online=False))
        yield devices


def _probe(transport: HidTransport, index: int, link: str) -> LogiDevice | None:
    hidpp = HidppDevice(transport, index)
    try:
        if hidpp.feature_index(FEATURE_CHANGE_HOST) is None:
            return None
        hosts, current = hidpp.host_info()
        name = hidpp.name()
        role = _classify(hidpp.kind())
    except (HidppError, HidppTimeout, FeatureMissing) as e:
        log.debug("%s: %s", link, e)
        return None
    return LogiDevice(name, role, link, hosts, current, hidpp)


def pick(devices: list[LogiDevice], role: str, name_filter: str | None) -> LogiDevice:
    """The one responding device for a role, optionally narrowed by a name substring."""
    matches = [d for d in devices if d.role == role]
    if name_filter:
        matches = [d for d in matches if name_filter.casefold() in d.name.casefold()]
    online = [d for d in matches if d.online]
    if len(online) == 1:
        return online[0]
    if len(online) > 1:
        names = ", ".join(d.name for d in online)
        raise LookupError(f"several {role}s found ({names}); set [devices] {role} in the config")
    if matches:
        d = matches[0]
        raise LookupError(f"{d.name} ({d.link}) is {OFFLINE_NOTE}")
    note = f" matching {name_filter!r}" if name_filter else ""
    raise LookupError(f"no {role} with Easy-Switch found{note}")
