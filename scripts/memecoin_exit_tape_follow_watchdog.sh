#!/usr/bin/env bash
# Watchdog for the exit price tape follower (part B, spec 2026-09-30 §4a). Cron calls it once a
# minute. DEFAULT OFF: it exits unless .env has MEMECOIN_EXIT_TAPE_FOLLOW_ENABLED=1.
#
# The follower measures. It reads the funded ledger READ-ONLY, writes only the tape file, and never
# signs or touches a wallet key.
#
# Same rules as scripts/memecoin_refusal_capture_watchdog.sh, whose comments carry the reasons:
#   * release-pinned launch; a follower not running the current release is replaced;
#   * owned processes matched on the script argument AND their own SNIPER_STATE_ROOT, via ps -eww;
#   * a stale heartbeat (the watchdog's clock, not the follower's) kills a wedged follower;
#   * single instance is the follower's own run lock; this script never flocks it.
set -u

# Both overridable for tests/test_memecoin_watchdog_exit_tape_follow.py, which runs this only inside
# its own pid namespace; cron sets neither.
ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
LOG="$ROOT/logs/memecoin_exit_tape_follow.log"
BEAT="$ROOT/data/memecoin_exit_tape_follow.heartbeat"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SCRIPT_REL="scripts/memecoin_exit_tape_follow.py"
STALE_S="${MEMECOIN_EXIT_TAPE_FOLLOW_HEARTBEAT_MAX_AGE_S:-120}"

CURRENT="$(readlink -f "$ROOT/.runtime/memecoin/current" 2>/dev/null || true)"
MATCH="$CURRENT/$SCRIPT_REL"
if [ -z "$CURRENT" ] || [ ! -f "$MATCH" ]; then
    MATCH="$ROOT/$SCRIPT_REL"
    CURRENT=""
fi

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"
log() { printf '%s watchdog %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

ENABLED=$(grep -E '^MEMECOIN_EXIT_TAPE_FOLLOW_ENABLED=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
[ "${ENABLED:-0}" = "1" ] || exit 0

owned_pids() {
    ps -eww -o pid=,args= | while read -r pid rest; do
        script="${rest##* }"
        case "$script" in
            "$ROOT/$SCRIPT_REL") ;;
            "$ROOT"/.runtime/memecoin/releases/*/"$SCRIPT_REL") ;;
            *) continue ;;
        esac
        env_root=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^SNIPER_STATE_ROOT=//p')
        [ "$env_root" = "$ROOT" ] || continue
        printf '%s %s\n' "$pid" "$script"
    done
}
stop_owned() {
    pids="$(printf '%s\n' "$OWNED" | awk 'NF {print $1}')"
    kill -TERM $pids 2>/dev/null
    sleep 5
    kill -KILL $pids 2>/dev/null
}
OWNED="$(owned_pids)"
RUNNING=$(printf '%s\n' "$OWNED" | grep -c . || true)

if [ -n "$CURRENT" ] && [ "$RUNNING" -gt 0 ]; then
    CURRENT_N=$(printf '%s\n' "$OWNED" | grep -cF -- "$CURRENT/" || true)
    if [ "$CURRENT_N" -lt "$RUNNING" ]; then
        log "follower running from a release that is not $(basename "$CURRENT") — replacing it"
        stop_owned
        RUNNING=0
    fi
fi
if [ "$RUNNING" -gt 0 ] && [ -f "$BEAT" ]; then
    AGE=$(( $(date -u +%s) - $(stat -c %Y "$BEAT" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt "$STALE_S" ]; then
        log "heartbeat ${AGE}s stale (max ${STALE_S}s) — killing the wedged follower"
        stop_owned
        RUNNING=0
    fi
fi
[ "$RUNNING" -gt 0 ] && exit 0

BEFORE=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
setsid env PYTHONPATH="${CURRENT:-$ROOT}" SNIPER_STATE_ROOT="$ROOT" \
    "$PY" -u "$MATCH" >> "$LOG" 2>&1 &
sleep 5
AFTER=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
if [ "$AFTER" -gt "$BEFORE" ]; then
    log "started — heartbeat began advancing"
else
    log "start FAILED — no heartbeat within 5s of launch"
fi
exit 0
