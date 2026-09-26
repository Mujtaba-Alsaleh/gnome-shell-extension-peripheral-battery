#!/usr/bin/env bash
# =============================================================================
# install.sh — Peripheral Battery Status installer with pre-flight check
#
# 1. Runs READ-ONLY compatibility tests (GNOME 50, python3, Legion Go raw
#    interface present, the 0x04 battery report actually streams).
# 2. Only if ALL tests pass: installs the extension (if not already) and
#    creates the udev rule granting hidraw access.
#
# No system modification happens before the checks have passed.
# Usage:  ./install.sh [--check] [--force]
#   --check   run only the compatibility tests, make no changes
#   --force   overwrite the extension files even if already installed
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VID="17ef"
PID="61eb"
RULE_FILE="/etc/udev/rules.d/99-legion-go-battery.rules"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-8}"          # seconds to await a battery report
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHECK_ONLY=false
FORCE_EXT=false
for a in "$@"; do
    case "$a" in
        --check) CHECK_ONLY=true ;;
        --force) FORCE_EXT=true ;;
        *) echo "unknown argument: $a" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
C_RESET='\033[0m'; C_GREEN='\033[32m'; C_RED='\033[31m'; C_YEL='\033[33m'
C_CYA='\033[36m'; C_BOLD='\033[1m'
ok()   { echo -e "  ${C_GREEN}✔${C_RESET} $*"; }
warn() { echo -e "  ${C_YEL}⚠${C_RESET} $*"; }
fail() { echo -e "  ${C_RED}✘${C_RESET} $*" >&2; exit 1; }
phase(){ echo; echo -e "${C_BOLD}${C_CYA}== $* ==${C_RESET}"; }

# The user whose seat the extension/udev rule should target.
if [[ $EUID -eq 0 ]]; then
    TARGET_USER="${SUDO_USER:-}"
    if [[ -z "$TARGET_USER" ]]; then
        TARGET_USER="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')"
        [[ -n "$TARGET_USER" ]] || fail "running as root without a target user; run as your user instead."
    fi
else
    TARGET_USER="${USER:-$(id -un)}"
fi
# The extension UUID suffix is the current user (matches metadata.json,
# which is templated with the same placeholder during install).
UUID="peripheral-battery-status@${TARGET_USER}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
EXTDIR="$TARGET_HOME/.local/share/gnome-shell/extensions/$UUID"

# Run a command as the target (non-root) user.
run_as_user() {
    if [[ $EUID -eq 0 ]]; then
        su -s /bin/bash "$TARGET_USER" -c "$1"
    else
        bash -c "$1"
    fi
}

# ---- the hidraw stream probe (read-only; exit codes matter) -------------
#  0 = battery report found          1 = timeout / read error
#  2 = no Legion device present      3 = present but no permission
PROBE_TMP="$(mktemp /tmp/legion-go-probe.XXXXXX.py)"
SERVICE_TMP="$(mktemp /tmp/legion-go-service.XXXXXX)"
trap 'rm -f "$PROBE_TMP" "$SERVICE_TMP"' EXIT
cat > "$PROBE_TMP" <<'PY'
import os, select, sys, time
timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
node = None
try:
    nodes = os.listdir('/sys/class/hidraw')
except OSError:
    print('NO_DEVICE'); sys.exit(2)
for n in nodes:
    dev = os.path.realpath(f'/sys/class/hidraw/{n}/device')
    hid = os.path.basename(dev)
    if not hid.startswith('0003:17EF:'):
        continue
    try:
        with open(f'/sys/bus/hid/devices/{hid}/uevent') as f:
            ue = dict(l.split('=', 1) for l in f.read().splitlines() if '=' in l)
    except OSError:
        continue
    if 'legion' in ue.get('HID_NAME', '').lower() and ue.get('HID_PHYS', '').endswith('/input2'):
        node = f'/dev/{n}'
        break
if not node:
    print('NO_DEVICE'); sys.exit(2)
try:
    fd = os.open(node, os.O_RDONLY)
except PermissionError:
    print('NO_PERM', node); sys.exit(3)
except OSError as e:
    print('OPEN_ERR', node, e); sys.exit(1)
deadline = time.time() + timeout
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 1.0)
    if not r:
        continue
    try:
        buf = os.read(fd, 64)
    except OSError as e:
        print('READ_ERR', node, e); sys.exit(1)
    if len(buf) >= 64 and buf[0] in (0x04, 0x74):
        print(f'OK {node} id=0x{buf[0]:02x} left={buf[5]} right={buf[7]} '
              f'attachedL={1 - ((buf[12] >> 7) & 1)} attachedR={1 - ((buf[13] >> 7) & 1)}')
        sys.exit(0)
print('TIMEOUT', node); sys.exit(1)
PY

# ---------------------------------------------------------------------------
# PHASE 1 — compatibility tests (strictly read-only)
# ---------------------------------------------------------------------------
phase "1/2  Compatibility check (no changes made)"

echo -e "  target user : ${C_BOLD}$TARGET_USER${C_RESET}"
echo -e "  extension   : $EXTDIR"

# 1. python3 ---------------------------------------------------------------
command -v python3 >/dev/null || fail "python3 not found — cannot run the helper."
ok "python3 found ($(python3 --version 2>&1))"

# 2. GNOME Shell >= 50 ------------------------------------------------------
gsv="$(gnome-shell --version 2>/dev/null || true)"
major="$(sed -n 's/.*GNOME Shell \([0-9][0-9]*\).*/\1/p' <<<"$gsv")"
if [[ -z "$major" ]]; then
    fail "could not determine GNOME Shell version ('gnome-shell --version' -> '$gsv')."
fi
if (( major < 50 )); then
    fail "GNOME Shell $major detected, but this extension requires GNOME Shell 50."
fi
ok "shell version: $gsv (>= 50 required)"

# 3. install sources present ------------------------------------------------
for f in extension.js metadata.json tools/legion_go_battery.py; do
    [[ -f "$SCRIPT_DIR/$f" ]] || fail "missing '$f' — run this script from the repository root."
done
ok "install sources present"

# 4. Legion Go raw interface present ---------------------------------------
probe() { # $1 = interpreter prefix ("" or "sudo")
    local out rc
    if [[ -n "$1" ]]; then
        out="$($1 python3 "$PROBE_TMP" "$PROBE_TIMEOUT" 2>&1)" && rc=0 || rc=$?
    else
        out="$(python3 "$PROBE_TMP" "$PROBE_TIMEOUT" 2>&1)" && rc=0 || rc=$?
    fi
    PROBE_OUT="$out"; PROBE_RC="$rc"
}

probe ""   # try as the current user first
case "$PROBE_RC" in
    0)
        ok "raw interface readable as user: $PROBE_OUT"
        ;;
    3)
        warn "raw interface not readable as the current user (expected before the udev rule)"
        # escalate read-only probe with sudo to verify the STREAM works at all
        probe "sudo"
        case "$PROBE_RC" in
            0)
                ok "battery report confirmed (read via sudo): $PROBE_OUT"
                ;;
            2)
                fail "Legion Go raw interface not found — are the controllers docked?"
                ;;
            1)
                case "$PROBE_OUT" in
                    TIMEOUT*)
                        fail "interface found, but no battery report within ${PROBE_TIMEOUT}s. "
                            "On your system the daemon that drives the controllers (hhd / "
                            "inputplumber) must be running so the report stream is active."
                        ;;
                    *) fail "could not read the raw interface: $PROBE_OUT" ;;
                esac
                ;;
            *)
                fail "unexpected probe result: $PROBE_OUT"
                ;;
        esac
        ;;
    2)
        fail "Legion Go raw interface not found — are the controllers docked?"
        ;;
    1)
        case "$PROBE_OUT" in
            TIMEOUT*)
                fail "interface found, but no battery report within ${PROBE_TIMEOUT}s. "
                    "The daemon that drives the controllers (hhd / inputplumber) must be "
                    "running so the report stream is active."
                ;;
            *) fail "could not read the raw interface: $PROBE_OUT" ;;
        esac
        ;;
    *)
        fail "unexpected probe result: $PROBE_OUT"
        ;;
esac

# informational: which kernel driver is bound
node="$(awk '{print $2}' <<<"$PROBE_OUT")"   # e.g. /dev/hidraw15
drv="$(basename "$(readlink -f "/sys/class/hidraw/$(basename "$node")/device/driver" 2>/dev/null || true)" 2>/dev/null || true)"
[[ -n "$drv" ]] && echo -e "  ${C_CYA}info:${C_RESET} kernel driver bound: $drv"

# 5. extension target is writable ------------------------------------------
if [[ -d "$EXTDIR" ]]; then
    [[ -w "$EXTDIR" ]] || [[ $EUID -eq 0 ]] || fail "extension dir exists but is not writable: $EXTDIR"
elif [[ -d "$(dirname "$EXTDIR")" ]]; then
    [[ -w "$(dirname "$EXTDIR")" ]] || [[ $EUID -eq 0 ]] || \
        fail "cannot install into $EXTDIR (parent not writable)"
fi
ok "extension install location writable"

# 6. udev rule state --------------------------------------------------------
if [[ -f "$RULE_FILE" ]]; then
    if grep -qi "$VID" "$RULE_FILE"; then
        ok "udev rule already present: $RULE_FILE (kept as-is)"
        RULE_EXISTS=true
    else
        warn "a udev rule exists at $RULE_FILE but does not cover $VID — leaving it untouched"
        RULE_EXISTS=true
    fi
else
    RULE_EXISTS=false
fi

phase "compatibility: ${C_GREEN}ALL CHECKS PASSED${C_RESET}"
if $CHECK_ONLY; then
    echo -e "  (--check mode: no changes were made)"
    exit 0
fi

# ---------------------------------------------------------------------------
# PHASE 2 — install (only reached if every check above passed)
# ---------------------------------------------------------------------------
phase "2/2  Installing"

# 7. udev rule --------------------------------------------------------------
# OWNER+MODE covers seat-less sessions (SSH, embedded harnesss); the uaccess
# tag additionally grants the active seat user on normal desktops. Belt &
# suspenders, no group edits, no relogin required.
if ! $RULE_EXISTS; then
    read -r -d '' RULE <<EOF || true
# Lenovo Legion Go detachable controller batteries (Peripheral Battery Status):
# make the raw HID interface readable so a userspace helper can read the
# battery report (id 0x04). Vendor-wide: the rail enumerates both the docked
# (61eb) and detached/wireless (61ed) presentations with this vendor ID.
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="$VID", OWNER="$TARGET_USER", MODE="0660", TAG+="uaccess"
EOF
    echo -e "  creating $RULE_FILE"
    echo "$RULE" | sudo tee "$RULE_FILE" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw
    ok "udev rule created + reloaded + devices re-triggered"
else
    ok "udev rule left untouched"
fi

# 8. extension files --------------------------------------------------------
if [[ -f "$EXTDIR/extension.js" ]] && ! $FORCE_EXT; then
    ok "extension already installed (use --force to refresh)"
else
    echo "  copying files into $EXTDIR (metadata templated for user '$TARGET_USER')"
    if [[ $EUID -eq 0 ]]; then
        install -d -m 0755 "$EXTDIR/tools"
        install -m 0644 "$SCRIPT_DIR/extension.js" "$SCRIPT_DIR/README.md" "$EXTDIR/"
        sed "s/@your_username/@${TARGET_USER}/g" "$SCRIPT_DIR/metadata.json" > "$EXTDIR/metadata.json"
        install -m 0755 "$SCRIPT_DIR/tools/legion_go_battery.py" "$EXTDIR/tools/"
        chown -R "$TARGET_USER:" "$EXTDIR"
    else
        mkdir -p "$EXTDIR/tools"
        cp "$SCRIPT_DIR/extension.js" "$SCRIPT_DIR/README.md" "$EXTDIR/"
        sed "s/@your_username/@${TARGET_USER}/g" "$SCRIPT_DIR/metadata.json" > "$EXTDIR/metadata.json"
        cp "$SCRIPT_DIR/tools/legion_go_battery.py" "$EXTDIR/tools/"
        chmod +x "$EXTDIR/tools/legion_go_battery.py"
    fi
    ok "extension files installed (uuid: $UUID)"
fi

# 8b. systemd user helper service ------------------------------------------
# The helper runs as a systemd --user unit (not spawned by the shell, which
# avoids Gio.Subprocess quirks and survives shell restarts). Installed even
# if the extension files were already present.
UNIT_NAME="peripheral-battery-legion.service"
UNIT_DIR="$TARGET_HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=Legion Go controller battery helper (Peripheral Battery Status)
# Reads the docked controllers' raw HID report and writes
# ~/.cache/peripheral-battery-status/legion-go.json for the extension.
# Runs outside the graphical session so it survives shell restarts.

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$EXTDIR/tools/legion_go_battery.py"
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
if [[ -d "$UNIT_DIR" ]]; then
    [[ -w "$UNIT_DIR" ]] || [[ $EUID -eq 0 ]] || fail "systemd user dir not writable: $UNIT_DIR"
else
    if [[ $EUID -eq 0 ]]; then
        install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" "$UNIT_DIR"
    else
        mkdir -p "$UNIT_DIR"
    fi
fi
install -m 0644 "$SERVICE_TMP" "$UNIT_FILE"
if [[ $EUID -eq 0 ]]; then chown "$TARGET_USER:" "$UNIT_FILE"; fi
if run_as_user "systemctl --user daemon-reload && systemctl --user enable --now '$UNIT_NAME'"; then
    ok "helper service installed, enabled and started ($UNIT_NAME)"
else
    warn "service installed but could not be enabled (is a user session active?). "
         "Enable it later with: systemctl --user enable --now $UNIT_NAME"
fi

# 9. post-install verify: user-level read must now work ---------------------
probe ""
if [[ "$PROBE_RC" -eq 0 ]]; then
    ok "raw interface now readable as $TARGET_USER: $PROBE_OUT"
else
    warn "udev rule installed but the device is not yet user-readable "
         "(may need a replug, or your session has no seat). Try: "
         "sudo udevadm trigger --subsystem-match=hidraw"
fi

# 10. end-to-end smoke test: helper service + fresh state file ---------------
# (A manual one-shot helper run would now just lose the flock to the running
# service, so the smoke test checks the service and its state file instead.)
STATE_JSON="$TARGET_HOME/.cache/peripheral-battery-status/legion-go.json"
if run_as_user "systemctl --user is-active --quiet '$UNIT_NAME'"; then
    echo "  helper service is active; waiting for a state write…"
    for _ in 1 2 3 4 5 6; do
        if [[ -s "$STATE_JSON" ]] && (( $(date +%s) - $(stat -c %Y "$STATE_JSON" 2>/dev/null || echo 0) < 60 )); then
            break
        fi
        sleep 1
    done
    if [[ -s "$STATE_JSON" ]]; then
        ok "helper service writes state; state file:"
        sed 's/^/    /' "$STATE_JSON"
    else
        warn "service is active but wrote no state file yet — "
             "check 'systemctl --user status $UNIT_NAME'"
    fi
else
    echo "  service inactive — running a one-shot helper smoke test…"
    rm -f "$STATE_JSON"
    run_as_user "timeout 5 python3 '$EXTDIR/tools/legion_go_battery.py' & HP=\$!; sleep 4; kill \$HP 2>/dev/null; wait \$HP 2>/dev/null"
    if [[ -f "$STATE_JSON" ]]; then
        ok "helper works; state file:"
        sed 's/^/    /' "$STATE_JSON"
    else
        warn "helper ran but wrote no state file — see ~/.cache/peripheral-battery-status/"
    fi
fi

# 11. enable the extension --------------------------------------------------
if run_as_user "gnome-extensions enable '$UUID' >/dev/null 2>&1"; then
    ok "extension enabled ($UUID)"
else
    warn "could not enable right now (no shell session?) — "
         "run 'gnome-extensions enable $UUID' after logging in"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
phase "done"
echo -e "  ${C_BOLD}Log out and back in${C_RESET} so GNOME Shell loads the extension."
echo "  It should then show \"Legion Go Left / Right\" when the controllers are docked."
echo
echo "  Uninstall:   make uninstall          (removes the extension, not the udev rule)"
echo "  Rule file:   $RULE_FILE  (remove it with sudo to revert access)"