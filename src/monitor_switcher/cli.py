"""Switch monitor input and Logitech keyboard/mouse channel between computers."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

from monitor_switcher import config as cfg
from monitor_switcher import ddc, devices
from monitor_switcher.hidpp import FeatureMissing, HidppError, HidppTimeout
from monitor_switcher.switcher import Switcher, SwitchError, relative_target

INPUT_LABELS = {0x0F: "dp", 0x10: "usbc", 0x11: "hdmi1", 0x12: "hdmi2"}


def _input_label(code: int) -> str:
    name = INPUT_LABELS.get(code)
    return f"{code:#04x} ({name})" if name else f"{code:#04x}"


def cmd_probe(args: argparse.Namespace) -> int:
    """Show what this computer sees; read-only."""
    print("Monitors (DDC/CI):")
    on_screen: tuple[str, int] | None = None
    with ddc.open_monitors() as monitors:
        if not monitors:
            print("  none found")
        for m in monitors:
            label = m.pnp_id or "?"
            try:
                current = m.get_input()
            except ddc.DdcError as e:
                print(f"  {label:<10} {m.device}  no DDC/CI answer ({e})")
                continue
            try:
                caps = ", ".join(_input_label(c) for c in ddc.supported_inputs(m.capabilities()))
            except ddc.DdcError:
                caps = "?"
            print(f"  {label:<10} {m.device}  current input {_input_label(current)}")
            print(f"  {'':<10} supported inputs: {caps}")
            on_screen = (m.pnp_id, current)

    print("\nKeyboards and mice (HID++ Easy-Switch):")
    channels: dict[str, int] = {}
    try:
        with devices.open_devices() as found:
            if not found:
                print("  none found")
            for d in found:
                if not d.online:
                    print(f"  {d.name} [{d.role}] via {d.link}: {devices.OFFLINE_NOTE}")
                    continue
                print(f"  {d.name} [{d.role}] via {d.link}: channel {d.current + 1} of {d.hosts}")
                channels.setdefault(d.role, d.current + 1)
                try:
                    for slot in d.hidpp.host_slots():
                        state = "paired" if slot.paired else "empty"
                        name = f" {slot.name!r}" if slot.name else ""
                        print(f"      channel {slot.index + 1}: {state}{name}")
                except (HidppError, HidppTimeout, FeatureMissing) as e:
                    print(f"      (host names unavailable: {e})")
    except (ImportError, OSError) as e:
        print(f"  cannot access HID devices: {e}")

    if on_screen:
        pnp_id, current = on_screen
        monitor_value = f'"{INPUT_LABELS[current]}"' if current in INPUT_LABELS else hex(current)
        print("\nThis computer as a config entry:\n")
        if pnp_id:
            print(f'[monitor]\nid = "{pnp_id}"\n')
        print("[[target]]")
        print(f'name = "{socket.gethostname()}"')
        print('hotkey = "ctrl+alt+shift+1"   # pick a free number')
        print(f"monitor = {monitor_value}")
        for role in ("keyboard", "mouse"):
            if role in channels:
                print(f"{role} = {channels[role]}")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    config = cfg.load_config(args.config)
    switcher = Switcher(config)
    try:
        if args.target.casefold() in ("next", "prev"):
            offset = 1 if args.target.casefold() == "next" else -1
            target = relative_target(config, switcher.current_input(), offset)
        else:
            target = config.target(args.target)
        result = switcher.switch(target, dry_run=args.dry_run)
    except (SwitchError, ddc.DdcError) as e:
        print(f"error: {e} (nothing was changed)", file=sys.stderr)
        return 1
    print(f"{'Plan for' if args.dry_run else 'Switch to'} {target.name}:")
    for step in result.steps:
        print(f"  {step}")
    return 0 if result.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    config = cfg.load_config(args.config)
    print(f"{args.config}: OK\n")
    print(f"  {'target':<16} {'hotkey':<22} {'monitor':<14} {'keyboard':<9} mouse")
    for t in config.targets:
        hotkey = t.hotkey.text if t.hotkey else "-"
        keyboard = t.keyboard if t.keyboard is not None else "-"
        mouse = t.mouse if t.mouse is not None else "-"
        line = f"{t.name:<16} {hotkey:<22} {_input_label(t.monitor_input):<14} {keyboard!s:<9}"
        print(f"  {line} {mouse}")
    for warning in cfg.warnings(config):
        print(f"warning: {warning}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from monitor_switcher import daemon

    path = daemon.setup_logging(args.verbose)
    log = logging.getLogger("monitor_switcher")
    log.info("starting; config %s, log %s", args.config, path)
    try:
        config = cfg.load_config(args.config)
    except cfg.ConfigError as e:
        log.error("config error: %s", e)
        daemon.show_error(f"Config error:\n{e}")
        return 2
    for warning in cfg.warnings(config):
        log.warning(warning)
    try:
        code = daemon.run(config)
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    if code == daemon.EXIT_ALREADY_RUNNING:
        return code  # quiet: e.g. the Startup shortcut ran while `start` had already run
    if code:
        daemon.show_error(f"monitor-switcher stopped; see {path}")
    return code


def cmd_stop(args: argparse.Namespace) -> int:
    from monitor_switcher import daemon

    if daemon.stop():
        print("stop requested")
        return 0
    print("monitor-switcher is not running")
    return 1


def cmd_install_autostart(args: argparse.Namespace) -> int:
    from monitor_switcher import autostart

    cfg.load_config(args.config)  # refuse to install with a broken config
    path = autostart.install(args.config)
    print(f"created {path}")
    print("the hotkey listener now starts at login; use `start` to run it right away")
    return 0


def cmd_uninstall_autostart(args: argparse.Namespace) -> int:
    from monitor_switcher import autostart

    if autostart.uninstall():
        print(f"removed {autostart.shortcut_path()}")
        print("a listener that is already running keeps running; use `stop` to end it")
        return 0
    print("autostart was not installed")
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    import time

    from monitor_switcher import autostart, daemon

    if daemon.is_running():
        print("monitor-switcher is already running")
        return 0
    cfg.load_config(args.config)  # report config errors here, not in a message box
    autostart.start_detached(args.config)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if daemon.is_running():
            print(f"started in the background; log: {daemon.log_path()}")
            return 0
        time.sleep(0.2)
    print(f"did not start; see {daemon.log_path()}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    from monitor_switcher import autostart, daemon

    shortcut = autostart.shortcut_path()
    print(f"listener:  {'running' if daemon.is_running() else 'not running'}")
    print(f"autostart: {'installed' if shortcut.exists() else 'not installed'} ({shortcut})")
    print(f"config:    {args.config}{'' if args.config.exists() else ' (missing)'}")
    print(f"log:       {daemon.log_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor-switcher", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=cfg.default_config_path(),
        help="config file (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="show monitors, devices and this computer's config entry")

    switch = sub.add_parser("switch", help="switch monitor, keyboard and mouse to a target")
    switch.add_argument("target", help='target name from the config, or "next" / "prev"')
    switch.add_argument("-n", "--dry-run", action="store_true", help="show the plan only")

    sub.add_parser("check", help="validate the config file")
    sub.add_parser("run", help="listen for hotkeys in the background (Ctrl+C or `stop` to end)")
    sub.add_parser("start", help="start the hotkey listener in the background (no window)")
    sub.add_parser("stop", help="stop the running hotkey listener")
    sub.add_parser("status", help="show whether the listener runs and autostart is set up")
    sub.add_parser("install-autostart", help="start the listener at login (Startup shortcut)")
    sub.add_parser("uninstall-autostart", help="remove the Startup shortcut")
    return parser


COMMANDS = {
    "probe": cmd_probe,
    "switch": cmd_switch,
    "check": cmd_check,
    "run": cmd_run,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "install-autostart": cmd_install_autostart,
    "uninstall-autostart": cmd_uninstall_autostart,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run" and sys.stderr is not None:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.WARNING,
            format="%(levelname)s %(name)s: %(message)s",
        )
    try:
        return COMMANDS[args.command](args)
    except cfg.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
