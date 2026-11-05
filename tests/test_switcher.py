from contextlib import contextmanager

import pytest

from monitor_switcher import ddc
from monitor_switcher.config import parse_config
from monitor_switcher.switcher import Switcher, SwitchError, relative_target

CONFIG = parse_config(
    {
        "monitor": {"id": "MSI3DD6"},
        "target": [
            {"name": "A", "monitor": "hdmi1", "keyboard": 1, "mouse": 1},
            {"name": "B", "monitor": "dp", "keyboard": 2, "mouse": 2},
            {"name": "C", "monitor": "usbc", "keyboard": 3, "mouse": 3},
            {"name": "D", "monitor": "hdmi2", "keyboard": 3, "mouse": 3},
        ],
    }
)


class Log(list):
    pass


class FakeMonitor:
    def __init__(self, log, current=0x0F, failures=0):
        self.pnp_id = "MSI3DD6"
        self.description = "Generic PnP Monitor"
        self.log = log
        self.current = current
        self.failures = failures

    def get_input(self):
        return self.current

    def set_input(self, value):
        if self.failures:
            self.failures -= 1
            self.log.append("ddc write failed")
            raise ddc.DdcError("write failed")
        self.log.append(f"monitor {value:#04x}")
        self.current = value


class FakeDevice:
    def __init__(self, log, name, role, current, hosts=3, link="receiver slot 1"):
        self.log, self.name, self.role = log, name, role
        self.current, self.hosts, self.link = current, hosts, link
        self.online = True

    def set_host(self, host):
        self.log.append(f"{self.role} {host + 1}")


def make_switcher(log, monitors, found, released=True):
    @contextmanager
    def open_monitors():
        yield monitors

    @contextmanager
    def open_devices():
        if isinstance(found, Exception):
            raise found
        yield found

    def wait_for_release(extra=()):
        log.append("wait")
        return released

    return Switcher(CONFIG, open_monitors, open_devices, wait_for_release, sleep=lambda s: None)


@pytest.fixture
def log():
    return Log()


def devices_on(log, channel):
    return [
        FakeDevice(log, "K375s", "keyboard", channel - 1),
        FakeDevice(log, "M720", "mouse", channel - 1, link="bluetooth"),
    ]


def test_order_is_release_mouse_keyboard_then_monitor(log):
    switcher = make_switcher(log, [FakeMonitor(log, current=0x0F)], devices_on(log, 2))
    result = switcher.switch(CONFIG.target("C"))
    assert log == ["wait", "mouse 3", "keyboard 3", "monitor 0x10"]
    assert result.ok
    assert [s.status for s in result.steps] == ["done", "done", "done"]


def test_shared_channel_only_switches_monitor(log):
    switcher = make_switcher(log, [FakeMonitor(log, current=0x10)], devices_on(log, 3))
    result = switcher.switch(CONFIG.target("D"))
    assert log == ["monitor 0x12"]
    assert [s.status for s in result.steps] == ["skipped", "skipped", "done"]


def test_dry_run_changes_nothing(log):
    switcher = make_switcher(log, [FakeMonitor(log)], devices_on(log, 2))
    result = switcher.switch(CONFIG.target("A"), dry_run=True)
    assert log == []
    assert [s.status for s in result.steps] == ["planned", "planned", "planned"]


def test_missing_monitor_aborts_before_touching_devices(log):
    monitor = FakeMonitor(log)
    monitor.pnp_id = "DEL4321"
    switcher = make_switcher(log, [monitor], devices_on(log, 2))
    with pytest.raises(SwitchError, match="not found"):
        switcher.switch(CONFIG.target("A"))
    assert log == []


def test_missing_device_still_switches_the_rest(log):
    found = [FakeDevice(log, "K375s", "keyboard", 1)]
    switcher = make_switcher(log, [FakeMonitor(log)], found)
    result = switcher.switch(CONFIG.target("A"))
    assert log == ["wait", "keyboard 1", "monitor 0x11"]
    assert not result.ok
    assert result.steps[0].status == "failed" and "no mouse" in result.steps[0].detail


def test_hid_unavailable_still_switches_monitor(log):
    switcher = make_switcher(log, [FakeMonitor(log)], ImportError("No module named 'hid'"))
    result = switcher.switch(CONFIG.target("A"))
    assert log == ["monitor 0x11"]
    assert [s.status for s in result.steps] == ["failed", "done"]


def test_channel_beyond_device_capacity_fails(log):
    found = [
        FakeDevice(log, "K375s", "keyboard", 1, hosts=2),
        FakeDevice(log, "M720", "mouse", 1, hosts=2),
    ]
    switcher = make_switcher(log, [FakeMonitor(log)], found)
    result = switcher.switch(CONFIG.target("C"))
    assert log == ["monitor 0x10"]
    assert [s.status for s in result.steps] == ["failed", "failed", "done"]


def test_ddc_write_is_retried_once(log):
    switcher = make_switcher(log, [FakeMonitor(log, current=0x11, failures=1)], devices_on(log, 1))
    result = switcher.switch(CONFIG.target("B"))
    assert log[-2:] == ["ddc write failed", "monitor 0x0f"]
    assert result.ok


def test_ddc_failure_after_retry_is_reported(log):
    switcher = make_switcher(log, [FakeMonitor(log, current=0x11, failures=2)], devices_on(log, 1))
    result = switcher.switch(CONFIG.target("B"))
    assert not result.ok
    assert result.steps[-1].status == "failed"


def test_keys_still_held_is_a_warning_not_a_failure(log):
    switcher = make_switcher(log, [FakeMonitor(log)], devices_on(log, 2), released=False)
    result = switcher.switch(CONFIG.target("A"))
    assert result.ok
    assert any(s.status == "warning" for s in result.steps)


def test_relative_target_wraps_around():
    assert relative_target(CONFIG, 0x12, 1).name == "A"
    assert relative_target(CONFIG, 0x11, -1).name == "D"
    assert relative_target(CONFIG, 0x0F, 1).name == "C"
    with pytest.raises(SwitchError):
        relative_target(CONFIG, 0x1B, 1)
