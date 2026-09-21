# Peripheral Battery Status

A GNOME Shell extension for **GNOME 50** that shows the battery level of your
Bluetooth and USB peripherals — mice, keyboards, gamepads, headsets, etc. — in
the **Quick Settings** area of the system menu, similar to the KDE Plasma
battery applet.

![screenshot placeholder](docs/screenshot.png)

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
- CPU friendly: **purely signal-driven** — no polling. Updates are triggered
  by BlueZ/UPower D-Bus signals (coalesced with a 250 ms debounce), and the
  UI is only rebuilt when the set of devices actually changes.
- **Lenovo Legion Go controllers** (the detachable LEFT/RIGHT gamepads): their
  battery is read from the docked controllers' USB HID stream by a tiny helper
  (`tools/legion_go_battery.py`) that the extension spawns on demand.

## Data sources

| Source            | What it covers                                             |
|-------------------|------------------------------------------------------------|
| BlueZ (`org.bluez`)  | Bluetooth devices exposing `Battery1.Percentage`, `Device1.BatteryLevel` or `Device1.BatteryPercentage` (bluez ≥ 5.56/5.64) |
| UPower (`org.freedesktop.UPower`) | USB/HID++/dongle devices, e.g. Logitech mice & keyboards (`UPower.Device`) |
| Legion Go controllers | Docked detachable controllers (`17ef:61eb`), read via hidraw — see below |

Devices are de-duplicated by name; BlueZ wins for Bluetooth devices.
The laptop's own battery and line power are excluded.

### Legion Go controllers

The Legion Go's detachable controllers have their own batteries. While docked
they enumerate as a single USB device (`17ef:61eb` "Legion Controller"); the
raw HID interface streams a status report (id `0x04`) containing both
controllers' battery level and attachment state, and the stock
`hid-lenovo-go` kernel driver passes it through to hidraw.

One-time setup (needs root): a udev rule makes the hidraw readable for your
user. It is installed by:

```sh
sudo tee /etc/udev/rules.d/99-legion-go-battery.rules > /dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="17ef", ATTRS{idProduct}=="61eb", OWNER="<user>", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

The extension spawns `tools/legion_go_battery.py` on enable; the helper reads
the stream and writes `~/.cache/peripheral-battery-status/legion-go.json`,
which the extension watches (inotify). While a controller is undocked its row
is kept, dimmed, as "Not connected".

> Note: Bluetooth battery levels only appear if the BlueZ *battery* plugin is
> enabled (default on modern distros; check `/etc/bluetooth/main.conf` →
> `LoadPlugins=battery`).

## Install

The one-shot installer runs a read-only compatibility check first (GNOME 50,
python3, and the Legion Go battery report actually streaming) and only when it
passes, installs the extension and creates the udev rule:

```sh
./install.sh           # check, then install
./install.sh --check   # only run the compatibility test, change nothing
```

Manual (extension only):

```sh
make install      # copies to ~/.local/share/gnome-shell/extensions/...
```

or manually:

```sh
cp -r . ~/.local/share/gnome-shell/extensions/peripheral-battery-status@your_username
gnome-extensions enable peripheral-battery-status@your_username
```

For a development link instead of a copy:

```sh
ln -s "$PWD" ~/.local/share/gnome-shell/extensions/peripheral-battery-status@your_username
gnome-extensions enable peripheral-battery-status@your_username
```

Restart the shell (`Alt+F2` → `r`, or log out/in) after a fresh
extension install on X11, or simply log out/in on Wayland — the shell
only scans the extension directories at session start (GNOME 45+).
`gnome-extensions enable <uuid>` then takes effect after the next login.

## Troubleshooting

- Nothing shows: your peripherals don't report battery info, the BlueZ battery
  plugin is off, or UPower isn't running. Check
  `journalctl --user -f` and look for `Peripheral Battery Status`
  messages, and run `gjs /path/to/probe.js`-style D-Bus queries
  (see `tools/probe.js`).
- Extension disabled/errored: `gnome-extensions info peripheral-battery-status@your_username`
  and `journalctl --user -u org.gnome.Shell`.
- The "Peripherals" toggle lives in Quick Settings (the grid shown by the
  system menu's sliders/brightness area). With many extensions it may be on a
  later page — the grid scrolls.

## Layout

```
metadata.json   extension metadata
extension.js    the extension (ESM, GNOME 50)
install.sh      one-shot installer: compatibility check, then install
tools/probe.js  standalone probe to dump what BlueZ/UPower report
tools/legion_go_battery.py  Legion Go controller battery helper (spawned)
```

## License

MIT — do whatever you like.
