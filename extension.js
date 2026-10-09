/*
 * Peripheral Battery Status
 *
 * Shows the battery level of Bluetooth and USB peripherals
 * (mice, keyboards, gamepads, headsets, ...) in the Notification/
 * Quick Settings area of the system menu, similar to KDE Plasma's
 * battery applet.
 *
 * Data sources:
 *   - BlueZ (org.bluez)                     : Bluetooth devices with battery info
 *   - UPower (org.freedesktop.UPower)       : USB/HID++/dongle devices
 *   - Legion Go controllers (hidraw + helper): docked detachable controllers
 *
 * CPU friendly: completely signal-driven (no polling); UI is only
 * rebuilt when the set of devices actually changes.
 *
 * Built for GNOME Shell 51 (ESM extension format); uses the Quick Settings
 * external-indicator API (Main.panel.statusArea.quickSettings).
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

/* ---------------------------------------------------------------- */
/* Constants                                                         */
/* ---------------------------------------------------------------- */

const DEBOUNCE_MS = 250;            // coalesce bursts of D-Bus signals
const LOW_BATTERY_THRESHOLD = 15;   // % below which we send a notification
const BAR_WIDTH = 72;               // progress bar width in px

const BLUEZ_SERVICE = 'org.bluez';
const BLUEZ_DEVICE_IFACE = 'org.bluez.Device1';
const BLUEZ_BATTERY_IFACE = 'org.bluez.Battery1';

const UPOWER_SERVICE = 'org.freedesktop.UPower';
const UPOWER_ROOT_PATH = '/org/freedesktop/UPower';
const UPOWER_IFACE = 'org.freedesktop.UPower';
const UPOWER_DEVICE_IFACE = 'org.freedesktop.UPower.Device';

const PROPERTIES_IFACE = 'org.freedesktop.DBus.Properties';
const OBJECT_MANAGER_IFACE = 'org.freedesktop.DBus.ObjectManager';

/* UPower device kinds we never want to show ("not a peripheral") */
const UPOWER_SKIP_KINDS = new Set([0, 1, 3]); // unknown, line power, UPS

const DEFAULT_ICON = 'battery-symbolic';

/* Legion Go controller source: helper writes a state file we watch.
 * The helper (tools/legion_go_battery.py) runs as the systemd user unit
 * LEGION_SERVICE (installed by install.sh) so it survives shell restarts.
 * Gio.Subprocess spawning is unreliable in some environments, so the
 * extension never execs the helper itself - it only asks systemd to run it. */
const LEGION_STATE_DIR = GLib.build_filenamev([
    GLib.get_user_cache_dir(), 'peripheral-battery-status']);
const LEGION_STATE_FILE = GLib.build_filenamev([LEGION_STATE_DIR, 'legion-go.json']);
const LEGION_SERVICE = 'peripheral-battery-legion.service';
const LEGION_ICON = 'input-gaming-symbolic';

const UPowerIcons = {
    4: 'video-display-symbolic',    // monitor
    5: 'input-mouse-symbolic',      // mouse
    6: 'input-keyboard-symbolic',   // keyboard
    7: 'phone-symbolic',            // PDA
    8: 'phone-symbolic',            // phone
    9: 'audio-card-symbolic',       // media player
    10: 'input-tablet-symbolic',    // tablet
    11: 'computer-symbolic',        // computer
    12: 'input-gaming-symbolic',    // gaming input
    13: 'input-tablet-symbolic',    // pen
    14: 'input-touchpad-symbolic',  // touchpad
    15: 'network-wireless-symbolic',// modem
    16: 'network-wireless-symbolic',// network device
    17: 'audio-headset-symbolic',   // headset
    18: 'audio-speakers-symbolic',  // speakers
    19: 'audio-headphones-symbolic',// headphones
    20: 'video-display-symbolic',   // video gadget
    21: 'input-gaming-symbolic',    // remote control
    22: 'printer-symbolic',         // printer
    23: 'input-touchpad-symbolic',  // scanner
    24: 'camera-photo-symbolic',    // camera
    25: 'face-smile-symbolic',      // wearable
    26: 'input-mouse-symbolic',     // toy
    27: 'input-mouse-symbolic',     // keyboard+mouse combo
    28: 'input-gaming-symbolic',    // gaming controller
};

const BluezIcons = {
    'input-mouse': 'input-mouse-symbolic',
    'input-keyboard': 'input-keyboard-symbolic',
    'input-gaming': 'input-gaming-symbolic',
    'input-tablet': 'input-tablet-symbolic',
    'input-touchpad': 'input-touchpad-symbolic',
    'audio-headset': 'audio-headset-symbolic',
    'audio-headphones': 'audio-headphones-symbolic',
    'audio-card': 'audio-speakers-symbolic',
    'phone': 'phone-symbolic',
    'computer': 'computer-symbolic',
    'video-display': 'video-display-symbolic',
    'camera-photo': 'camera-photo-symbolic',
};

const UPOWER_STATE_CHARGING = 1;
const UPOWER_STATE_FULL = 4;

/* ---------------------------------------------------------------- */
/* Helpers                                                           */
/* ---------------------------------------------------------------- */

function iconForUpowerType(type) {
    return UPowerIcons[type] ?? DEFAULT_ICON;
}

function iconForBluezIcon(icon) {
    return BluezIcons[icon] ?? DEFAULT_ICON;
}

function lowTextColor(pct) {
    if (pct <= 20)
        return '#e01b24'; // red
    if (pct <= 35)
        return '#ff7800'; // orange
    return null;
}

function barFillColor(pct) {
    if (pct <= 20)
        return '#e01b24'; // red
    if (pct <= 35)
        return '#ff7800'; // orange
    return '#2ec27e';     // green
}

/* ---------------------------------------------------------------- */
/* Extension                                                         */
/* ---------------------------------------------------------------- */

export default class PeripheralBatteryExtension extends Extension {
    enable() {
        this._devices = new Map();       // key (normalized name) -> device object
        this._lastFingerprint = null;    // change detection for the UI
        this._alerted = new Map();       // key -> true while low-battery alert active
        this._refreshIdle = 0;
        this._subs = [];
        this._legionSig = null;          // last Legion state we acted on

        this._buildQuickSettings();

        this._subscribeSignals();
        this._ensureLegionMonitor();
        this._ensureLegionHelper();
        this._refresh();
    }

    disable() {
        if (this._refreshIdle) {
            GLib.source_remove(this._refreshIdle);
            this._refreshIdle = 0;
        }
        for (const id of this._subs)
            Gio.DBus.system.signal_unsubscribe(id);
        this._subs = [];

        // Stop watching the Legion helper's state file. (The helper itself
        // is a systemd user service and keeps running across shell restarts.)
        this._legionRespawnAfter = 0;
        try {
            this._legionMonitor?.cancel();
        } catch (e) { /* already dead */ }
        this._legionMonitor = null;

        // The menu actor lives in the QuickSettings overlay, not in the
        // toggle, so destroy it explicitly before the toggle.
        this._toggle?.menu.destroy();
        this._toggle?.destroy();
        this._toggle = null;
        this._indicator?.destroy();
        this._indicator = null;
    }

    /* ---------------- Quick Settings UI ---------------- */

    _buildQuickSettings() {
        this._indicator = new QuickSettings.SystemIndicator();

        this._toggle = new QuickSettings.QuickMenuToggle({
            title: 'Peripherals',
            subtitle: null,
            iconName: DEFAULT_ICON,
            toggleMode: false,       // informational toggle: never shows "on"
            menuEnabled: true,
        });

        // Clicking anywhere on the row opens the devices menu.
        this._toggle.connect('clicked', () => this._toggle.menu.toggle());

        this._toggle.menu.setHeader(DEFAULT_ICON, 'Peripherals');
        this._section = new PopupMenu.PopupMenuSection();
        this._toggle.menu.addMenuItem(this._section);

        this._indicator.quickSettingsItems.push(this._toggle);

        // Since GNOME 50: external indicators are attached through the Quick
        // Settings menu's own API (Main.panel itself has no such method).
        const quickSettings = Main.panel.statusArea.quickSettings;
        if (!quickSettings?.addExternalIndicator) {
            throw new Error('Quick Settings external indicators '
                + '(addExternalIndicator) not available in this shell version');
        }
        quickSettings.addExternalIndicator(this._indicator);

        this._toggle.visible = false;
    }

    /* ---------------- D-Bus plumbing ---------------- */

    _dbusCallSync(service, path, iface, method, params, replyType) {
        return Gio.DBus.system.call_sync(service, path, iface, method,
            params, replyType, Gio.DBusCallFlags.NONE, -1, null);
    }

    _scheduleRefresh() {
        if (this._refreshIdle)
            return;
        this._refreshIdle = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, DEBOUNCE_MS, () => {
                this._refreshIdle = 0;
                this._refresh();
                return GLib.SOURCE_REMOVE;
            });
    }

    _subscribeSignals() {
        // Live percentage updates from BlueZ and UPower devices.
        this._subs.push(Gio.DBus.system.signal_subscribe(
            null, PROPERTIES_IFACE, 'PropertiesChanged', null, null,
            Gio.DBusSignalFlags.NONE,
            (conn, sender, objPath, iface, signal, params) => {
                if (objPath.startsWith('/org/bluez/hci') ||
                    objPath.startsWith('/org/freedesktop/UPower/devices'))
                    this._scheduleRefresh();
            }));

        // BlueZ device added/removed (ObjectManager on '/').
        this._subs.push(Gio.DBus.system.signal_subscribe(
            null, OBJECT_MANAGER_IFACE, null, '/', null,
            Gio.DBusSignalFlags.NONE, () => this._scheduleRefresh()));

        // UPower device added/removed.
        this._subs.push(Gio.DBus.system.signal_subscribe(
            null, UPOWER_IFACE, null, UPOWER_ROOT_PATH, null,
            Gio.DBusSignalFlags.NONE, () => this._scheduleRefresh()));
    }

    /* ---------------- Data collection ---------------- */

    _refresh() {
        // Self-heal: make sure the Legion helper is alive before we read
        // its state file. Cheap (single get_if_exited call) and idempotent
        // thanks to the flock + respawn back-off.
        this._ensureLegionHelper();

        const devices = new Map();

        try {
            this._collectUpowerDevices(devices);
        } catch (e) {
            console.warn(`[${this.metadata.uuid}] UPower: ${e.message}`);
        }

        try {
            this._collectBluezDevices(devices);
        } catch (e) {
            console.warn(`[${this.metadata.uuid}] BlueZ: ${e.message}`);
        }

        try {
            this._collectLegionDevices(devices);
        } catch (e) {
            console.warn(`[${this.metadata.uuid}] Legion: ${e.message}`);
        }

        this._devices = devices;
        this._checkLowBattery();
        this._updateUi();
    }

    _collectUpowerDevices(devices) {
        const reply = this._dbusCallSync(
            UPOWER_SERVICE, UPOWER_ROOT_PATH,
            UPOWER_IFACE, 'EnumerateDevices', null, null);
        const [paths] = reply.recursiveUnpack();

        for (const path of paths) {
            const props = this._upowerDeviceProps(path);
            if (!props)
                continue;

            const type = props.Type ?? 0;
            if (UPOWER_SKIP_KINDS.has(type))
                continue;
            // The laptop's own battery is not a peripheral.
            if (type === 2 && props.PowerSupply === true)
                continue;

            const pct = Math.round(props.Percentage ?? -1);
            if (pct <= 0 || props.IsPresent === false)
                continue;

            const name = props.Model ||
                path.substring(path.lastIndexOf('/') + 1).replace(/[_-]/g, ' ');

            const key = name.trim().toLowerCase();
            devices.set(key, {
                key,
                name: name.trim(),
                percentage: pct,
                icon: iconForUpowerType(type),
                charging: props.State === UPOWER_STATE_CHARGING ||
                    props.State === UPOWER_STATE_FULL,
                connected: true,
            });
        }
    }

    _upowerDeviceProps(path) {
        const reply = this._dbusCallSync(
            UPOWER_SERVICE, path, PROPERTIES_IFACE, 'GetAll',
            new GLib.Variant('(s)', [UPOWER_DEVICE_IFACE]),
            new GLib.VariantType('(a{sv})'));
        const [props] = reply.recursiveUnpack();
        return props;
    }

    _collectBluezDevices(devices) {
        const reply = this._dbusCallSync(
            BLUEZ_SERVICE, '/', OBJECT_MANAGER_IFACE,
            'GetManagedObjects', null, null);
        const [managed] = reply.recursiveUnpack();

        for (const [path, ifaces] of Object.entries(managed)) {
            if (!path.startsWith('/org/bluez/hci'))
                continue;

            const dev = ifaces[BLUEZ_DEVICE_IFACE];
            if (!dev)
                continue;
            if (!dev.Paired && !dev.Connected)
                continue;

            const pct = this._bluezPercentage(dev, ifaces);
            if (pct == null || pct <= 0)
                continue;

            const name = dev.Alias || dev.Name ||
                path.substring(path.lastIndexOf('_') + 1);

            // Prefer the BlueZ record over a UPower record with the
            // same name (BlueZ data is fresher for Bluetooth devices).
            const key = name.trim().toLowerCase();
            devices.set(key, {
                key,
                name: name.trim(),
                percentage: pct,
                icon: iconForBluezIcon(dev.Icon),
                charging: dev.Charging === true,
                connected: dev.Connected === true,
            });
        }
    }

    _bluezPercentage(dev, ifaces) {
        // bluez >= 5.64: percentage reported directly on Device1.
        if (typeof dev.BatteryPercentage === 'number')
            return Math.round(dev.BatteryPercentage);
        // Classic org.bluez.Battery1 interface.
        const battery = ifaces[BLUEZ_BATTERY_IFACE];
        if (battery && typeof battery.Percentage === 'number')
            return Math.round(battery.Percentage);
        // bluez 5.56+: byte battery level on Device1.
        if (typeof dev.BatteryLevel === 'number')
            return Math.round(dev.BatteryLevel);
        return null;
    }

    /* ---------------- Legion Go controllers ---------------- */

    _ensureLegionMonitor() {
        if (this._legionMonitor)
            return;
        // Watch the state directory (created by us or by the helper) so
        // events arrive even before the helper first writes the file.
        try {
            Gio.File.new_for_path(LEGION_STATE_DIR)
                .make_directory_with_parents(null);
        } catch (e) { /* exists or not creatable: monitor below may fail */ }
        try {
            const dir = Gio.File.new_for_path(LEGION_STATE_DIR);
            this._legionMonitor = dir.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._legionMonitor.connect('changed', (mon, file, other, event) => {
                // The helper writes atomically (tmp file + rename), and with
                // WATCH_MOVES a rename arrives as RENAMED: file=old name,
                // other=new name. Only checking `file` therefore missed every
                // state update; accept the destination from either slot.
                const target = file?.get_basename() === 'legion-go.json'
                    ? file
                    : other?.get_basename() === 'legion-go.json' ? other : null;
                if (!target)
                    return;
                // The helper rewrites the file for every report (~40/s), so
                // refresh only when the values we display actually changed.
                if (this._legionStateChanged(target))
                    this._scheduleRefresh();
            });
        } catch (e) {
            console.warn(`[${this.metadata.uuid}] Legion monitor: ${e.message}`);
        }
    }

    _ensureLegionHelper() {
        // The helper is a systemd user service; Gio.Subprocess spawning is
        // unreliable in some environments, so we never exec it ourselves.
        // Just ask systemd to (re)start the unit if it isn't running.
        const now = Date.now();
        if (now < (this._legionRespawnAfter ?? 0))
            return;                         // don't spam systemd
        this._legionRespawnAfter = now + 30000;
        try {
            const bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);
            bus.call('org.freedesktop.systemd1', '/org/freedesktop/systemd1',
                'org.freedesktop.systemd1.Manager', 'StartUnit',
                new GLib.Variant('(ss)', [LEGION_SERVICE, 'fail']),
                new GLib.VariantType('(o)'),
                Gio.DBusCallFlags.NONE, 5000, null,
                (b, res) => {
                    try { b.call_finish(res); }
                    catch (e) { /* already running or unit not installed */ }
                });
        } catch (e) {
            console.warn(`[${this.metadata.uuid}] Legion helper service: ${e.message}`);
        }
    }

    _legionStateChanged(file) {
        let state;
        try {
            const [, contents] = file.load_contents(null);
            state = JSON.parse(new TextDecoder().decode(contents));
        } catch (e) {
            return false;               // transient read error: not a change
        }
        const sig = `${state.ok}|${state.left?.pct}|${state.left?.attached}`
            + `|${state.right?.pct}|${state.right?.attached}`;
        if (sig === this._legionSig)
            return false;
        this._legionSig = sig;
        return true;
    }

    _collectLegionDevices(devices) {
        const file = Gio.File.new_for_path(LEGION_STATE_FILE);
        if (!file.query_exists(null))
            return;

        let contents;
        try {
            [, contents] = file.load_contents(null);
        } catch (e) {
            return;
        }
        let state;
        try {
            state = JSON.parse(new TextDecoder().decode(contents));
        } catch (e) {
            return;
        }
        if (state.ok !== true)
            return;

        for (const side of ['left', 'right']) {
            const s = state[side];
            if (!s)
                continue;
            const pct = Math.round(s.pct ?? -1);
            if (pct < 0)
                continue;

            const key = `legion go ${side}`;
            devices.set(key, {
                key,
                name: `Legion Go ${side === 'left' ? 'Left' : 'Right'}`,
                percentage: pct,
                icon: LEGION_ICON,
                charging: false,
                connected: s.attached === true,
            });
        }
    }

    /* ---------------- UI update ---------------- */

    _sortedDevices() {
        return [...this._devices.values()].sort((a, b) =>
            (b.connected - a.connected) || (a.percentage - b.percentage));
    }

    _updateUi() {
        const list = this._sortedDevices();

        const fingerprint = list.map(d =>
            `${d.key}|${d.percentage}|${d.charging}|${d.connected}|${d.icon}`).join(';');
        if (fingerprint === this._lastFingerprint)
            return;
        this._lastFingerprint = fingerprint;

        this._toggle.visible = list.length > 0;
        if (list.length === 0)
            return;

        const worst = list[0];
        const connected = list.filter(d => d.connected);
        const subtitle = connected.length > 1
            ? `${connected.length} connected \u00b7 ${worst.percentage}%`
            : `${(connected[0] ?? worst).name} \u00b7 ${worst.percentage}%`;

        this._toggle.icon_name = worst.icon;
        this._toggle.title = 'Peripherals';
        this._toggle.subtitle = subtitle;
        this._toggle.menu.setHeader(worst.icon, 'Peripherals', subtitle);

        this._section.removeAll();
        for (const device of list)
            this._section.addMenuItem(this._deviceRow(device));
    }

    _deviceRow(device) {
        const row = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });

        const icon = new St.Icon({
            style_class: 'popup-menu-icon',
            icon_name: device.icon,
        });
        row.add_child(icon);

        const name = new St.Label({
            text: device.name,
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        row.add_child(name);

        row.add_child(this._batteryBar(device.percentage));

        let pctText = `${device.percentage}%`;
        if (device.charging)
            pctText += ' \u26A1'; // charging bolt
        const pct = new St.Label({
            text: pctText,
            y_align: Clutter.ActorAlign.CENTER,
        });
        const color = lowTextColor(device.percentage);
        if (color)
            pct.set_style(`color: ${color};`);
        row.add_child(pct);

        if (!device.connected) {
            const sub = new St.Label({
                text: 'Not connected',
                style_class: 'device-subtitle',
                y_align: Clutter.ActorAlign.CENTER,
            });
            sub.set_style('opacity: 0.6;');
            row.add_child(sub);
        }

        return row;
    }

    _batteryBar(pct) {
        const track = new St.BoxLayout({
            width: BAR_WIDTH,
            height: 6,
            y_align: Clutter.ActorAlign.CENTER,
        });
        track.set_style(
            'background-color: rgba(127,127,127,0.3); border-radius: 3px;');

        const fill = new St.Widget({
            width: Math.max(2, Math.round(BAR_WIDTH * pct / 100)),
            height: 4,
        });
        fill.set_style(
            `background-color: ${barFillColor(pct)}; border-radius: 2px;`);
        track.add_child(fill);

        return track;
    }

    /* ---------------- Low battery notification ---------------- */

    _checkLowBattery() {
        for (const [key, dev] of this._devices) {
            const isLow = dev.connected &&
                dev.percentage <= LOW_BATTERY_THRESHOLD;

            if (isLow && this._alerted.get(key) !== true) {
                this._alerted.set(key, true);
                Main.notify(
                    `Low battery: ${dev.name}`,
                    `\u201C${dev.name}\u201D has ${dev.percentage}% battery left.`);
            } else if (!isLow) {
                this._alerted.set(key, false);
            }
        }

        // Forget devices that are gone, so a reappearance re-arms the alert.
        for (const key of [...this._alerted.keys()])
            if (!this._devices.has(key))
                this._alerted.delete(key);
    }
}