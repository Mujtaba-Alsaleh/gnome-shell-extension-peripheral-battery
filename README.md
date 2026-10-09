# Peripheral Battery Status

A GNOME Shell extension for **GNOME 51** that shows the battery level of your
Bluetooth and USB peripherals — mice, keyboards, gamepads, headsets, etc. — in
the **Quick Settings** area of the system menu, similar to the KDE Plasma
battery applet. It also shows the batteries of the **Lenovo Legion Go**'s
detachable left/right controllers.

## Features

- A **Quick Settings toggle** ("Peripherals") listing every peripheral with a
  battery level. No top-panel icon — the toggle only appears in the Quick
  Settings grid.
- Rich rows per device: icon, name, color-coded percentage (red below 20 %,
  orange below 35 %), a thin progress bar, a charging bolt (⚡), and a dimmed
  "Not connected" state for paired-but-off Bluetooth devices.
- The toggle shows the **worst** (lowest) battery among connected devices and
  hides itself completely when no peripheral reports battery info.
- **Low-battery notification** when a connected device drops to ≤ 15 %;
  it re-arms once the device charges back above that threshold.
- **Lenovo Legion Go** detachable controllers: both batteries shown whenever
  the dock is streaming, including while a controller is out of its rail.
- CPU friendly: **purely signal-driven** — no polling. Updates are triggered
  by BlueZ/UPower D-Bus signals (coalesced with a 250 ms debounce), and the
  UI is only rebuilt when the set of devices actually changes.

## Data sources

| Source            | What it covers                                             |
|-------------------|------------------------------------------------------------|
| BlueZ (`org.bluez`)  | Bluetooth devices exposing `Battery1.Percentage`, `Device1.BatteryLevel` or `Device1.BatteryPercentage` (bluez ≥ 5.56/5.64) |
| UPower (`org.freedesktop.UPower`) | USB/HID++/dongle devices, e.g. Logitech mice & keyboards (`UPower.Device`) |
| Legion Go controllers | Detachable controllers (`17ef:61eb`/`61ed`) read via the dock's hidraw interface — see below |

Devices are de-duplicated by name; BlueZ wins for Bluetooth devices.
The laptop's own battery and line power are excluded.

### Legion Go controllers

The Legion Go's detachable controllers have their own batteries. They talk to
the tablet over the dock's radios, and the tablet's own rail enumerates as USB
(`17ef:61eb`/`61ed` "Legion Controller"); the raw HID interface streams a status
report containing both controllers' battery level whether or not they are
docked, and the stock `hid-lenovo-go` kernel driver passes it through to
hidraw. A **udev rule** (vendor-wide `17ef`) makes the hidraw nodes readable by
your user.

`tools/legion_go_battery.py` runs as the per-user systemd service
`peripheral-battery-legion.service` (created and enabled by `install.sh`). It
reads the stream and writes `~/.cache/peripheral-battery-status/legion-go.json`,
which the extension watches (inotify). Running the helper as a service — not
having the shell spawn it on enable — means it survives shell restarts,
auto-starts at login, and systemd restarts it if it ever dies. Everything else
stays in user space: the only system change is the udev rule (see below), and
no kernel patching or extra packages are needed (`python3` only).

**Detached controllers:** the raw stream keeps reporting both battery levels
at all times, and those levels are **live radio telemetry** — measured at 99%
for a controller left switched on and sitting on the desk, well after it was
undocked, and previously verified over an hour-long drain while both
controllers were used detached away from the console. The extension therefore
shows the stream as-is, and a detached row reads *"Not connected"* next to a
level that is still real.

The dock's own docked/undocked bit (the low bit of bytes 12 and 13) travels
alongside the levels rather than gating them. The rail going quiet is what
clears a row: after ~15 s without a report the helper rewrites the payload
with `"pct": null` for both sides, keeping `"ok": true` and the previous
`last_seen`; `"ok": false` with `"last_seen": 0` instead means the interface
could not be found or opened at all. Undocking does *not* clear anything — it
re-enumerates the dock's HID devices for about a second (the node is
renumbered, and the brief gap before udev has finished with the new one can
even make the open fail), and the last state is kept across it. A controller
that powers off while still reporting shows 0%.

## Requirements

- **GNOME Shell 51** (the version `metadata.json` declares)
- `python3`
- A kernel with the mainline `hid-lenovo-go` driver (present on Fedora,
  Bazzite, Arch, and most current distributions)
- For the Legion Go rows: hhd, or **inputplumber** on Bazzite/SteamOS, must be
  running so the status report streams. A controller does not have to be in its
  rail — the levels keep coming either way.

Immutable systems (Bazzite, Fedora Atomic, SteamOS) are supported: the
installer only writes to `~/.local` and `/etc/udev/rules.d` — both mutable and
preserved across OS updates. Nothing is layered with rpm-ostree.

## Install

1. Open a terminal.
2. Download the repository:

   ```sh
   git clone https://github.com/Mujtaba-Alsaleh/gnome-shell-extension-peripheral-battery.git
   cd gnome-shell-extension-peripheral-battery
   ```

3. Make the installer executable:

   ```sh
   chmod +x install.sh
   ```

4. Run it:

   ```sh
   ./install.sh
   ```

5. **Log out and back in.** The "Peripherals" toggle now lists your devices —
   including "Legion Go Left" and "Legion Go Right" while the controllers
   are docked.

### One-liner

```sh
git clone https://github.com/Mujtaba-Alsaleh/gnome-shell-extension-peripheral-battery.git && cd gnome-shell-extension-peripheral-battery && chmod +x install.sh && ./install.sh
```

The installer first runs **read-only compatibility checks** (the GNOME Shell
version `metadata.json` declares, python3, the Legion Go interface present and
its battery report actually streaming) and only installs anything if every
check passes. It detects your username automatically for both the extension
UUID (`peripheral-battery-status@<your user>`) and the udev rule owner. It may
ask for your **sudo password once when creating the udev rule** — that is
expected. `./install.sh --check` runs only the tests and changes nothing.

> The installer is the one supported path. For a manual install you would
> have to do everything it does yourself — including **replacing the
> placeholders** (`your_username` in `metadata.json`) and renaming the
> extension directory to match. Use the script.

Uninstall:

```sh
make uninstall        # removes the extension and the helper service; the udev rule is left behind
sudo rm /etc/udev/rules.d/99-legion-go-battery.rules   # optional: revert access
```

After install or uninstall, **log out and back in** so GNOME Shell reloads
extension code (the shell only scans the extension directories at session
start).

## Troubleshooting

- **No "Legion Go" rows** — check the extension's log for warnings and the
  state file:
  `journalctl --user -u org.gnome.Shell | grep -i legion`
  and `cat ~/.cache/peripheral-battery-status/legion-go.json`.
- **A controller's level doesn't move** — the reported levels are live radio
  telemetry even while detached, but they only change as the battery actually
  drains; a fully-charged detach reads 99% for a long time. A row disappears
  when the rail stops streaming (state file shows `"pct": null`, after ~15 s
  without a report), and 0% means a controller powered off while still
  reporting.
- **The level jumps to a wrong value for a second after a re-dock** — the
  frames that come out of the re-enumeration can carry junk in the battery
  positions (1% and 129% were both seen). The helper drops those frames; if
  one ever gets through, `python3 tools/legion_go_capture.py` logs what the
  interface actually sent, and reports how many non-status frames it skipped.
- **The installer's stream check fails** — the controllers must be docked and
  hhd/inputplumber must be running; run `./install.sh --check` again after
  they are.
- **`gnome-extensions` reports the extension as errored** — read the cause
  with `journalctl --user -u org.gnome.Shell`.
- The "Peripherals" toggle lives in Quick Settings (the grid shown by the
  system menu's sliders/brightness area). With many extensions it may be on a
  later page — the grid scrolls.

## Layout

```
metadata.json   extension metadata
extension.js    the extension (ESM, GNOME 51)
install.sh      one-shot installer: compatibility check, then install
Makefile        make install/uninstall convenience (same templating as the script)
tools/probe.js  standalone probe to dump what BlueZ/UPower report
tools/legion_go_battery.py  Legion Go controller battery helper (systemd user service)
tools/legion_go_capture.py  diagnostic: log status reports across a detach/re-dock cycle
```

## License

GPL-3.0-or-later. This project is not affiliated with Lenovo.
