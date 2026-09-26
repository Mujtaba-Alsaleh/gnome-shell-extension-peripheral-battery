#!/usr/bin/env python3
"""
Legion Go controller raw-report capture (diagnostic tool).

Logs every 0x04/0x74 report from the docked controllers' raw HID interface
to a file so a detach/re-dock cycle can be inspected afterwards. Use it to
verify what the controllers actually report while detached and at the exact
moment of re-docking:

    python3 tools/legion_go_capture.py                       # default output
    python3 tools/legion_go_capture.py /tmp/opencode/dock.log

Output lines:
    <ISO timestamp>  left=<byte5> right=<byte7> connL=<byte10 bit7>
    connR=<byte11 bit7> attL=<1-(byte12 bit7)> attR=<1-(byte13 bit7)>

Run it, detach the controllers for a while, re-dock them, then press
Ctrl+C. A summary of the distinct states seen is printed on exit.

Opening the interface read-only is safe alongside hhd/inputplumber: hidraw
is multi-reader, and this script never writes to the device.
"""

import os
import select
import signal
import sys
import time
from datetime import datetime, timezone

REPORT_BYTES = 64
ACCEPTED_IDS = (0x04, 0x74)          # 0x04 on this unit; 0x74 is hhd's id
DEFAULT_OUT = "/tmp/opencode/legion_dock_capture.log"


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


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT

    raw = discover_raw_hidraw()
    if raw is None:
        print("No Legion Go raw (input2) interface found — controllers "
              "docked?", file=sys.stderr)
        return 1
    try:
        fd = os.open(raw, os.O_RDONLY)
    except OSError as e:
        print(f"cannot open {raw}: {e} (udev rule applied?)", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    log = open(out_path, "w")
    stop = False

    def _stop(*_a):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"capturing {raw} -> {out_path}  (Ctrl+C to stop)")
    seen = {}
    start = time.time()
    n = 0
    try:
        while not stop:
            buf = bytearray()
            while len(buf) < REPORT_BYTES and not stop:
                r, _, _ = select.select([fd], [], [], 5.0)
                if not r:
                    break
                try:
                    chunk = os.read(fd, REPORT_BYTES - len(buf))
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
            if len(buf) < REPORT_BYTES or buf[0] not in ACCEPTED_IDS:
                continue

            n += 1
            L, R = buf[5], buf[7]
            cL = (buf[10] >> 7) & 1
            cR = (buf[11] >> 7) & 1
            aL = 1 - ((buf[12] >> 7) & 1)
            aR = 1 - ((buf[13] >> 7) & 1)
            seen[(L, R, aL, aR, cL, cR)] = seen.get((L, R, aL, aR, cL, cR), 0) + 1
            log.write(f"{datetime.now(timezone.utc).isoformat()} "
                      f"left={L} right={R} connL={cL} connR={cR} "
                      f"attL={aL} attR={aR}\n")
            log.flush()   # durable even if killed mid-capture
    finally:
        os.close(fd)
        log.close()

    print(f"\ncaptured {n} reports over {time.time() - start:.1f}s "
          f"({n / max(time.time() - start, 0.01):.1f}/s); distinct states:")
    for k, count in sorted(seen.items()):
        L, R, aL, aR, cL, cR = k
        print(f"  left={L:3d} right={R:3d} attL={aL} attR={aR} "
              f"connL={cL} connR={cR}  x{count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())