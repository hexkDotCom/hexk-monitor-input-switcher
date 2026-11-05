"""Minimal HID++ 2.0 client: feature lookup, device name/type, and Easy-Switch host change."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

SHORT_REPORT = 0x10
LONG_REPORT = 0x11
SHORT_LEN = 7
LONG_LEN = 20
SOFTWARE_ID = 0x0D  # identifies our requests; Logi Options uses others

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_NAME = 0x0005
FEATURE_CHANGE_HOST = 0x1814
FEATURE_HOSTS_INFO = 0x1815

HIDPP10_ERROR = 0x8F
HIDPP20_ERROR = 0xFF

# DEVICE_NAME.getDeviceType values
KIND_KEYBOARD = 0
KIND_MOUSE = 3
KIND_TRACKPAD = 4
KIND_TRACKBALL = 5
POINTER_KINDS = {KIND_MOUSE, KIND_TRACKPAD, KIND_TRACKBALL}


class Transport(Protocol):
    def write(self, report: bytes) -> None: ...

    def read(self, timeout_ms: int) -> bytes | None:
        """Return one input report, or None when nothing arrived in time."""
        ...


class HidppError(Exception):
    """The device answered with an error code."""

    def __init__(self, code: int, protocol: int):
        super().__init__(f"HID++ {protocol}.0 error {code:#04x}")
        self.code = code
        self.protocol = protocol


class HidppTimeout(Exception):
    """The device did not answer (asleep, out of range, or slot unpaired)."""


class FeatureMissing(Exception):
    """The device does not implement a required HID++ feature."""


@dataclass(frozen=True)
class HostSlot:
    index: int  # 0-based
    paired: bool
    name: str


class HidppDevice:
    def __init__(self, transport: Transport, index: int, timeout: float = 0.5):
        self.transport = transport
        self.index = index  # receiver slot 1-6, or 0xFF for a direct (Bluetooth) link
        self.timeout = timeout
        self._features: dict[int, int | None] = {}

    def _frame(self, feature_index: int, function: int, params: bytes) -> bytes:
        if len(params) > LONG_LEN - 4:
            raise ValueError("too many parameters for a long report")
        header = bytes([LONG_REPORT, self.index, feature_index, (function << 4) | SOFTWARE_ID])
        return (header + params).ljust(LONG_LEN, b"\x00")

    def request(self, feature_index: int, function: int, params: bytes = b"") -> bytes:
        """Send a request and return the reply parameters (16 bytes)."""
        self.transport.write(self._frame(feature_index, function, params))
        func_sw = (function << 4) | SOFTWARE_ID
        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            report = self.transport.read(max(1, int(remaining * 1000)))
            if report and (reply := self._match(report, feature_index, func_sw)) is not None:
                return reply
        raise HidppTimeout(f"no answer from device {self.index:#04x}")

    def _match(self, report: bytes, feature_index: int, func_sw: int) -> bytes | None:
        """Reply parameters if `report` answers our request; raise on a matching error."""
        rid = report[0]
        if rid == LONG_REPORT and len(report) >= LONG_LEN and report[1] == self.index:
            if report[2] == feature_index and report[3] == func_sw:
                return bytes(report[4:LONG_LEN])
            if report[2:5] == bytes([HIDPP20_ERROR, feature_index, func_sw]):
                raise HidppError(report[5], 2)
        short_error = bytes([self.index, HIDPP10_ERROR, feature_index, func_sw])
        if rid == SHORT_REPORT and len(report) >= SHORT_LEN and report[1:5] == short_error:
            raise HidppError(report[5], 1)
        return None

    def send(self, feature_index: int, function: int, params: bytes = b"") -> None:
        """Send a request that gets no reply (the device leaves immediately)."""
        self.transport.write(self._frame(feature_index, function, params))

    def feature_index(self, feature_id: int) -> int | None:
        if feature_id not in self._features:
            reply = self.request(0x00, 0, feature_id.to_bytes(2, "big"))
            self._features[feature_id] = reply[0] or None
        return self._features[feature_id]

    def _require(self, feature_id: int) -> int:
        index = self.feature_index(feature_id)
        if index is None:
            raise FeatureMissing(f"device {self.index:#04x} lacks feature {feature_id:#06x}")
        return index

    def name(self) -> str:
        index = self._require(FEATURE_DEVICE_NAME)
        length = self.request(index, 0)[0]
        name = b""
        while len(name) < length:
            chunk = self.request(index, 1, bytes([len(name)]))
            piece = chunk[: length - len(name)].rstrip(b"\x00")
            if not piece:
                break
            name += piece
        return name.decode("utf-8", errors="replace")

    def kind(self) -> int:
        return self.request(self._require(FEATURE_DEVICE_NAME), 2)[0]

    def host_info(self) -> tuple[int, int]:
        """Return (number of channels, current channel 0-based)."""
        reply = self.request(self._require(FEATURE_CHANGE_HOST), 0)
        return reply[0], reply[1]

    def set_host(self, host: int) -> None:
        """Switch to channel `host` (0-based). The device disconnects; no reply."""
        self.send(self._require(FEATURE_CHANGE_HOST), 1, bytes([host]))

    def host_slots(self) -> list[HostSlot]:
        """Pairing state and host names per channel (HOSTS_INFO), if supported."""
        index = self.feature_index(FEATURE_HOSTS_INFO)
        if index is None:
            return []
        count = self.request(index, 0)[2]
        slots = []
        for host in range(count):
            info = self.request(index, 1, bytes([host]))
            paired, name_len = bool(info[1] & 0x01), info[4]
            name = b""
            while len(name) < name_len:
                chunk = self.request(index, 3, bytes([host, len(name)]))
                piece = chunk[2:][: name_len - len(name)]
                if not piece:
                    break
                name += piece
            slots.append(HostSlot(host, paired, name.decode("utf-8", errors="replace")))
        return slots
