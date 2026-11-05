"""Switch the monitor input and the keyboard/mouse channel to a target computer.

Order matters:
1. Preflight: find the monitor and devices; abort before changing anything if
   the monitor is missing.
2. Wait until modifiers and mouse buttons are released, so the old computer
   doesn't keep a stuck Ctrl/Alt after the keyboard leaves.
3. Mouse, then keyboard (HID++), while they're still reachable. A receiver in
   the monitor's USB hub follows the monitor input, so it must go before DDC.
4. Monitor input (DDC/CI) last; it needs no USB.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field

from monitor_switcher import ddc, devices, keys
from monitor_switcher.config import Config, Target

log = logging.getLogger(__name__)

DDC_RETRY_DELAY = 0.5


class SwitchError(Exception):
    """Nothing was changed because a precondition failed."""


@dataclass
class Step:
    action: str
    status: str  # "done", "skipped", "planned", "failed", "warning"
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.status:>7}] {self.action}" + (f" - {self.detail}" if self.detail else "")


@dataclass
class SwitchResult:
    target: Target
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)


def relative_target(config: Config, current_input: int, offset: int) -> Target:
    """The target `offset` places after the one currently on screen (wraps around)."""
    targets = config.targets
    for i, t in enumerate(targets):
        if t.monitor_input == current_input:
            return targets[(i + offset) % len(targets)]
    raise SwitchError(f"monitor input {current_input:#04x} is not in the config")


class Switcher:
    def __init__(
        self,
        config: Config,
        open_monitors: Callable[[], AbstractContextManager[list]] = ddc.open_monitors,
        open_devices: Callable[[], AbstractContextManager[list]] = devices.open_devices,
        wait_for_release: Callable[..., bool] = keys.wait_for_release,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._open_monitors = open_monitors
        self._open_devices = open_devices
        self._wait_for_release = wait_for_release
        self._sleep = sleep

    def current_input(self) -> int:
        with self._open_monitors() as monitors:
            return ddc.select_monitor(monitors, self.config.monitor_id).get_input()

    def switch(self, target: Target, dry_run: bool = False, hotkey_vk: int | None = None):
        result = SwitchResult(target)
        with ExitStack() as stack:
            monitors = stack.enter_context(self._open_monitors())
            try:
                monitor = ddc.select_monitor(monitors, self.config.monitor_id)
            except ddc.DdcError as e:
                raise SwitchError(str(e)) from None

            try:
                found = stack.enter_context(self._open_devices())
            except (ImportError, OSError) as e:
                result.steps.append(Step("find keyboard/mouse", "failed", str(e)))
                found = None

            moves = self._plan_devices(target, found, result) if found is not None else []
            switch_monitor = self._plan_monitor(target, monitor, result)

            if dry_run:
                return result

            if moves:
                extra = (hotkey_vk,) if hotkey_vk else ()
                if not self._wait_for_release(extra=extra):
                    result.steps.append(
                        Step("wait for key release", "warning", "keys still held; continuing")
                    )
            for device, channel, step in moves:
                try:
                    device.set_host(channel - 1)
                    step.status = "done"
                except OSError as e:
                    step.status, step.detail = "failed", str(e)
            if switch_monitor:
                self._set_input(monitor, target.monitor_input, switch_monitor)
        return result

    def _plan_devices(self, target: Target, found: list, result: SwitchResult):
        moves = []
        wanted = (
            ("mouse", target.mouse, self.config.mouse_filter),
            ("keyboard", target.keyboard, self.config.keyboard_filter),
        )
        for role, channel, name_filter in wanted:
            if channel is None:
                continue
            action = f"{role} -> channel {channel}"
            try:
                device = devices.pick(found, role, name_filter)
            except LookupError as e:
                result.steps.append(Step(action, "failed", str(e)))
                continue
            action = f"{role} {device.name} -> channel {channel}"
            if channel > device.hosts:
                result.steps.append(
                    Step(action, "failed", f"device only has {device.hosts} channels")
                )
            elif device.current == channel - 1:
                result.steps.append(Step(action, "skipped", "already on that channel"))
            else:
                step = Step(action, "planned", f"via {device.link}")
                result.steps.append(step)
                moves.append((device, channel, step))
        return moves

    def _plan_monitor(self, target: Target, monitor, result: SwitchResult) -> Step | None:
        action = f"monitor input -> {target.monitor_input:#04x}"
        try:
            current = monitor.get_input()
        except ddc.DdcError as e:
            log.debug("could not read current input: %s", e)
            current = None
        if current == target.monitor_input:
            result.steps.append(Step(action, "skipped", "already on that input"))
            return None
        step = Step(action, "planned", f"from {current:#04x}" if current is not None else "")
        result.steps.append(step)
        return step

    def _set_input(self, monitor, value: int, step: Step) -> None:
        for attempt in (1, 2):
            try:
                monitor.set_input(value)
                step.status = "done"
                return
            except ddc.DdcError as e:
                log.warning("DDC write attempt %d failed: %s", attempt, e)
                step.status, step.detail = "failed", str(e)
                if attempt == 1:
                    self._sleep(DDC_RETRY_DELAY)
