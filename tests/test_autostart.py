import subprocess
import sys
from pathlib import Path

import pytest

from monitor_switcher import autostart

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows shell")


def test_launch_args_are_isolated_and_use_absolute_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = autostart.launch_args(Path("config.toml"))
    assert args == [
        "-I",
        "-m",
        "monitor_switcher",
        "--config",
        str(tmp_path / "config.toml"),
        "run",
    ]


def test_pythonw_sits_next_to_the_running_interpreter():
    assert autostart.pythonw().parent == Path(sys.executable).parent


@windows_only
def test_startup_folder_is_the_users_startup_folder():
    folder = autostart.startup_folder()
    assert folder.name == "Startup"
    assert folder.is_dir()


@windows_only
def test_install_writes_a_shortcut_and_uninstall_removes_it(tmp_path):
    config = tmp_path / "my config.toml"
    path = autostart.install(config, folder=tmp_path)
    assert path == tmp_path / autostart.SHORTCUT_NAME

    data = path.read_bytes()
    assert data[:4] == b"\x4c\x00\x00\x00"  # shell link header size
    arguments = subprocess.list2cmdline(autostart.launch_args(config))
    assert arguments.encode("utf-16-le") in data
    assert '"' in arguments  # the path with a space is quoted
    assert autostart.DESCRIPTION.encode("utf-16-le") in data

    assert autostart.uninstall(folder=tmp_path)
    assert not path.exists()
    assert not autostart.uninstall(folder=tmp_path)


@windows_only
def test_installed_shortcut_command_starts_the_package(tmp_path):
    """The exact interpreter + flags the shortcut uses can import the package."""
    result = subprocess.run(
        [str(Path(sys.executable)), "-I", "-m", "monitor_switcher", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "install-autostart" in result.stdout
