#!/bin/sh
# Diagnose and recover a KiroCrew gateway that will not start after an in-place
# upgrade, on macOS / Linux. POSIX twin of scripts/recover-gateway.ps1.
#
# The failure this repairs
# ------------------------
# An upgrade replaces the installed bytes but does not stop the gateway the
# previous version left running. That old gateway keeps the process-lifetime
# advisory flock on "$KIROCREW_HOME/gateway.lock" (kiro_crew.gateway_lock) and
# keeps its dashboard port bound, so the fresh gateway cannot take the singleton
# lock, never restarts the MCP gateway daemon, and every pooled MCP stub connect
# fails ("connect failed: ... No such file or directory" for the socket) —
# logged to "$KIROCREW_HOME/logs/stub_fallback.jsonl". The app appears not to
# start / has no working tools.
#
# Because the orphaned gateway may still answer HTTP, the backend's own
# port-reclaim treats it as a HEALTHY_PEER and refuses to reclaim, so a plain
# relaunch does not fix it. This script closes that gap for the operator.
#
# Safety:
#   * Read-only by default; pass --fix to make changes.
#   * State files are copied to "$KIROCREW_HOME/backups/gateway-recovery-<ts>"
#     before removal, so a fix is reversible.
#   * Only a KiroCrew gateway is stopped, by its whole process group, and a
#     gateway that answers HTTP is left alone unless --force is given.
#
# Usage:
#   scripts/recover-gateway.sh                 # diagnose only
#   scripts/recover-gateway.sh --fix           # stop superseded gateway, clear state
#   scripts/recover-gateway.sh --fix --force   # also stop an HTTP-answering gateway
set -eu

FIX=0
FORCE=0
KIROCREW_HOME_ARG=""
for arg in "$@"; do
    case "$arg" in
        --fix) FIX=1 ;;
        --force) FORCE=1 ;;
        --home=*) KIROCREW_HOME_ARG="${arg#--home=}" ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

CREW_HOME="${KIROCREW_HOME_ARG:-${KIROCREW_HOME:-$HOME/.kiro/crew}}"
if [ ! -d "$CREW_HOME" ]; then
    echo "[!] KIROCREW_HOME not found: $CREW_HOME" >&2
    exit 1
fi

echo "== KiroCrew gateway recovery =="
echo "  -> Data home: $CREW_HOME"
echo "  -> Mode: $( [ "$FIX" = 1 ] && echo FIX || echo diagnose-only )$( [ "$FORCE" = 1 ] && echo ' +force' )"

RUN_DIR="$CREW_HOME/run"
LOCK_FILE="$CREW_HOME/gateway.lock"
STUB_LOG="$CREW_HOME/logs/stub_fallback.jsonl"

# --- Which ports did gateways advertise? -----------------------------------
PORTS=""
if [ -d "$RUN_DIR" ]; then
    for f in "$RUN_DIR"/gateway-*.pid; do
        [ -e "$f" ] || continue
        base=$(basename "$f" .pid)          # gateway-<port>
        port=${base#gateway-}
        case "$port" in (*[!0-9]*) continue ;; esac
        PORTS="$PORTS $port"
    done
fi
[ -n "$PORTS" ] || PORTS="5476"

# lsof-based port owner (trusted path is the caller's responsibility here).
port_owner_pid() {
    lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -n1
}
http_alive() {
    # Return 0 if 127.0.0.1:$1 returns an HTTP status line within 3s.
    if command -v curl >/dev/null 2>&1; then
        curl -s -o /dev/null -m 3 "http://127.0.0.1:$1/" && return 0 || return 1
    fi
    # Fallback: /dev/tcp
    ( exec 3<>"/dev/tcp/127.0.0.1/$1" 2>/dev/null &&
      printf 'GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' >&3 &&
      head -c5 <&3 | grep -q '^HTTP/' ) 2>/dev/null
}

echo ""
echo "== 1. Locating the gateway that owns this home =="
TARGET_PIDS=""
HEALTHY_ANY=0
for port in $PORTS; do
    owner=$(port_owner_pid "$port" || true)
    if [ -z "$owner" ]; then echo "  -> Port $port: no listener."; continue; fi
    if http_alive "$port"; then alive=yes; HEALTHY_ANY=1; else alive=no; fi
    cmd=$(ps -o command= -p "$owner" 2>/dev/null || true)
    is_kirocrew=no
    case "$cmd" in *kiro_crew*|*kirocrew*) is_kirocrew=yes ;; esac
    echo "  -> Port $port: pid $owner http=$alive kirocrew=$is_kirocrew"
    echo "        cmd: $cmd"
    if [ "$is_kirocrew" = yes ]; then
        if [ "$alive" = no ] || [ "$FORCE" = 1 ]; then
            TARGET_PIDS="$TARGET_PIDS $owner"
        fi
    fi
done

echo ""
echo "== 2. MCP stub-connect health =="
if [ -f "$STUB_LOG" ]; then
    if tail -n 20 "$STUB_LOG" 2>/dev/null | grep -q 'connect failed'; then
        echo "  [warn] Recent MCP connect failures present — the symptom this repairs."
    else
        echo "  [ok]   No recent connect failures in the tail."
    fi
else
    echo "  -> No stub_fallback.jsonl yet."
fi

echo ""
echo "== 3. Verdict =="
# Trim leading spaces
TARGET_PIDS=$(echo "$TARGET_PIDS" | xargs 2>/dev/null || true)
if [ -z "$TARGET_PIDS" ]; then
    if [ "$HEALTHY_ANY" = 1 ]; then
        echo "  [ok]   A gateway is serving and none is flagged superseded."
        echo "         If the app still misbehaves, confirm the wanted gateway and re-run with --force."
    else
        echo "  -> No stale gateway found. Check $CREW_HOME/gateway.log."
    fi
    [ "$FIX" = 1 ] || exit 0
fi
for p in $TARGET_PIDS; do echo "  [!]    Will stop pid $p (and its process group)"; done

if [ "$FIX" != 1 ]; then
    echo ""
    echo "  -> Diagnose-only. Re-run with --fix to apply."
    exit 0
fi

# --- 4. Backup -------------------------------------------------------------
echo ""
echo "== 4. Backing up state (reversible) =="
TS=$(date +%Y%m%d_%H%M%S)
BACKUP="$CREW_HOME/backups/gateway-recovery-$TS"
mkdir -p "$BACKUP"
for f in gateway.lock kiro_pids.txt kiro_session_pids.txt open_slots.json app_backends.pids.json mcp-gateway/gateway.sock.lock; do
    [ -f "$CREW_HOME/$f" ] && cp -p "$CREW_HOME/$f" "$BACKUP/" 2>/dev/null || true
done
[ -d "$RUN_DIR" ] && cp -pR "$RUN_DIR" "$BACKUP/run" 2>/dev/null || true
echo "  [ok]   Backup: $BACKUP"

# --- 5. Stop the superseded gateway group(s) -------------------------------
echo ""
echo "== 5. Stopping superseded gateway process group(s) =="
for p in $TARGET_PIDS; do
    if ! kill -0 "$p" 2>/dev/null; then echo "  -> pid $p already gone."; continue; fi
    # Signal the process group (gateway is started with start_new_session), so
    # its kiro-cli / MCP children go too. Fall back to the bare pid.
    pgid=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ' || true)
    echo "  -> TERM pid $p (pgid ${pgid:-n/a})"
    if [ -n "$pgid" ]; then kill -TERM "-$pgid" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
    else kill -TERM "$p" 2>/dev/null || true; fi
    sleep 2
    if kill -0 "$p" 2>/dev/null; then
        echo "  -> KILL pid $p"
        if [ -n "$pgid" ]; then kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$p" 2>/dev/null || true
        else kill -KILL "$p" 2>/dev/null || true; fi
        sleep 1
    fi
    if kill -0 "$p" 2>/dev/null; then echo "  [!] pid $p survived."; else echo "  [ok] pid $p stopped."; fi
done

# --- 6. Clear stale state --------------------------------------------------
echo ""
echo "== 6. Clearing stale lock/pid/run state =="
for f in gateway.lock kiro_pids.txt kiro_session_pids.txt open_slots.json app_backends.pids.json mcp-gateway/gateway.sock.lock; do
    if [ -f "$CREW_HOME/$f" ]; then rm -f "$CREW_HOME/$f" && echo "  [ok]   removed $f"; fi
done
if [ -d "$RUN_DIR" ]; then rm -f "$RUN_DIR"/gateway-* 2>/dev/null || true; echo "  [ok]   cleared run/gateway-* markers"; fi

echo ""
echo "== Done =="
echo "  [ok]   Recovery complete. Restart KiroCrew normally."
echo "  [ok]   Prior state backed up at: $BACKUP"
