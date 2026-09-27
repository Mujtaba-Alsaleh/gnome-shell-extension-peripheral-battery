#!/usr/bin/env python3
"""
Legion Go controller battery helper for "Peripheral Battery Status".

Reads the raw HID interface of the docked Lenovo Legion Go controllers
(the stock hid-lenovo-go kernel driver passes report id 0x04 through to
hidraw) and publishes the LEFT/RIGHT controller battery + attachment
state to a small state file that the GNOME Shell extension watches via
inotify.

Report layout (the 64-byte status report, report id 0x04), measured on this
unit rather than taken from documentation:
    byte  5         left controller battery (0-100)
    byte  7         right controller battery (0-100)
    byte 12 bit 0   left controller docked    (bit 0 clear -> docked)
    byte 13 bit 0   right controller docked   (bit 0 clear -> docked)

Undocking the right controller was observed to move byte 13 from 0x02 to 0x01
while byte 12 stayed at 0x02. The flag is therefore the low bit, not bit 7:
those bytes only ever hold a small value, so reading bit 7 answers "docked"
no matter what. Bytes 10 and 11 read 0x01 in every captured report and are
deliberately not interpreted.

A docked controller is not the only one the dock can report on: one left
switched on and sitting on the desk kept reporting its real level (99%, minutes
after it was undocked), because it stays in radio contact with the tablet. So
the level is published whether or not the controller is docked, and `attached`
travels alongside it. Only the frames that arrive during the re-dock
transition are unreliable, and those are dropped by is_status_report() below:
the same endpoint also carries other 64-byte messages with the same report id,
which are button/axis traffic, with attachment bytes of 0x00 or 0x80 and
nonsense in the battery positions - 1% and 129% were both seen.

The state file is only rewritten when the values change.

The helper single-instances itself with an flock on the state directory:
the extension always spawns it, and a competing instance simply exits.

Losing the interface is not treated as losing the reading: undocking
re-enumerates the dock and the open descriptor dies with EIO, but the dock
comes straight back with a fresh node and the level resumes about a second
later, so the last state is kept rather than blanked. The rows are only
cleared when the rail really is unusable - no interface found, or no report
for SILENCE_TIMEOUT seconds.

State file format (JSON):
    {
      "ok": true,
      "last_seen": <epoch seconds>,
      "left":  {"pct": 100, "attached": true},
      "right": {"pct": 99, "attached": false}
    }

The helper exits when its stdin reaches EOF, so the extension can manage
its lifetime by keeping a pipe open (and closing it to stop the helper).
"""

import fcntl
import json
import os
import select
import signal
import stat
import sys
import time

REPORT_BYTES = 64
# The status report's own id, and only that one: 0x74 never appeared on the wire
# and hhd's own map for this dock keys buttons and axes on it, so accepting it
# would mean publishing button traffic as a battery level.
ACCEPTED_IDS = (0x04,)
HIDRAW_SYSFS = "/sys/class/hidraw"
HID_SYSFS = "/sys/bus/hid/devices"
LEGION_VID_PREFIX = "0003:17EF:"          # Lenovo
# The dock exposes three interfaces and only the config (raw) one declares the
# vendor usage page; the gamepad and pointer interfaces are Generic Desktop.
# Identifying it by usage page rather than by the HID_PHYS "/input2" index
# matters because docking and undocking the controllers makes the kernel create
# fresh hid devices and renumber hidraw - a cached or index-based answer ends
# up pointing at a node that no longer exists.
LEGION_RAW_USAGE_PAGE = 0xFFA0
LEGION_RAW_PHYS_SUFFIX = "/input2"         # fallback if no descriptor can be read

SILENCE_TIMEOUT = 15.0               # seconds without a report -> "ok": false
DISCOVERY_RETRY = 3.0                # seconds between device re-scans

STATE_DIR = os.path.join(os.path.expanduser("~/.cache"),
                         "peripheral-battery-status")
STATE_PATH = os.path.join(STATE_DIR, "legion-go.json")
LOCK_PATH = os.path.join(STATE_DIR, "legion-go.lock")


def _acquire_lock():
    """Single-instance guard via flock on the state directory.

    The extension always spawns the helper; this lock makes a competing
    instance exit silently, so a helper that survived a shell restart is
    never duplicated. The lock dies with the process, so there is nothing
    to clean up.
    """
    global _lock_fd
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        _lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        if _lock_fd is not None:
            try:
                os.close(_lock_fd)
            except OSError:
                pass
        return False


def _first_usage_page(descriptor):
    """Usage page of the first usage in a HID report descriptor.

    A short item is one prefix byte plus a 0/1/2/4 byte little-endian value,
    with the size in the prefix's low two bits; long items (0xFE) carry their
    own data-size byte and are skipped.
    """
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        i += 1
        if prefix == 0xFE:                       # long item
            if i >= len(descriptor):
                return None
            i += 1 + descriptor[i]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(descriptor[i:i + size], "little")
        i += size
        if prefix & 0xFC == 0x04:                # Global, tag 0: Usage Page
            return value
    return None


def _hid_node(node):
    """(HID_PHYS, report descriptor) for a dock interface, or None.

    Anything that is not one of the dock's own interfaces gives None, so an
    unreadable sysfs entry is indistinguishable from "not ours".
    """
    device = os.path.realpath(os.path.join(HIDRAW_SYSFS, node, "device"))
    hid = os.path.basename(device)
    if not hid.startswith(LEGION_VID_PREFIX):
        return None
    base = os.path.join(HID_SYSFS, hid)
    try:
        with open(f"{base}/uevent") as f:
            ue = dict(line.split("=", 1) for line in f.read().splitlines()
                      if "=" in line)
    except OSError:
        return None
    if "legion" not in ue.get("HID_NAME", "").lower():
        return None
    try:
        with open(f"{base}/report_descriptor", "rb") as f:
            descriptor = f.read()
    except OSError:
        descriptor = b""
    return ue.get("HID_PHYS", ""), descriptor


def _is_raw_interface(phys, descriptor):
    page = _first_usage_page(descriptor)
    if page is not None:
        return page == LEGION_RAW_USAGE_PAGE
    return phys.endswith(LEGION_RAW_PHYS_SUFFIX)


def discover_raw_hidraw():
    """Return /dev/hidrawN of the Legion raw (config) interface, or None."""
    try:
        nodes = sorted(os.listdir(HIDRAW_SYSFS))
    except OSError:
        return None

    fallback = None
    for node in nodes:
        info = _hid_node(node)
        if info is None:
            continue
        phys, descriptor = info
        if _first_usage_page(descriptor) == LEGION_RAW_USAGE_PAGE:
            return os.path.join("/dev", node)
        if fallback is None and phys.endswith(LEGION_RAW_PHYS_SUFFIX):
            fallback = os.path.join("/dev", node)   # descriptor unreadable
    return fallback


def is_status_report(buf):
    """Whether a 64-byte frame is a status report and not another message.

    Everything on this endpoint is 64 bytes with report id 0x04, so the id
    cannot tell the kinds apart. A status report's attachment bytes (12, 13)
    are a small value with the docked flag in the low bit; the other messages
    use 0x00 or 0x80 there and their battery bytes are not a level at all.
    """
    if len(buf) < REPORT_BYTES or buf[0] not in ACCEPTED_IDS:
        return False
    return all(0 < byte < 0x80 for byte in (buf[12], buf[13]))


def parse_report(buf):
    """Extract battery/attachment state from a status report, or None."""
    if not is_status_report(buf):
        return None
    return {
        "left":  min(100, buf[5]),
        "right": min(100, buf[7]),
        # attachment flag is inverted: bit 0 clear -> docked
        "attL":  1 - (buf[12] & 1),
        "attR":  1 - (buf[13] & 1),
    }


def snapshot(state, last_seen, ok=True):
    """Fold the raw parse into the public JSON state.

    The level is published whether or not the controller is docked: a
    detached controller stays in radio contact with the tablet and the dock
    keeps reporting its real level - 99% was observed for one left switched
    on and sitting on the desk, well after it was undocked. Only the frames
    that arrive during the re-dock transition are unreliable, and those are
    dropped upstream by is_status_report().

    ``attached`` stays in the payload as a separate fact, so the extension
    can tell "in the dock" from "out of the dock" without also losing the
    battery level.
    """
    s = state or {}
    return {
        "ok": ok,
        "last_seen": last_seen,
        "left": {
            "pct": s.get("left"),
            "attached": bool(s.get("attL")),
        },
        "right": {
            "pct": s.get("right"),
            "attached": bool(s.get("attR")),
        },
    }


def write_state(payload):
    """Atomically write the state file (only if it changed)."""
    global _last_payload
    text = json.dumps(payload, indent=1)
    if text == _last_payload:
        return
    _last_payload = text
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


_last_payload = None
_lock_fd = None
_stop = False

# Only treat stdin-EOF as "parent wants us to exit" when stdin actually
# is a pipe (extension-managed). A TTY or /dev/null (manual runs) must
# not kill the helper.
def _stdin_is_pipe():
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
    except OSError:
        return False


WATCH_STDIN = _stdin_is_pipe()


def _on_signal(*_a):
    global _stop
    _stop = True


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if not _acquire_lock():
        # Another instance is already running (e.g. it survived a shell
        # restart); the extension always spawns us, so just bow out.
        return 0

    state = None      # last parsed report values
    last_seen = 0.0

    while not _stop:
        raw = discover_raw_hidraw()
        if raw is None:
            write_state(snapshot(None, 0.0, ok=False))
            _wait_once(DISCOVERY_RETRY)
            continue

        try:
            fd = os.open(raw, os.O_RDONLY)
        except OSError:
            write_state(snapshot(None, 0.0, ok=False))
            _wait_once(DISCOVERY_RETRY)
            continue

        gone = False
        try:
            while not _stop:
                if _readable(fd, 1.0):
                    buf = _read_report(fd)
                    if buf is None:      # device gone / stream ended
                        gone = True
                        break
                    parsed = parse_report(buf)
                    if parsed is None:
                        continue
                    state = parsed
                    last_seen = time.time()
                    write_state(snapshot(state, last_seen))
                elif (state is not None and not _stop
                        and time.time() - last_seen > SILENCE_TIMEOUT):
                    # The stream has been quiet for a while, which is not
                    # something undocking does any more - the dock keeps
                    # reporting across a re-dock. Something is really wrong,
                    # so drop the last sample instead of showing a stale one.
                    write_state(snapshot(None, last_seen))
                    state = None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if gone:
            # Undocking re-enumerates the dock and the interface dies with
            # EIO. Keep the last state rather than clearing it: the re-scan
            # finds the new node in about a second, and wiping here would
            # make both rows blink out on every dock/undock. A dock that is
            # really gone is caught by the discovery check above, and a
            # stream that never comes back by SILENCE_TIMEOUT.
            _wait_once(1.0)   # let udev recreate nodes before re-scanning


def _readable(fd, timeout):
    watch = [fd] + ([sys.stdin] if WATCH_STDIN else [])
    r, _, _ = select.select(watch, [], [], timeout)
    return fd in r


def _read_report(fd):
    """Read one full 64-byte report; returns bytes or None on failure."""
    buf = bytearray()
    while len(buf) < REPORT_BYTES:
        watch = [fd] + ([sys.stdin] if WATCH_STDIN else [])
        r = select.select(watch, [], [], 5.0)[0]
        if WATCH_STDIN and sys.stdin in r:
            # stdin readable: either EOF (extension killed the pipe) or
            # the user piped something in. EOF means -> exit.
            if os.read(sys.stdin.fileno(), 1) == b"":
                raise SystemExit(0)
            continue
        if fd not in r:
            return None          # timeout -> caller re-checks state
        try:
            chunk = os.read(fd, REPORT_BYTES - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf[:REPORT_BYTES])


def _wait_once(seconds):
    """Sleep in small slices so signals are handled promptly, and exit as
    soon as the parent (extension) closes our stdin pipe — even when we are
    stuck in a retry loop (device missing or not readable), so a helper can
    never outlive its shell and poison the single-instance lock."""
    deadline = time.time() + seconds
    while not _stop and time.time() < deadline:
        if WATCH_STDIN:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if sys.stdin in r and os.read(sys.stdin.fileno(), 1) == b"":
                    raise SystemExit(0)
            except OSError:
                pass
        time.sleep(0.05)


if __name__ == "__main__":
    main()