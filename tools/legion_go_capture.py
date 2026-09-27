#!/usr/bin/env python3
"""
Legion Go controller raw-report capture (diagnostic tool).

Logs every status report from the dock's raw HID interface to a file so a
detach/re-dock cycle can be inspected afterwards. Use it to verify what the
controllers actually report while out of their rails and at the exact moment
of re-docking:

    python3 tools/legion_go_capture.py                       # default output
    python3 tools/legion_go_capture.py /tmp/opencode/dock.log

Output lines:
    <ISO timestamp>  left=<byte5> right=<byte7> attL=<1-(byte12 bit0)>
    attR=<1-(byte13 bit0)>

Run it, detach the controllers for a while, re-dock them, then press
Ctrl+C. A summary of the distinct states seen is printed on exit, together
with how many non-status frames were skipped: the endpoint carries button
traffic as well, and those frames are not battery readings.

Opening the interface read-only is safe alongside hhd/inputplumber: hidraw
is multi-reader, and this script never writes to the device.
"""

import os
import select
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from legion_go_battery import (  # noqa: E402  - the decode lives in one place
    REPORT_BYTES,
    discover_raw_hidraw,
    is_status_report,
)

DEFAULT_OUT = "/tmp/opencode/legion_dock_capture.log"


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT

    raw = discover_raw_hidraw()
    if raw is None:
        print("No Legion Go raw (vendor usage page) interface found — is "
              "this a Legion Go with hid-lenovo-go loaded?", file=sys.stderr)
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
    skipped = 0
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
            if len(buf) < REPORT_BYTES:
                continue
            if not is_status_report(buf):
                skipped += 1
                continue

            n += 1
            L, R = buf[5], buf[7]
            aL = 1 - (buf[12] & 1)
            aR = 1 - (buf[13] & 1)
            seen[(L, R, aL, aR)] = seen.get((L, R, aL, aR), 0) + 1
            log.write(f"{datetime.now(timezone.utc).isoformat()} "
                      f"left={L} right={R} attL={aL} attR={aR}\n")
            log.flush()   # durable even if killed mid-capture
    finally:
        os.close(fd)
        log.close()

    print(f"\ncaptured {n} reports over {time.time() - start:.1f}s "
          f"({n / max(time.time() - start, 0.01):.1f}/s), skipped {skipped} "
          f"non-status frames; distinct states:")
    for k, count in sorted(seen.items()):
        L, R, aL, aR = k
        print(f"  left={L:3d} right={R:3d} attL={aL} attR={aR}  x{count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())