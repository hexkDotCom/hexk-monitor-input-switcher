"""Start the hotkey listener at login, per user, without admin rights.

A shortcut in the user's Startup folder runs `pythonw.exe -I -m monitor_switcher
run`. The shortcut is written through the Windows shell's own IShellLink COM
object (the same thing Explorer uses), not by spawning PowerShell or writing
.vbs/.cmd scripts, which security tools treat as suspicious.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

SHORTCUT_NAME = "monitor-switcher.lnk"
DESCRIPTION = "Monitor and keyboard/mouse switcher (hotkey listener)"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def pythonw() -> Path:
    """The windowless interpreter next to the running one (venv-aware)."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def launch_args(config_path: Path) -> list[str]:
    """Arguments for the interpreter. -I: ignore PYTHON* variables and the
    current directory, so nothing next to the shortcut can be imported."""
    return ["-I", "-m", "monitor_switcher", "--config", str(config_path.resolve()), "run"]


def working_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "monitor-switcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


def startup_folder() -> Path:
    """The user's Startup folder (follows folder redirection)."""
    return _known_folder("{B97D20BB-F46A-4C97-BA10-5E3608430854}")  # FOLDERID_Startup


def shortcut_path() -> Path:
    return startup_folder() / SHORTCUT_NAME


def install(config_path: Path, folder: Path | None = None) -> Path:
    path = (folder or startup_folder()) / SHORTCUT_NAME
    _write_shortcut(
        path,
        target=pythonw(),
        arguments=subprocess.list2cmdline(launch_args(config_path)),
        workdir=working_directory(),
        description=DESCRIPTION,
    )
    return path


def uninstall(folder: Path | None = None) -> bool:
    path = (folder or startup_folder()) / SHORTCUT_NAME
    if not path.exists():
        return False
    path.unlink()
    return True


def start_detached(config_path: Path) -> None:
    """Launch the listener now, exactly as the shortcut would at login."""
    subprocess.Popen(
        [str(pythonw()), *launch_args(config_path)],
        cwd=working_directory(),
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


# --- Win32 / COM plumbing ---------------------------------------------------------------------


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850

# vtable slots
_QUERY_INTERFACE, _RELEASE = 0, 2
_SET_DESCRIPTION, _SET_WORKING_DIRECTORY, _SET_ARGUMENTS, _SET_PATH = 7, 9, 11, 20
_PERSIST_SAVE = 6


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("autostart is only implemented for Windows")


def _known_folder(folder_id: str) -> Path:
    _require_windows()
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    out = ctypes.c_wchar_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(_GUID.parse(folder_id)), 0, None, out)
    if hr != 0:
        raise OSError(f"SHGetKnownFolderPath failed (HRESULT {hr & 0xFFFFFFFF:#010x})")
    try:
        return Path(out.value)
    finally:
        ole32.CoTaskMemFree(out)


def _com_call(obj: ctypes.c_void_p, slot: int, *args: tuple[type, object]) -> None:
    """Call vtable method `slot` of a COM object; raise on a failed HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(t for t, _ in args))
    hr = prototype(vtable[slot])(obj, *(v for _, v in args))
    if hr < 0:
        raise OSError(f"COM call failed (HRESULT {hr & 0xFFFFFFFF:#010x})")


def _release(obj: ctypes.c_void_p) -> None:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)


def _write_shortcut(path: Path, target: Path, arguments: str, workdir: Path, description: str):
    _require_windows()
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long

    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    initialized = hr >= 0  # S_OK or S_FALSE; RPC_E_CHANGED_MODE means COM is already up
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitializeEx failed (HRESULT {hr & 0xFFFFFFFF:#010x})")
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_GUID.parse(CLSID_SHELL_LINK)),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(_GUID.parse(IID_ISHELL_LINK_W)),
            ctypes.byref(link),
        )
        if hr < 0:
            raise OSError(f"creating a shell link failed (HRESULT {hr & 0xFFFFFFFF:#010x})")
        wstr = ctypes.c_wchar_p
        _com_call(link, _SET_PATH, (wstr, str(target)))
        _com_call(link, _SET_ARGUMENTS, (wstr, arguments))
        _com_call(link, _SET_WORKING_DIRECTORY, (wstr, str(workdir)))
        _com_call(link, _SET_DESCRIPTION, (wstr, description))
        iid = _GUID.parse(IID_IPERSIST_FILE)
        _com_call(
            link,
            _QUERY_INTERFACE,
            (ctypes.POINTER(_GUID), ctypes.byref(iid)),
            (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(persist)),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _com_call(persist, _PERSIST_SAVE, (wstr, str(path)), (wintypes.BOOL, True))
    finally:
        if persist:
            _release(persist)
        if link:
            _release(link)
        if initialized:
            ole32.CoUninitialize()
