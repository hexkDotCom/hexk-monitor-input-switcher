# monitor-switcher

Share one monitor and one Logitech multi-device keyboard and mouse between
several Windows laptops. Press a hotkey on the laptop you're using, and the
monitor input, keyboard and mouse all move to another laptop.

- **No admin rights:** per-user Python, a Startup-folder shortcut, standard
  Windows APIs. No service, driver, scheduled task, registry entry or
  network listener.
- **Monitor:** switches the input over DDC/CI (MCCS VCP `0x60`) through
  Windows' `dxva2.dll`.
- **Keyboard and mouse:** moves Logitech Easy-Switch devices to another
  channel with the HID++ 2.0 `CHANGE_HOST` command, through a Unifying
  receiver or Bluetooth.
- **Flexible mapping:** monitor input, keyboard channel and mouse channel are
  set separately per laptop. Four laptops can share three Easy-Switch
  channels with the monitor's built-in KVM.

**Setting it up on several laptops? Start with the
[setup guide](docs/setup.md).**

## Tested hardware

| Device | Model |
|---|---|
| Monitor | MSI MPG 321CURX QD-OLED (2× HDMI, DP, USB-C, built-in KVM) |
| Keyboard | Logitech K375s (Unifying / Bluetooth, 3 channels) |
| Mouse | Logitech M720 Triathlon (Unifying / Bluetooth, 3 channels) |
| OS | Windows 11 |

Other monitors with DDC/CI input switching, and other Logitech HID++ 2.0
devices with Easy-Switch, should work. Use `probe` to check. Input codes vary
by vendor; any raw VCP value can go in the config.

## Quick start

Requires Windows 10/11 and Python 3.11+ (a per-user install is fine).

```bash
git clone <this repository> monitor-switcher
```

```bash
cd monitor-switcher
```

```bash
uv sync --no-dev
```

Without [uv](https://docs.astral.sh/uv/): `py -m venv .venv`, then
`.venv\Scripts\python -m pip install .`, and use
`.venv\Scripts\python -m monitor_switcher` wherever this README says
`uv run monitor-switcher`.

1. On each laptop, while it's on screen, see what it sees. This only reads:

   ```bash
   uv run monitor-switcher probe
   ```

2. Copy [config.example.toml](config.example.toml) to
   `%APPDATA%\monitor-switcher\config.toml`. Add one `[[target]]` per laptop
   from the `probe` output, and use the same file on every laptop. Then check
   it:

   ```bash
   uv run monitor-switcher check
   ```

3. Try it:

   ```bash
   uv run monitor-switcher switch "Laptop B" --dry-run
   ```

   ```bash
   uv run monitor-switcher run
   ```

   `run` listens for hotkeys in the console until Ctrl+C.

4. Start it at every login, and now, in the background:

   ```bash
   uv run monitor-switcher install-autostart
   ```

   ```bash
   uv run monitor-switcher start
   ```

## Commands

| Command | What it does |
|---|---|
| `probe` | Show monitors, keyboards/mice, current channels, paired host names, and a config entry for this laptop (read-only) |
| `check` | Validate the config and show the mapping |
| `switch NAME` / `switch next` / `switch prev` | Switch now; add `--dry-run` to only show the plan |
| `run` | Listen for hotkeys in the foreground (Ctrl+C to quit) |
| `start` / `stop` | Start the listener in the background / stop it |
| `status` | Is the listener running, is autostart installed, where are the config and log |
| `install-autostart` / `uninstall-autostart` | Add/remove the Startup-folder shortcut |

Global options: `--config PATH` (default
`%APPDATA%\monitor-switcher\config.toml`) and `-v` for debug logging. The
listener logs to `%LOCALAPPDATA%\monitor-switcher\switcher.log`.

## How a switch works

1. The hotkey fires on key-down, on the laptop that's on screen. Most
   monitors only accept DDC/CI from the active input.
2. The tool finds the monitor and the Logitech devices right away, while the
   keyboard's radio link is still active. The K375s drops it about a second
   after the last keystroke.
3. It waits for Ctrl/Alt/Shift/Win and the mouse buttons to be released. That
   way the old laptop doesn't keep a "stuck" modifier.
4. It switches the mouse, then the keyboard (HID++ `setCurrentHost`).
5. It switches the monitor input last (DDC/CI), retrying once. A receiver in
   the monitor's USB hub follows the monitor input, so the devices must move
   first, while still reachable.

If a step fails, the others still run, the listener beeps, and the log says
what happened. Recover by hand with the monitor's input button or the
Easy-Switch buttons.

## Hotkeys

- Hotkeys are registered with `RegisterHotKey`, the standard Windows API, not
  a low-level keyboard hook.
- Holding a hotkey down doesn't repeat it, and hotkeys pressed during a
  switch are ignored.
- A combination already used by another program is logged and skipped; the
  others still work.
- Hotkeys don't fire on the lock screen or a UAC prompt.
- The listener must run on every laptop.

## Development

Code layout:

```
src/monitor_switcher/
  autostart.py  Startup-folder shortcut (IShellLink via ctypes), background start
  cli.py        command line
  config.py     config file, input names, hotkey parsing
  daemon.py     hotkey listener: RegisterHotKey loop, single instance, logging
  ddc.py        DDC/CI monitor control (ctypes over dxva2)
  devices.py    find Logitech devices via hidapi (Unifying receiver + Bluetooth LE)
  hidpp.py      HID++ 2.0 protocol (transport-independent)
  keys.py       wait for modifier/mouse-button release
  switcher.py   switch orchestration and ordering
tests/          pytest unit tests (hardware replaced by fakes)
docs/setup.md   multi-laptop setup guide
```

Install dependencies (including test tools):

```bash
uv sync
```

Run the tests:

```bash
uv run pytest
```

Lint and check formatting:

```bash
uv run ruff check .
```

```bash
uv run ruff format --check .
```

Some tests drive real Win32 APIs (hotkey message loop, shell-link creation)
and are skipped on other platforms.

## License

[MIT](LICENSE)
