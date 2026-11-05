"""Wait for keys and mouse buttons to be released before moving the keyboard away."""

from __future__ import annotations

import ctypes
import sys
import time

MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)
# Shift/Ctrl/Alt (generic and left/right) and the Windows keys.
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
WATCHED_KEYS = MOUSE_BUTTONS + MODIFIER_KEYS


def any_pressed(extra: tuple[int, ...] = ()) -> bool:
    if sys.platform != "win32":
        return False
    state = ctypes.windll.user32.GetAsyncKeyState
    return any(state(vk) & 0x8000 for vk in (*WATCHED_KEYS, *extra))


def wait_for_release(timeout: float = 2.0, extra: tuple[int, ...] = ()) -> bool:
    """Block until nothing watched is pressed. Returns False if the timeout ran out."""
    deadline = time.monotonic() + timeout
    while any_pressed(extra):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True
