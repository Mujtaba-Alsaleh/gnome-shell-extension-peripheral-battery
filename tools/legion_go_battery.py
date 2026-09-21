#!/usr/bin/env python3
"""
Legion Go controller battery helper for "Peripheral Battery Status".

Reads the raw HID interface of the docked Lenovo Legion Go controllers
(the stock hid-lenovo-go kernel driver passes report id 0x04 through to
hidraw) and publishes the LEFT/RIGHT controller battery + attachment
state to a small state file that the GNOME Shell extension watches via
inotify.

Report layout (report id 0x04, 64 bytes; hhd documents the same layout
under report id 0x74):
    byte  5         left controller battery (0-100)
    byte  7         right controller battery (0-100)
    byte 10 bit 7   left controller radio-connected
    byte 11 bit 7   right controller radio-connected
    byte 12 bit 7   left controller attached   (bit = 0 -> attached)
    byte 13 bit 7   right controller attached  (bit = 0 -> attached)

The state file is only rewritten when the values change. When the stream
goes quiet or the interface dies (controller undock tears the interface
down on this unit -- no flag-flip report is emitted, and the final report
can carry corrupt values), a "detached" state is written that drops the
last battery levels; the extension then hides those rows until the
controllers are docked again.

State file format (JSON):
    {
      "ok": true,
      "last_seen": <epoch seconds>,
      "left":  {"pct": 100, "attached": true,  "connected": false},
      "right": {"pct": 100, "attached": true,  "connected": false}
    }

The helper exits when its stdin reaches EOF, so the extension can manage
its lifetime by keeping a pipe open (and closing it to stop the helper).
"""

import json
import os
import select
import signal
import stat
import sys
import time

REPORT_BYTES = 64
ACCEPTED_IDS = (0x04, 0x74)          # 0x04 on this unit; 0x74 is hhd's id
SILENCE_TIMEOUT = 15.0               # seconds without a report -> detached
DISCOVERY_RETRY = 3.0                # seconds between device re-scans

STATE_DIR = os.path.join(os.path.expanduser("~/.cache"),
                         "peripheral-battery-status")
STATE_PATH = os.path.join(STATE_DIR, "legion-go.json")


def discover_raw_hidraw():
    """Return /dev/hidrawN of the Legion raw (config) interface, or None."""
    try:
        nodes = os.listdir("/sys/class/hidraw")
    except OSError:
        return None

    for node in nodes:
        dev = os.path.realpath(os.path.join("/sys/class/hidraw", node,
                                            "device"))
        hid = os.path.basename(dev)
        if not hid.startswith("0003:17EF:"):
            continue
        try:
            with open(f"/sys/bus/hid/devices/{hid}/uevent") as f:
                ue = dict(line.split("=", 1) for line in f.read().splitlines()
                          if "=" in line)
        except OSError:
            continue
        name = ue.get("HID_NAME", "")
        phys = ue.get("HID_PHYS", "")
        if "legion" in name.lower() and phys.endswith("/input2"):
            return os.path.join("/dev", node)
    return None


def parse_report(buf):
    """Extract battery/attachment state from a raw report, or None."""
    if len(buf) < REPORT_BYTES or buf[0] not in ACCEPTED_IDS:
        return None
    return {
        "left":  min(100, buf[5]),
        "right": min(100, buf[7]),
        "connL": (buf[10] >> 7) & 1,
        "connR": (buf[11] >> 7) & 1,
        # attachment flag is inverted: bit=0 -> attached
        "attL":  1 - ((buf[12] >> 7) & 1),
        "attR":  1 - ((buf[13] >> 7) & 1),
    }


def snapshot(state, last_seen, ok=True):
    """Fold the raw parse into the public JSON state."""
    s = state or {}
    return {
        "ok": ok,
        "last_seen": last_seen,
        "left": {
            "pct": s.get("left"),
            "attached": bool(s.get("attL")),
            "connected": bool(s.get("connL")),
        },
        "right": {
            "pct": s.get("right"),
            "attached": bool(s.get("attR")),
            "connected": bool(s.get("connR")),
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
                    # Stream went quiet (controllers undocked/off). Drop the
                    # last sample: on this unit undocking tears the interface
                    # down and the final report can carry corrupt values.
                    write_state(snapshot(None, last_seen))
                    state = None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if gone:
            # Undock / re-enumeration killed the interface before any
            # flag-flip report arrived. Forget the last sample.
            write_state(snapshot(None, last_seen))
            state = None
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
    """Sleep in small slices so signals are handled promptly."""
    deadline = time.time() + seconds
    while not _stop and time.time() < deadline:
        time.sleep(0.1)


if __name__ == "__main__":
    main()