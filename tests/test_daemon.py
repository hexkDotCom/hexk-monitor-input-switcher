import ctypes
import sys
import threading
import time
import uuid

import pytest

from monitor_switcher import daemon
from monitor_switcher.config import parse_config
from monitor_switcher.switcher import Step, SwitchError, SwitchResult

CONFIG = parse_config(
    {
        "target": [
            {"name": "A", "monitor": "hdmi1", "keyboard": 1, "hotkey": "ctrl+alt+shift+1"},
            {"name": "B", "monitor": "dp", "keyboard": 2, "hotkey": "ctrl+alt+shift+2"},
            {"name": "C", "monitor": "usbc", "keyboard": 3},
        ],
        "hotkeys": {"next": "ctrl+alt+shift+right"},
    }
)


class FakeSwitcher:
    def __init__(self, current=0x11, fail=False, error=None):
        self.current, self.fail, self.error = current, fail, error
        self.calls = []

    def current_input(self):
        return self.current

    def switch(self, target, dry_run=False, hotkey_vk=None):
        if self.error:
            raise self.error
        self.calls.append((target.name, hotkey_vk))
        status = "failed" if self.fail else "done"
        return SwitchResult(target, [Step("monitor", status)])


def test_bindings_cover_target_hotkeys_and_next():
    bindings = daemon.bindings_from_config(CONFIG)
    assert [(b.hotkey.text, b.action) for b in bindings] == [
        ("ctrl+alt+shift+1", "A"),
        ("ctrl+alt+shift+2", "B"),
        ("ctrl+alt+shift+right", "next"),
    ]


def test_handle_switches_to_target_and_passes_hotkey_key():
    switcher, beeps = FakeSwitcher(), []
    binding = daemon.bindings_from_config(CONFIG)[1]
    assert daemon.handle(binding, CONFIG, switcher, beep=lambda: beeps.append(1))
    assert switcher.calls == [("B", ord("2"))]
    assert beeps == []


def test_handle_next_uses_current_input():
    switcher = FakeSwitcher(current=0x0F)  # on B
    daemon.handle(daemon.bindings_from_config(CONFIG)[2], CONFIG, switcher)
    assert switcher.calls[0][0] == "C"


def test_handle_beeps_on_failed_step_and_on_abort():
    beeps = []
    binding = daemon.bindings_from_config(CONFIG)[0]
    assert not daemon.handle(binding, CONFIG, FakeSwitcher(fail=True), lambda: beeps.append(1))
    aborted = FakeSwitcher(error=SwitchError("monitor not found"))
    assert not daemon.handle(binding, CONFIG, aborted, lambda: beeps.append(1))
    assert beeps == [1, 1]


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 message loop")

# A combination nobody uses, so the test doesn't collide with real hotkeys.
TEST_CONFIG = parse_config(
    {"target": [{"name": "A", "monitor": "hdmi1", "hotkey": "ctrl+alt+shift+win+f24"}]}
)


def start_daemon(switcher, names):
    state = {}

    def body():
        state["thread_id"] = threading.get_native_id()
        state["code"] = daemon.run(TEST_CONFIG, switcher, *names)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, state


def post_hotkey(thread_id, hotkey_id=1, timeout=3.0):
    post = ctypes.windll.user32.PostThreadMessageW
    post.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    deadline = time.monotonic() + timeout
    while not post(thread_id, daemon.WM_HOTKEY, hotkey_id, 0):  # queue not created yet
        assert time.monotonic() < deadline, "daemon thread has no message queue"
        time.sleep(0.02)


@windows_only
def test_run_handles_hotkey_and_stops_on_request():
    names = (f"Local\\msw-test-{uuid.uuid4()}", f"Local\\msw-test-{uuid.uuid4()}")
    switcher = FakeSwitcher()
    thread, state = start_daemon(switcher, names)
    deadline = time.monotonic() + 3
    while "thread_id" not in state:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    post_hotkey(state["thread_id"])
    while not switcher.calls:
        assert time.monotonic() < deadline, "hotkey was not handled"
        time.sleep(0.02)
    assert switcher.calls == [("A", 0x87)]
    assert daemon.is_running(names[0])

    assert daemon.stop(names[1])
    thread.join(3)
    assert not thread.is_alive()
    assert state["code"] == 0
    assert not daemon.stop(names[1])  # nothing left to stop
    assert not daemon.is_running(names[0])


@windows_only
def test_second_instance_exits():
    names = (f"Local\\msw-test-{uuid.uuid4()}", f"Local\\msw-test-{uuid.uuid4()}")
    first, _ = start_daemon(FakeSwitcher(), names)
    time.sleep(0.3)
    try:
        assert daemon.run(TEST_CONFIG, FakeSwitcher(), *names) == daemon.EXIT_ALREADY_RUNNING
    finally:
        daemon.stop(names[1])
        first.join(3)
    assert not first.is_alive()
