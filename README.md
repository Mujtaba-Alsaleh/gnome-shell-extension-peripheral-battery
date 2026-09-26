# Peripheral Battery Status

A GNOME Shell extension for **GNOME 50** that shows the battery level of your
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
- **Lenovo Legion Go** detachable controllers: both batteries shown while
  docked; rows disappear when they are undocked.
- CPU friendly: **purely signal-driven** — no polling. Updates are triggered
  by BlueZ/UPower D-Bus signals (coalesced with a 250 ms debounce), and the
  UI is only rebuilt when the set of devices actually changes.

## Data sources

| Source            | What it covers                                             |
|-------------------|------------------------------------------------------------|
| BlueZ (`org.bluez`)  | Bluetooth devices exposing `Battery1.Percentage`, `Device1.BatteryLevel` or `Device1.BatteryPercentage` (bluez ≥ 5.56/5.64) |
| UPower (`org.freedesktop.UPower`) | USB/HID++/dongle devices, e.g. Logitech mice & keyboards (`UPower.Device`) |
| Legion Go controllers | Docked detachable controllers (`17ef:61eb`/`61ed`), read via hidraw — see below |

Devices are de-duplicated by name; BlueZ wins for Bluetooth devices.
The laptop's own battery and line power are excluded.

### Legion Go controllers

The Legion Go's detachable controllers have their own batteries. Docked, they
enumerate as USB devices (`17ef:61eb`/`61ed` "Legion Controller"); the raw HID
interface streams a status report (id `0x04`) containing both controllers'
battery level, and the stock `hid-lenovo-go` kernel driver passes it through
to hidraw. A **udev rule** (vendor-wide `17ef`) makes the hidraw nodes
readable by your user.

`tools/legion_go_battery.py` runs as the per-user systemd service
`peripheral-battery-legion.service` (created and enabled by `install.sh`). It
reads the stream and writes `~/.cache/peripheral-battery-status/legion-go.json`,
which the extension watches (inotify). Running the helper as a service — not
having the shell spawn it on enable — means it survives shell restarts,
auto-starts at login, and systemd restarts it if it ever dies. Everything else
stays in user space: the only system change is the udev rule (see below), and
no kernel patching or extra packages are needed (`python3` only).

**What to expect with detached controllers:** the report stream keeps sending
battery bytes while the rail is enumerated, and it marks both controllers
"attached" even when one is physically detached — docked and detached frames
are byte-identical. The extension therefore shows whatever the stream reports
and hides a row only when the stream goes silent (the helper writes
`"ok": false` after ~15 s without a report). See
[Troubleshooting](#troubleshooting) for how to tell a live readout from a
stale one.

## Requirements

- **GNOME Shell 50** (e.g. stock GNOME on Fedora 43+, Bazzite GNOME)
- `python3`
- A kernel with the mainline `hid-lenovo-go` driver (present on Fedora,
  Bazzite, Arch, and most current distributions)
- For the Legion Go rows: the controllers must be docked, and the daemon that
  manages them (hhd, or **inputplumber** on Bazzite/SteamOS) must be running
  so the status report streams.

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

The installer first runs **read-only compatibility checks** (GNOME 50,
python3, the Legion Go interface present and its battery report actually
streaming) and only installs anything if every check passes. It detects your
username automatically for both the extension UUID
(`peripheral-battery-status@<your user>`) and the udev rule owner. It may ask
for your **sudo password once when creating the udev rule** — that is
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
- **Rows stay at 100%/99% while the controllers are detached** — the raw
  report stream keeps sending battery bytes (with "attached" still set) while
  the rail is enumerated, so the rows keep their last reported value until the
  stream goes silent. Whether that detached figure is the controller's live
  radio battery or a frozen rail value cannot be told from a single sample:
  let them drain detached and watch
  `cat ~/.cache/peripheral-battery-status/legion-go.json` (or
  `python3 tools/legion_go_capture.py`) — a drifting level is live, a pinned
  one is stale. The row only hides once the stream stops (`"ok": false`).
- **Controllers report 100% immediately after re-docking** after a draining
  session — capture what the interface actually sends during a
  detach/re-dock cycle to check whether the level is stale or genuine:
  `python3 tools/legion_go_capture.py` (see `tools/legion_go_capture.py`).
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
extension.js    the extension (ESM, GNOME 50)
install.sh      one-shot installer: compatibility check, then install
Makefile        make install/uninstall convenience (same templating as the script)
tools/probe.js  standalone probe to dump what BlueZ/UPower report
tools/legion_go_battery.py  Legion Go controller battery helper (systemd user service)
tools/legion_go_capture.py  diagnostic: log raw 0x04/0x74 reports during a detach/re-dock cycle
```

## License

GPL-3.0-or-later. This project is not affiliated with Lenovo.
