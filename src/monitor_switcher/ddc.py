"""DDC/CI monitor control through the Windows dxva2 API (no admin rights needed)."""

from __future__ import annotations

import ctypes
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass

VCP_INPUT_SOURCE = 0x60


class DdcError(Exception):
    """A DDC/CI call failed or no matching monitor was found."""


@dataclass
class Monitor:
    pnp_id: str  # e.g. "MSI3DD6"; empty when Windows didn't report one
    device: str  # e.g. r"\\.\DISPLAY1"
    description: str
    handle: int

    def get_vcp(self, code: int) -> tuple[int, int]:
        """Return (current, maximum) for a VCP code."""
        current, maximum = wintypes.DWORD(), wintypes.DWORD()
        if not _dxva2().GetVCPFeatureAndVCPFeatureReply(
            self.handle, code, None, ctypes.byref(current), ctypes.byref(maximum)
        ):
            raise DdcError(_with_error(f"reading VCP {code:#04x} failed"))
        return current.value, maximum.value

    def set_vcp(self, code: int, value: int) -> None:
        if not _dxva2().SetVCPFeature(self.handle, code, value):
            raise DdcError(_with_error(f"writing VCP {code:#04x}={value:#x} failed"))

    def get_input(self) -> int:
        return self.get_vcp(VCP_INPUT_SOURCE)[0]

    def set_input(self, value: int) -> None:
        self.set_vcp(VCP_INPUT_SOURCE, value)

    def capabilities(self) -> str:
        dll = _dxva2()
        length = wintypes.DWORD()
        if not dll.GetCapabilitiesStringLength(self.handle, ctypes.byref(length)):
            raise DdcError(_with_error("reading capabilities length failed"))
        buf = ctypes.create_string_buffer(length.value)
        if not dll.CapabilitiesRequestAndCapabilitiesReply(self.handle, buf, length):
            raise DdcError(_with_error("reading capabilities failed"))
        return buf.value.decode("ascii", errors="replace")


def supported_inputs(caps: str) -> list[int]:
    """Input codes listed for VCP 0x60 in an MCCS capabilities string."""
    vcp = re.search(r"vcp\((.*)\)", caps)
    if not vcp:
        return []
    match = re.search(r"(?:^|[\s)])60\(([0-9A-Fa-f ]*)\)", vcp.group(1))
    if not match:
        return []
    return [int(v, 16) for v in match.group(1).split()]


@contextmanager
def open_monitors() -> Iterator[list[Monitor]]:
    """Yield every physical monitor; handles are released on exit."""
    dll, user32 = _dxva2(), _user32()
    groups: list[tuple[int, ctypes.Array]] = []
    monitors: list[Monitor] = []
    try:
        for hmon in _enum_hmonitors():
            count = wintypes.DWORD()
            if not dll.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
                continue
            if count.value == 0:
                continue
            arr = (_PhysicalMonitor * count.value)()
            if not dll.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                continue
            groups.append((count.value, arr))
            device = _monitor_device_name(user32, hmon)
            ids = _pnp_ids(user32, device)
            for i, pm in enumerate(arr):
                pnp_id = ids[i] if len(ids) == count.value else ""
                monitors.append(
                    Monitor(pnp_id, device, pm.szPhysicalMonitorDescription, pm.hPhysicalMonitor)
                )
        yield monitors
    finally:
        for count, arr in groups:
            dll.DestroyPhysicalMonitors(count, arr)


def select_monitor(monitors: list[Monitor], pnp_id: str | None) -> Monitor:
    """Pick the configured monitor, or the only DDC-capable one."""
    if pnp_id:
        matches = [m for m in monitors if m.pnp_id.casefold() == pnp_id.casefold()]
    else:
        matches = [m for m in monitors if _answers_ddc(m)]
    if len(matches) == 1:
        return matches[0]
    seen = ", ".join(m.pnp_id or m.description for m in monitors) or "none"
    if not matches:
        wanted = f"monitor {pnp_id}" if pnp_id else "a DDC/CI-capable monitor"
        raise DdcError(f"{wanted} not found (monitors seen: {seen})")
    raise DdcError(f"several monitors match ({seen}); set [monitor] id in the config")


def _answers_ddc(monitor: Monitor) -> bool:
    try:
        monitor.get_input()
    except DdcError:
        return False
    return True


# --- Win32 plumbing ---------------------------------------------------------------------------


class _PhysicalMonitor(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


_DISPLAY_DEVICE_ACTIVE = 0x1
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


def _enum_hmonitors() -> list[int]:
    found: list[int] = []

    def callback(hmon, _hdc, _rect, _lparam):
        found.append(hmon)
        return True

    if not _user32().EnumDisplayMonitors(None, None, _MonitorEnumProc(callback), 0):
        raise DdcError(_with_error("EnumDisplayMonitors failed"))
    return found


def _monitor_device_name(user32, hmon: int) -> str:
    info = _MonitorInfoEx()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return ""
    return info.szDevice


def _pnp_ids(user32, adapter_device: str) -> list[str]:
    """PnP IDs of the active monitors on an adapter output, e.g. ["MSI3DD6"]."""
    ids = []
    i = 0
    while True:
        dd = _DisplayDevice()
        dd.cb = ctypes.sizeof(dd)
        if not user32.EnumDisplayDevicesW(adapter_device, i, ctypes.byref(dd), 0):
            break
        i += 1
        if dd.StateFlags & _DISPLAY_DEVICE_ACTIVE:
            parts = dd.DeviceID.split("\\")  # MONITOR\MSI3DD6\{guid}\0001
            ids.append(parts[1] if len(parts) > 1 else "")
    return ids


_libs: dict[str, ctypes.WinDLL] = {}


def _user32():
    if "user32" not in _libs:
        _require_windows()
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.EnumDisplayMonitors.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            _MonitorEnumProc,
            wintypes.LPARAM,
        ]
        lib.EnumDisplayMonitors.restype = wintypes.BOOL
        lib.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfoEx)]
        lib.GetMonitorInfoW.restype = wintypes.BOOL
        lib.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_DisplayDevice),
            wintypes.DWORD,
        ]
        lib.EnumDisplayDevicesW.restype = wintypes.BOOL
        _libs["user32"] = lib
    return _libs["user32"]


def _dxva2():
    if "dxva2" not in _libs:
        _require_windows()
        lib = ctypes.WinDLL("dxva2", use_last_error=True)
        dword_p = ctypes.POINTER(wintypes.DWORD)
        lib.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, dword_p]
        lib.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR,
            wintypes.DWORD,
            ctypes.POINTER(_PhysicalMonitor),
        ]
        lib.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(_PhysicalMonitor)]
        lib.GetVCPFeatureAndVCPFeatureReply.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ubyte,
            ctypes.c_void_p,
            dword_p,
            dword_p,
        ]
        lib.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
        lib.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, dword_p]
        lib.CapabilitiesRequestAndCapabilitiesReply.argtypes = [
            wintypes.HANDLE,
            ctypes.c_char_p,
            wintypes.DWORD,
        ]
        for fn in (
            lib.GetNumberOfPhysicalMonitorsFromHMONITOR,
            lib.GetPhysicalMonitorsFromHMONITOR,
            lib.DestroyPhysicalMonitors,
            lib.GetVCPFeatureAndVCPFeatureReply,
            lib.SetVCPFeature,
            lib.GetCapabilitiesStringLength,
            lib.CapabilitiesRequestAndCapabilitiesReply,
        ):
            fn.restype = wintypes.BOOL
        _libs["dxva2"] = lib
    return _libs["dxva2"]


def _with_error(message: str) -> str:
    code = ctypes.get_last_error()
    return f"{message} (Windows error {code:#x})" if code else message


def _require_windows() -> None:
    if sys.platform != "win32":
        raise DdcError("DDC/CI control is only implemented for Windows")
