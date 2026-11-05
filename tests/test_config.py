import tomllib
from pathlib import Path

import pytest

from monitor_switcher.config import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    ConfigError,
    load_config,
    parse_config,
    parse_hotkey,
    parse_input,
    warnings,
)

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def targets(*rows):
    return {"target": [dict(r) for r in rows]}


def test_example_config_loads_without_warnings():
    config = load_config(EXAMPLE)
    assert [t.name for t in config.targets] == ["Laptop A", "Laptop B", "Laptop C", "Laptop D"]
    assert config.monitor_id == "MSI3DD6"
    assert warnings(config) == []


def test_mapping_is_independent_per_target():
    config = parse_config(
        targets(
            {"name": "A", "monitor": "hdmi1", "keyboard": 1, "mouse": 1},
            {"name": "B", "monitor": "dp", "keyboard": 2, "mouse": 3},
            {"name": "C", "monitor": 0x10, "keyboard": 3},
        )
    )
    a, b, c = config.targets
    assert (a.monitor_input, a.keyboard, a.mouse) == (0x11, 1, 1)
    assert (b.monitor_input, b.keyboard, b.mouse) == (0x0F, 2, 3)
    assert (c.monitor_input, c.keyboard, c.mouse) == (0x10, 3, None)


def test_target_lookup_is_case_insensitive():
    config = parse_config(targets({"name": "Work Laptop", "monitor": "dp"}))
    assert config.target("work laptop").name == "Work Laptop"
    with pytest.raises(ConfigError, match="unknown target"):
        config.target("other")


@pytest.mark.parametrize(
    ("text", "modifiers", "vk"),
    [
        ("ctrl+alt+shift+1", MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("1")),
        ("Ctrl + Shift + F12", MOD_CONTROL | MOD_SHIFT, 0x7B),
        ("alt+right", MOD_ALT, 0x27),
        ("ctrl+k", MOD_CONTROL, ord("K")),
        ("ctrl+num3", MOD_CONTROL, 0x63),
    ],
)
def test_parse_hotkey(text, modifiers, vk):
    hotkey = parse_hotkey(text)
    assert (hotkey.modifiers, hotkey.vk) == (modifiers, vk)


@pytest.mark.parametrize("text", ["1", "ctrl+", "hyper+1", "ctrl+bogus", "ctrl+f25"])
def test_parse_hotkey_rejects_invalid(text):
    with pytest.raises(ConfigError):
        parse_hotkey(text)


def test_parse_input_names_and_numbers():
    assert parse_input("HDMI2") == 0x12
    assert parse_input("usbc") == 0x10
    assert parse_input(0x1B) == 0x1B
    for bad in ("vga", 0, 300, True):
        with pytest.raises(ConfigError):
            parse_input(bad)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "at least one"),
        ([{"monitor": "dp"}], "needs a name"),
        ([{"name": "A"}], "needs a monitor input"),
        ([{"name": "A", "monitor": "dp", "keyboard": 0}], "channel number"),
        ([{"name": "A", "monitor": "dp"}, {"name": "a", "monitor": "hdmi1"}], "duplicate"),
        ([{"name": "A", "monitor": "dp"}, {"name": "B", "monitor": 0x0F}], "same monitor input"),
        (
            [
                {"name": "A", "monitor": "dp", "hotkey": "ctrl+alt+1"},
                {"name": "B", "monitor": "hdmi1", "hotkey": "alt+ctrl+1"},
            ],
            "same hotkey",
        ),
    ],
)
def test_invalid_configs(rows, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(targets(*rows))


def test_warns_about_shared_channel_without_usbc():
    config = parse_config(
        targets(
            {"name": "A", "monitor": "dp", "keyboard": 3, "hotkey": "ctrl+alt+1"},
            {"name": "B", "monitor": "hdmi1", "keyboard": 3, "hotkey": "ctrl+alt+2"},
        )
    )
    assert any("neither uses USB-C" in w for w in warnings(config))


def test_warns_about_three_laptops_on_one_channel_and_missing_hotkey():
    config = parse_config(
        targets(
            {"name": "A", "monitor": "dp", "mouse": 3},
            {"name": "B", "monitor": "hdmi1", "mouse": 3},
            {"name": "C", "monitor": "usbc", "mouse": 3},
        )
    )
    found = warnings(config)
    assert any("at most 2 laptops" in w for w in found)
    assert any("no hotkey" in w for w in found)


def test_load_config_reports_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[[target]\n")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_example_is_valid_toml():
    with EXAMPLE.open("rb") as f:
        assert "target" in tomllib.load(f)
