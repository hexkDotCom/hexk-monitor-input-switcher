"""Configuration file loading and validation."""

from __future__ import annotations

import os
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# MCCS VCP 0x60 (input source) values. MSI exposes USB-C as "DP2" (0x10).
INPUT_NAMES: dict[str, int] = {
    "dp": 0x0F,
    "dp1": 0x0F,
    "dp2": 0x10,
    "usbc": 0x10,
    "hdmi1": 0x11,
    "hdmi2": 0x12,
}

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

_NAMED_KEYS = {
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "space": 0x20,
    "tab": 0x09,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
}


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


@dataclass(frozen=True)
class Hotkey:
    text: str
    modifiers: int
    vk: int


@dataclass(frozen=True)
class Target:
    name: str
    monitor_input: int
    keyboard: int | None  # Easy-Switch channel, 1-based; None = leave alone
    mouse: int | None
    hotkey: Hotkey | None = None


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...]
    monitor_id: str | None = None  # PnP ID such as "MSI3DD6"
    keyboard_filter: str | None = None  # substring of the HID++ device name
    mouse_filter: str | None = None
    next_hotkey: Hotkey | None = None
    prev_hotkey: Hotkey | None = None

    def target(self, name: str) -> Target:
        for t in self.targets:
            if t.name.casefold() == name.casefold():
                return t
        names = ", ".join(t.name for t in self.targets)
        raise ConfigError(f"unknown target {name!r}; configured: {names}")


def default_config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "monitor-switcher" / "config.toml"


def parse_hotkey(text: str) -> Hotkey:
    parts = [p.strip().casefold() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ConfigError(f"hotkey {text!r} needs at least one modifier and a key")
    modifiers = 0
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ConfigError(f"hotkey {text!r}: unknown modifier {part!r}")
        modifiers |= _MODIFIERS[part]
    return Hotkey(text=text, modifiers=modifiers, vk=_parse_key(text, parts[-1]))


def _parse_key(text: str, key: str) -> int:
    if len(key) == 1 and (key.isdigit() or "a" <= key <= "z"):
        return ord(key.upper())
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x6F + int(key[1:])
    if key.startswith("num") and key[3:].isdigit() and len(key) == 4:
        return 0x60 + int(key[3:])
    raise ConfigError(f"hotkey {text!r}: unknown key {key!r}")


def parse_input(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"invalid monitor input {value!r}")
    if isinstance(value, int):
        if not 0 < value <= 0xFF:
            raise ConfigError(f"monitor input {value:#x} is out of range")
        return value
    if isinstance(value, str) and value.casefold() in INPUT_NAMES:
        return INPUT_NAMES[value.casefold()]
    names = ", ".join(INPUT_NAMES)
    raise ConfigError(f"invalid monitor input {value!r}; use one of {names} or a number")


def _parse_channel(target: str, field: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"target {target!r}: {field} must be a channel number from 1")
    return value


def parse_config(data: dict[str, Any]) -> Config:
    raw_targets = data.get("target")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("config needs at least one [[target]] section")

    targets = []
    for i, raw in enumerate(raw_targets, 1):
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[target]] #{i} needs a name")
        if "monitor" not in raw:
            raise ConfigError(f"target {name!r} needs a monitor input")
        hotkey = raw.get("hotkey")
        targets.append(
            Target(
                name=name,
                monitor_input=parse_input(raw["monitor"]),
                keyboard=_parse_channel(name, "keyboard", raw.get("keyboard")),
                mouse=_parse_channel(name, "mouse", raw.get("mouse")),
                hotkey=parse_hotkey(hotkey) if hotkey else None,
            )
        )

    monitor = data.get("monitor", {})
    devices = data.get("devices", {})
    hotkeys = data.get("hotkeys", {})
    config = Config(
        targets=tuple(targets),
        monitor_id=monitor.get("id"),
        keyboard_filter=devices.get("keyboard"),
        mouse_filter=devices.get("mouse"),
        next_hotkey=parse_hotkey(hotkeys["next"]) if hotkeys.get("next") else None,
        prev_hotkey=parse_hotkey(hotkeys["prev"]) if hotkeys.get("prev") else None,
    )
    _check_duplicates(config)
    return config


def _check_duplicates(config: Config) -> None:
    names = Counter(t.name.casefold() for t in config.targets)
    if dupes := [n for n, c in names.items() if c > 1]:
        raise ConfigError(f"duplicate target names: {', '.join(dupes)}")

    inputs = Counter(t.monitor_input for t in config.targets)
    if dupes := [f"{i:#04x}" for i, c in inputs.items() if c > 1]:
        raise ConfigError(f"several targets use the same monitor input: {', '.join(dupes)}")

    hotkeys = [t.hotkey for t in config.targets] + [config.next_hotkey, config.prev_hotkey]
    combos = Counter((h.modifiers, h.vk) for h in hotkeys if h)
    if any(c > 1 for c in combos.values()):
        raise ConfigError("the same hotkey is assigned more than once")


def warnings(config: Config) -> list[str]:
    """Problems that don't stop the tool but probably aren't intended."""
    found = []
    for device in ("keyboard", "mouse"):
        by_channel: dict[int, list[Target]] = {}
        for t in config.targets:
            channel = getattr(t, device)
            if channel is not None:
                by_channel.setdefault(channel, []).append(t)
        for channel, shared in by_channel.items():
            if channel > 3:
                found.append(f"{device} channel {channel}: K375s/M720 only have channels 1-3")
            if len(shared) > 2:
                names = ", ".join(t.name for t in shared)
                found.append(
                    f"{device} channel {channel} is shared by {names}; the monitor's KVM "
                    "can serve at most 2 laptops (USB-C and USB-B upstream)"
                )
            elif len(shared) == 2 and INPUT_NAMES["usbc"] not in {t.monitor_input for t in shared}:
                names = " and ".join(t.name for t in shared)
                found.append(
                    f"{device} channel {channel} is shared by {names}, but neither uses USB-C; "
                    "a shared receiver only works via the monitor's USB-C + USB-B KVM"
                )
    for t in config.targets:
        if t.hotkey is None:
            found.append(f"target {t.name!r} has no hotkey (reachable only via next/prev)")
    return found


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(data)
