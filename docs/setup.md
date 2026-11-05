# Setup guide: one monitor, several laptops

This guide sets up `monitor-switcher` on up to four Windows laptops that
share one monitor and one Logitech keyboard + mouse. When you're done, a
hotkey on the laptop you're using switches the monitor input, the keyboard
and the mouse to another laptop.

Nothing here needs administrator rights. The tool only uses standard per-user
Windows features (a Startup-folder shortcut, a registered hotkey, the monitor
and HID APIs). On a managed work laptop, still follow your organisation's
software policy.

Reference setup used throughout: MSI MPG 321CURX QD-OLED, Logitech K375s
keyboard, Logitech M720 Triathlon mouse. Other DDC/CI monitors and HID++ 2.0
Logitech devices with Easy-Switch should work the same way.

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [Wiring](#2-wiring)
3. [Monitor settings](#3-monitor-settings)
4. [Pair the keyboard and mouse](#4-pair-the-keyboard-and-mouse)
5. [Install Python (no admin)](#5-install-python-no-admin)
6. [Install monitor-switcher](#6-install-monitor-switcher)
7. [Collect each laptop's values](#7-collect-each-laptops-values)
8. [Write the config](#8-write-the-config)
9. [Test](#9-test)
10. [Start at login](#10-start-at-login)
11. [Daily use](#11-daily-use)
12. [Troubleshooting](#12-troubleshooting)
13. [Update or remove](#13-update-or-remove)

## 1. How it fits together

- **Monitor:** the laptop that's on screen sends a DDC/CI command over the
  video cable to change the input. Most monitors only listen to the active
  input, so a switch always starts from the laptop you're looking at.
- **Keyboard and mouse:** K375s and M720 have three Easy-Switch channels,
  each paired to one computer. The tool sends Logitech's HID++
  "change host" command, the same as pressing the Easy-Switch button.
- **Mapping:** each laptop is one `[[target]]` in the config, with its own
  monitor input, keyboard channel and mouse channel. These don't need to
  match: "keyboard channel 2" can mean "monitor input 3".
- **Order:** the tool waits for you to release the hotkey, switches the
  mouse, then the keyboard, then the monitor last.

## 2. Wiring

Three channels, four laptops: two laptops share one channel through a
receiver plugged into the monitor's USB hub. The monitor's built-in KVM
moves that hub, and the receiver, to whichever of its two USB upstream
ports belongs to the input on screen.

| Laptop | Video to monitor | USB to monitor | Keyboard/mouse channel |
|---|---|---|---|
| A | HDMI 1 | none | 1, own Unifying receiver or Bluetooth |
| B | DisplayPort | none | 2, own Unifying receiver or Bluetooth |
| C | USB-C (video and USB in one cable) | via the same USB-C cable | 3, receiver in the monitor's USB-A port |
| D | HDMI 2 | USB-A to USB-B cable into the monitor's upstream port | 3, the same receiver |

- Switching between C and D only changes the monitor input; the keyboard and
  mouse stay on channel 3.
- Laptop C's USB-C port must carry video (DisplayPort Alt Mode, often marked
  with a DP or Thunderbolt logo).
- Connect laptops **directly**. Docks, and especially DisplayLink adapters,
  often block DDC/CI.
- K375s and M720 are **Unifying** devices. Logi Bolt receivers don't work with
  them. One Unifying receiver can hold both the keyboard and the mouse.

Fewer than four laptops? Skip the shared receiver and give each laptop its
own channel.

## 3. Monitor settings

On the MSI MPG 321CURX on-screen menu:

| Setting | Value | Why |
|---|---|---|
| KVM | **Auto** | The USB hub follows the active input |
| Input Auto Scan | **Off** | Otherwise switching to a sleeping laptop bounces back to a live input |
| HDMI CEC | **Off** | PCs don't use CEC; it can wake the monitor or change its input on its own |

This monitor has DDC/CI always on. On other monitors, turn on **DDC/CI** in
the on-screen menu.

## 4. Pair the keyboard and mouse

Pairing is stored in the device and in the receiver, not in the laptop. So
you can pair a Unifying receiver on any computer (your personal PC with
Logitech software), then plug it into the work laptop. The work laptop needs
no Logitech software.

For each channel:

1. Press the Easy-Switch button for that channel on the keyboard (F1/F2/F3 on
   the K375s) or under the mouse, until its light blinks fast.
2. Pair it:
   - **Unifying receiver:** use Logitech Unifying Software or Logi Options on
     any computer, then move the receiver to its laptop or to the monitor's
     USB-A port.
   - **Bluetooth:** Windows Settings > Bluetooth & devices > Add device. This
     needs no admin rights, but your organisation may block Bluetooth.
3. Repeat for the mouse.

Write down which channel belongs to which laptop. The keyboard and mouse may
use different channel numbers for the same laptop; the config handles that.

## 5. Install Python (no admin)

You need Python 3.11 or newer. Check first:

```bash
py --version
```

If it's missing, choose one:

- **python.org installer:** run it, choose *Customize installation*, and
  untick *Use admin privileges when installing py.exe*. It installs into your
  user profile.
- **uv** (Python manager by Astral): installs per-user and can install Python
  for you. See <https://docs.astral.sh/uv/getting-started/installation/>.

If Windows says Python "has been blocked by your organization", application
control (AppLocker/WDAC) is in place. Stop here and ask IT; don't work around
it.

## 6. Install monitor-switcher

Put the tool in a folder that won't move, for example
`%USERPROFILE%\Tools\monitor-switcher`. Clone the repository, or download it
as a ZIP and extract it there.

Then create its environment. Pick **one** option and use it on every laptop.

**Option A, with uv:**

```bash
uv sync --no-dev
```

Run the tool with:

```bash
uv run monitor-switcher --help
```

**Option B, with plain Python:**

```bash
py -m venv .venv
```

```bash
.venv\Scripts\python -m pip install .
```

Run the tool with:

```bash
.venv\Scripts\python -m monitor_switcher --help
```

Behind a corporate proxy, set `HTTPS_PROXY` before installing so pip or uv
can download the one dependency (`hidapi`).

The rest of this guide writes `monitor-switcher <command>`. Replace it with
`uv run monitor-switcher` (option A) or `.venv\Scripts\python -m
monitor_switcher` (option B), run from the tool's folder.

## 7. Collect each laptop's values

On each laptop, while it's the one **on screen** and the keyboard and mouse
are switched to it, run:

```bash
monitor-switcher probe
```

`probe` only reads; it changes nothing. Example output:

```
Monitors (DDC/CI):
  MSI3DD6    \\.\DISPLAY1  current input 0x0f (dp)
             supported inputs: 0x11 (hdmi1), 0x12 (hdmi2), 0x0f (dp), 0x10 (usbc)

Keyboards and mice (HID++ Easy-Switch):
  K370s/K375s Multi-Device Wireless Keyboard [keyboard] via receiver slot 1: channel 2 of 3
  M720 Triathlon Multi-Device Mouse [mouse] via bluetooth: channel 2 of 3
      channel 1: paired 'LAPTOP-A'
      channel 2: paired 'LAPTOP-B'
      channel 3: paired 'LAPTOP-C'

This computer as a config entry:

[monitor]
id = "MSI3DD6"

[[target]]
name = "LAPTOP-B"
hotkey = "ctrl+alt+shift+1"   # pick a free number
monitor = "dp"
keyboard = 2
mouse = 2
```

Note each laptop's `[[target]]` values.

- If the keyboard shows *not responding*, its radio link went idle (it does
  that about a second after the last keystroke). Press a key and run `probe`
  again.
- For laptops C and D, which share channel 3, the keyboard and mouse values
  are the same; only `monitor` differs.

## 8. Write the config

Copy [`config.example.toml`](../config.example.toml) to
`%APPDATA%\monitor-switcher\config.toml` and fill in one `[[target]]` per
laptop with the values from `probe`:

```toml
[monitor]
id = "MSI3DD6"

[[target]]
name = "Laptop A"
hotkey = "ctrl+alt+shift+1"
monitor = "hdmi1"
keyboard = 1
mouse = 1

[[target]]
name = "Laptop B"
hotkey = "ctrl+alt+shift+2"
monitor = "dp"
keyboard = 2
mouse = 2

# ... Laptop C (usbc, channel 3) and Laptop D (hdmi2, channel 3)

[hotkeys]
next = "ctrl+alt+shift+right"
prev = "ctrl+alt+shift+left"
```

- `monitor`: `hdmi1`, `hdmi2`, `dp`, `usbc`, or the raw code `probe` shows
  (such as `0x11`).
- `keyboard` / `mouse`: Easy-Switch channel 1-3. Leave a line out to not
  switch that device.
- `hotkey`: modifiers `ctrl`, `alt`, `shift`, `win` plus a key: `0`-`9`,
  `a`-`z`, `f1`-`f24`, `left`/`right`/`up`/`down`, `home`, `end`, `pageup`,
  `pagedown`, `insert`, `delete`, `space`, `tab`, `num0`-`num9`.
  `ctrl+alt+shift+…` is the default because `ctrl+alt+…` is AltGr on many
  keyboard layouts.
- If more than one Logitech keyboard or mouse is found, add a `[devices]`
  section with part of the device name (see the example config).

Use the **same file on every laptop**, then check it:

```bash
monitor-switcher check
```

## 9. Test

Preview a switch without sending anything:

```bash
monitor-switcher switch "Laptop B" --dry-run
```

Then test the hotkeys in a console window. This window stays open; Ctrl+C
quits:

```bash
monitor-switcher run
```

Press a hotkey. The monitor, keyboard and mouse should move to that laptop.
To come back, the listener must be running on that laptop too.

`switch "Laptop B"` without `--dry-run` also works from the command line. The
keyboard may be missed because its link goes idle before the command starts.
Hold Shift for two seconds right after pressing Enter to keep it awake.

## 10. Start at login

On each laptop:

```bash
monitor-switcher install-autostart
```

```bash
monitor-switcher start
```

```bash
monitor-switcher status
```

`install-autostart` adds a `monitor-switcher` shortcut to your Startup folder
(`shell:startup`). At login it runs the listener with no window. It needs no
admin rights, scheduled task, service or registry entry. The shortcut points
at the tool's folder and the config path, so run `install-autostart` again if
you move either.

## 11. Daily use

| Action | How |
|---|---|
| Switch to a laptop | Its hotkey, e.g. Ctrl+Alt+Shift+1 |
| Cycle laptops | Ctrl+Alt+Shift+Right / Left |
| See what happened | `%LOCALAPPDATA%\monitor-switcher\switcher.log` |
| Stop / start the listener | `monitor-switcher stop` / `monitor-switcher start` |
| After editing the config | `monitor-switcher stop`, then `monitor-switcher start` |

- A failed switch beeps; the log says which step failed.
- If a switch only partly worked, recover by hand with the monitor's input
  button and the Easy-Switch buttons.
- Hotkeys don't work on the lock screen or during a UAC prompt; Windows blocks
  them there.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `monitor ... not found` | The laptop isn't the one on screen, a dock blocks DDC/CI, or DDC/CI is off in the monitor menu. Connect directly; check `probe`. |
| `several monitors match` | Set `[monitor] id` in the config to the ID `probe` shows. |
| Keyboard *not responding* | Its link was idle or it's on another channel. From a hotkey this doesn't happen. From the command line, keep typing (see [Test](#9-test)). |
| Device *channel N* fails | The device has fewer channels than the config asks for. |
| `hotkey ... is taken by another program` | Another app owns that combination. Pick another in the config. The other hotkeys still work. |
| Monitor switches but shows "No signal" | The target laptop is asleep or its cable is loose. With Input Auto Scan on, the monitor may jump back. |
| Laptops C/D get no keyboard | Check that KVM is **Auto**, and that C's USB-C cable and D's USB-B cable both go to the monitor. |
| Nothing happens at login | Run `monitor-switcher status` and check the log. A config error shows a message box. |
| `Python ... blocked by your organization` | Application control is enforced. Ask IT. |

For more detail, add `-v` to any command for debug output.

## 13. Update or remove

**Update:**

```bash
monitor-switcher stop
```

Then pull or extract the new version into the same folder, run step 6 again,
and finish with:

```bash
monitor-switcher start
```

**Remove completely:**

```bash
monitor-switcher stop
```

```bash
monitor-switcher uninstall-autostart
```

Then delete the tool's folder, `%APPDATA%\monitor-switcher` (config) and
`%LOCALAPPDATA%\monitor-switcher` (log).
