#!/usr/bin/env bash
# Watchdog for the 4663 fast lane M0 feed shadow (spec 2026-10-01-memecoin-4663-fast-lane-design.md
# §6). Cron calls it once a minute. DEFAULT OFF: it exits unless .env has MEMECOIN_RH_FAST_LANE=shadow.
#
# The shadow measures, in its OWN process (never inside the funded runner, Gamma MEDIUM). It reads
# the funded ledger READ-ONLY, writes only its own SQLite file, and never signs or touches a key.
#
# Same rules as scripts/memecoin_refusal_capture_watchdog.sh, whose comments carry the reasons:
#   * release-pinned launch; a shadow not running the current release is replaced;
#   * owned processes matched on the script argument AND their own SNIPER_STATE_ROOT, via ps -eww;
#   * a stale heartbeat (the watchdog's clock, not the shadow's) kills a wedged shadow;
#   * single instance is the shadow's own run lock; this script never flocks it.
set -u

# Both overridable for tests/test_memecoin_watchdog_rh_feed_shadow.py, which runs this only inside
# its own pid namespace; cron sets neither.
ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
LOG="$ROOT/logs/memecoin_rh_feed_shadow.log"
BEAT="$ROOT/data/memecoin_rh_feed_shadow.heartbeat"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SCRIPT_REL="scripts/memecoin_rh_feed_shadow_loop.py"
STALE_S="${MEMECOIN_RH_FEED_SHADOW_HEARTBEAT_MAX_AGE_S:-120}"

CURRENT="$(readlink -f "$ROOT/.runtime/memecoin/current" 2>/dev/null || true)"
MATCH="$CURRENT/$SCRIPT_REL"
if [ -z "$CURRENT" ] || [ ! -f "$MATCH" ]; then
    MATCH="$ROOT/$SCRIPT_REL"
    CURRENT=""
fi

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"
log() { printf '%s watchdog %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }
SHYFT_PROFILE=0
PROFILE=$(grep -E '^MEMECOIN_SOLANA_INFRA_PROVIDER=' "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'"' ')
[ "${PROFILE:-}" != shyft ] || SHYFT_PROFILE=1
if [ "$SHYFT_PROFILE" -eq 1 ] && [ "${MEMECOIN_RH_FEED_WATCHDOG_LOCK_HELD:-0}" != 1 ]; then
    exec 9> "$ROOT/data/memecoin_rh_feed_shadow.watchdog.lock"
    flock -n 9 || exit 0
fi


owned_pids() {
    if [ "$SHYFT_PROFILE" -eq 1 ]; then
        PYTHONSAFEPATH=1 PYTHONPATH="$CURRENT" "$PY" -c \
            "from scripts.memecoin_runtime_verify import _owned_script_rows; [print(pid,path) for pid,path in _owned_script_rows('$ROOT','$SCRIPT_REL')]"
        return
    fi
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
    if [ "$SHYFT_PROFILE" -eq 1 ]; then
        record=$(mktemp "$ROOT/data/.rh-stop.XXXXXX") || return 1
        if ! "$PY" "$CURRENT/scripts/memecoin_runtime_verify.py" --code-root "$CURRENT" \
            --state-root "$ROOT" --process-record-script "$SCRIPT_REL" > "$record" \
            || ! "$PY" "$CURRENT/scripts/memecoin_runtime_verify.py" --code-root "$CURRENT" \
                --state-root "$ROOT" --stop-process-record "$record"; then
            rm -f "$record"
            log "exact old RH process did not stop; replacement held"
            return 1
        fi
        rm -f "$record"
        return 0
    fi
    pids="$(printf '%s\n' "$OWNED" | awk 'NF {print $1}')"
    kill -TERM $pids 2>/dev/null
    sleep 5
    kill -KILL $pids 2>/dev/null
    return 0
}
OWNED="$(owned_pids)" || {
    log "exact RH process discovery failed; replacement held"
    exit 1
}
RUNNING=$(printf '%s\n' "$OWNED" | grep -c . || true)

# THE SWITCH IS ALSO THE ROLLBACK (Gamma LOW): off means no shadow, so a running one is stopped,
# not merely left unrelaunched. The loop reads .env only at startup.
MODE=$(grep -E '^MEMECOIN_RH_FAST_LANE=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ' | tr 'A-Z' 'a-z')
if [ "${MODE:-}" != "shadow" ] && [ "${MODE:-}" != "runner_shadow" ] && [ "${MODE:-}" != "live" ]; then
    if [ "$RUNNING" -gt 0 ]; then
        log "MEMECOIN_RH_FAST_LANE is not shadow, runner_shadow or live — stopping the running shadow"
        stop_owned || exit 1
    fi
    exit 0
fi

if [ -n "$CURRENT" ] && [ "$RUNNING" -gt 0 ]; then
    CURRENT_N=$(printf '%s\n' "$OWNED" | grep -cF -- "$CURRENT/" || true)
    if [ "$CURRENT_N" -lt "$RUNNING" ]; then
        log "shadow running from a release that is not $(basename "$CURRENT") — replacing it"
        stop_owned || exit 1
        RUNNING=0
    fi
fi
if [ "$RUNNING" -gt 0 ] && [ -f "$BEAT" ]; then
    AGE=$(( $(date -u +%s) - $(stat -c %Y "$BEAT" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt "$STALE_S" ]; then
        log "heartbeat ${AGE}s stale (max ${STALE_S}s) — killing the wedged shadow"
        stop_owned || exit 1
        RUNNING=0
    fi
fi
[ "$RUNNING" -gt 0 ] && exit 0

BEFORE=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
setsid env PYTHONPATH="${CURRENT:-$ROOT}" SNIPER_STATE_ROOT="$ROOT" \
    "$PY" -u "$MATCH" 9>&- >> "$LOG" 2>&1 &
sleep 5
AFTER=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
if [ "$AFTER" -gt "$BEFORE" ]; then
    log "started — heartbeat began advancing"
else
    log "start FAILED — no heartbeat within 5s of launch"
fi
exit 0
