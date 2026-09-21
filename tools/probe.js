/*
 * Standalone probe: print what BlueZ and UPower report for batteries.
 * Run with:  gjs tools/probe.js
 * Useful to debug why a device isn't (or is) listed by the extension.
 */
const {Gio, GLib} = imports.gi;

const conn = Gio.DBus.system;

function callSync(service, path, iface, method, args, replyType) {
    return conn.call_sync(service, path, iface, method, args, replyType,
        Gio.DBusCallFlags.NONE, -1, null);
}

print('=== UPower (org.freedesktop.UPower) ===');
try {
    const [paths] = callSync('org.freedesktop.UPower', '/org/freedesktop/UPower',
        'org.freedesktop.UPower', 'EnumerateDevices', null, null).recursiveUnpack();
    for (const p of paths) {
        const [props] = callSync('org.freedesktop.UPower', p,
            'org.freedesktop.DBus.Properties', 'GetAll',
            new GLib.Variant('(s)', ['org.freedesktop.UPower.Device']),
            new GLib.VariantType('(a{sv})')).recursiveUnpack();
        print(`  ${p}\n    type=${props.Type} pct=${props.Percentage} ` +
            `model="${props.Model}" supply=${props.PowerSupply} ` +
            `present=${props.IsPresent} state=${props.State} icon=${props.IconName}`);
    }
    print(`  (total ${paths.length} devices)`);
} catch (e) {
    print('  ERROR: ' + e.message);
}

print('\n=== BlueZ (org.bluez) ===');
try {
    const [managed] = callSync('org.bluez', '/',
        'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects',
        null, null).recursiveUnpack();
    let n = 0;
    for (const [path, ifaces] of Object.entries(managed)) {
        if (!path.startsWith('/org/bluez/hci'))
            continue;
        const dev = ifaces['org.bluez.Device1'];
        if (!dev)
            continue;
        const bat = ifaces['org.bluez.Battery1'];
        print(`  ${path}\n    alias="${dev.Alias}" icon=${dev.Icon} ` +
            `conn=${dev.Connected} paired=${dev.Paired}\n    ` +
            `Device1.BatteryLevel=${dev.BatteryLevel} ` +
            `Device1.BatteryPercentage=${dev.BatteryPercentage} ` +
            `Battery1.Percentage=${bat ? bat.Percentage : '-'}`);
        n++;
    }
    print(`  (total ${n} devices)`);
} catch (e) {
    print('  ERROR: ' + e.message);
}