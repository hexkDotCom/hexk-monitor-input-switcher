"""Background hotkey listener.

Uses thread hotkeys (RegisterHotKey with no window), not a keyboard hook, so
there is nothing keylogger-like for security software to flag. One instance
per user session is enforced with a named mutex; `stop` signals a named event.

The switch runs right in the message loop when WM_HOTKEY arrives. That is at
key-down, while the keyboard's radio link is still active, which matters:
the K375s drops its link about a second after the last keystroke.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from monitor_switcher import ddc
from monitor_switcher.config import Config, Hotkey
from monitor_switcher.switcher import Switcher, SwitchError, relative_target

log = logging.getLogger(__name__)

EXIT_ALREADY_RUNNING = 3  # not an error: a second launch just leaves the first one running

MUTEX_NAME = "Local\\MonitorSwitcher.Instance"
STOP_EVENT_NAME = "Local\\MonitorSwitcher.Stop"

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
WAIT_OBJECT_0 = 0x0000
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
MB_ICONERROR = 0x0010
POLL_MS = 250  # wake up regularly so Ctrl+C works in a console


@dataclass(frozen=True)
class Binding:
    hotkey: Hotkey
    action: str  # target name, or "next" / "prev"


def bindings_from_config(config: Config) -> list[Binding]:
    bindings = [Binding(t.hotkey, t.name) for t in config.targets if t.hotkey]
    if config.next_hotkey:
        bindings.append(Binding(config.next_hotkey, "next"))
    if config.prev_hotkey:
        bindings.append(Binding(config.prev_hotkey, "prev"))
    return bindings


def handle(
    binding: Binding,
    config: Config,
    switcher: Switcher,
    beep: Callable[[], None] = lambda: None,
) -> bool:
    """Run one hotkey action; log the outcome and beep on failure."""
    started = time.monotonic()
    try:
        if binding.action in ("next", "prev"):
            offset = 1 if binding.action == "next" else -1
            target = relative_target(config, switcher.current_input(), offset)
        else:
            target = config.target(binding.action)
        result = switcher.switch(target, hotkey_vk=binding.hotkey.vk)
    except (SwitchError, ddc.DdcError) as e:
        log.error("%s: %s (nothing was changed)", binding.hotkey.text, e)
        beep()
        return False
    elapsed = (time.monotonic() - started) * 1000
    log.info("%s -> %s (%.0f ms)", binding.hotkey.text, target.name, elapsed)
    for step in result.steps:
        log.log(logging.INFO if step.status != "failed" else logging.ERROR, "  %s", step)
    if not result.ok:
        beep()
    return result.ok


def run(
    config: Config,
    switcher: Switcher | None = None,
    mutex_name: str = MUTEX_NAME,
    stop_event_name: str = STOP_EVENT_NAME,
) -> int:
    """Listen for hotkeys until stopped. Returns a process exit code."""
    w = _Win32()
    switcher = switcher or Switcher(config)

    ctypes.set_last_error(0)
    mutex = w.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        w.kernel32.CloseHandle(mutex)
        log.info("monitor-switcher is already running; this copy exits")
        return EXIT_ALREADY_RUNNING
    stop_event = w.kernel32.CreateEventW(None, True, False, stop_event_name)
    w.kernel32.ResetEvent(stop_event)

    registered: dict[int, Binding] = {}
    try:
        for hotkey_id, binding in enumerate(bindings_from_config(config), start=1):
            hk = binding.hotkey
            if w.user32.RegisterHotKey(None, hotkey_id, hk.modifiers | MOD_NOREPEAT, hk.vk):
                registered[hotkey_id] = binding
                log.info("hotkey %s -> %s", hk.text, binding.action)
            else:
                log.error(
                    "hotkey %s is taken by another program (Windows error %d)",
                    hk.text,
                    ctypes.get_last_error(),
                )
        if not registered:
            log.error("no hotkey could be registered; exiting")
            return 1

        log.info("listening for hotkeys (pid %d)", os.getpid())
        msg = wintypes.MSG()
        handles = (wintypes.HANDLE * 1)(stop_event)
        while True:
            wait = w.user32.MsgWaitForMultipleObjects(1, handles, False, POLL_MS, QS_ALLINPUT)
            if wait == WAIT_OBJECT_0:
                log.info("stop requested")
                return 0
            while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY and msg.wParam in registered:
                    try:
                        handle(registered[msg.wParam], config, switcher, beep=w.beep)
                    except Exception:  # keep listening whatever one switch does
                        log.exception("switch failed unexpectedly")
                        w.beep()
                    _drop_queued_hotkeys(w, msg)
    finally:
        for hotkey_id in registered:
            w.user32.UnregisterHotKey(None, hotkey_id)
        w.kernel32.CloseHandle(stop_event)
        w.kernel32.CloseHandle(mutex)


def _drop_queued_hotkeys(w: _Win32, msg: wintypes.MSG) -> None:
    """Hotkeys pressed during a switch refer to a screen that's gone; ignore them."""
    while w.user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
        log.info("ignored a hotkey pressed while switching")


def stop(stop_event_name: str = STOP_EVENT_NAME) -> bool:
    """Ask a running instance to exit. Returns False if none is running."""
    w = _Win32()
    event = w.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, stop_event_name)
    if not event:
        return False
    try:
        return bool(w.kernel32.SetEvent(event))
    finally:
        w.kernel32.CloseHandle(event)


def is_running(mutex_name: str = MUTEX_NAME) -> bool:
    w = _Win32()
    mutex = w.kernel32.OpenMutexW(SYNCHRONIZE, False, mutex_name)
    if not mutex:
        return False
    w.kernel32.CloseHandle(mutex)
    return True


def log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "monitor-switcher" / "switcher.log"


def setup_logging(verbose: bool = False) -> Path:
    """Log to a rotating file, plus the console when there is one."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    ]
    if sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    sys.excepthook = lambda *exc: log.critical("unhandled exception", exc_info=exc)
    return path


def show_error(message: str) -> None:
    """Make a startup failure visible when running without a console (pythonw)."""
    if sys.stderr is None and sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "monitor-switcher", MB_ICONERROR)


class _Win32:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("the hotkey daemon only runs on Windows")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = ctypes.WinDLL("user32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenEventW.restype = wintypes.HANDLE
        k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenMutexW.restype = wintypes.HANDLE
        for name in ("SetEvent", "ResetEvent", "CloseHandle"):
            fn = getattr(k, name)
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = wintypes.BOOL
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.MsgWaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        u.MsgWaitForMultipleObjects.restype = wintypes.DWORD
        u.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        u.PeekMessageW.restype = wintypes.BOOL
        u.MessageBeep.argtypes = [wintypes.UINT]
        u.MessageBeep.restype = wintypes.BOOL
        self.kernel32, self.user32 = k, u

    def beep(self) -> None:
        self.user32.MessageBeep(MB_ICONERROR)
