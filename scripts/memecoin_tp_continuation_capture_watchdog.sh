#!/usr/bin/env bash
# Watchdog for the TP continuation capture worker. Cron calls this once a minute; the flock
# permits exactly one loop process. DEFAULT OFF — it exits unless MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED=1.
#
# This process measures. It never signs, submits, or touches a wallet key. The loop it starts
# reads the funded ledger READ-ONLY and writes only its own database.
#
# IT RESTARTS ON A STALE HEARTBEAT, NOT ONLY ON AN ABSENT PROCESS. A restart-if-gone watchdog
# cannot see a wedge, and the capture loop is silent by design — at roughly four capturable
# refusals an hour, "no log output" is the normal healthy case, so a hung poll looks exactly like
# a quiet one. The loop stamps data/memecoin_tp_continuation.heartbeat at the top of every pass;
# this kills and relaunches when that stops advancing. The clock is the WATCHDOG'S, never the
# loop's own counter: a liveness signal written by the thing it monitors is blind to that thing
# dying (finding: liveness-counters-need-external-clocks, hit three times).
set -u

ROOT=/home/bigpapi/Desktop/Sniper
LOG="$ROOT/logs/memecoin_tp_continuation_capture.log"
LOCK="$ROOT/data/memecoin_tp_continuation.worker.lock"
BEAT="$ROOT/data/memecoin_tp_continuation.heartbeat"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
MATCH="$ROOT/scripts/memecoin_tp_continuation_capture_loop.py"

# Generous against MEMECOIN_CAPTURE_POLL_S. A margin that is tight against the poll turns one slow
# provider call into a kill, and killing a healthy loop costs more than a late restart.
STALE_S="${MEMECOIN_TP_CONTINUATION_HEARTBEAT_MAX_AGE_S:-300}"

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"

log() { printf '%s watchdog %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# Read the flag from .env without exporting the whole file into this shell.
ENABLED=$(grep -E '^MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
[ "${ENABLED:-0}" = "1" ] || exit 0

# --- wedge check -------------------------------------------------------------------------------
# Only meaningful while a loop is actually running. `ps -eo args | grep -F` and never `pgrep -f`:
# pgrep matches every command line, so a cron wrapper or an editor holding the path reads as the
# process itself (finding: runtime-repoint-hazards-2026-08-03).
RUNNING=$(ps -eo args | grep -F "$MATCH" | grep -v grep | wc -l)
if [ "$RUNNING" -gt 0 ] && [ -f "$BEAT" ]; then
    AGE=$(( $(date -u +%s) - $(stat -c %Y "$BEAT" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt "$STALE_S" ]; then
        log "heartbeat ${AGE}s stale (max ${STALE_S}s) — killing the wedged loop"
        pkill -TERM -f "$MATCH" 2>/dev/null
        sleep 5
        pkill -KILL -f "$MATCH" 2>/dev/null
        RUNNING=0
    fi
fi

# --- start -------------------------------------------------------------------------------------
# Single-instance is enforced by the lock ALONE. A pgrep guard was removed from the sibling
# watchdog because its false positive made the watchdog decline to start the worker forever,
# silently. flock holds for exactly the lifetime of the process that owns it.
#
# The watchdog must NOT flock this file itself: the loop acquires the same path internally, so a
# launcher-held flock is inherited by the child's environment and the loop reads its own parent
# as "another instance" and exits — every launch self-blocks (live, 2026-08-28 17:16, twice).
# The loop's internal acquire_run_lock is the single point of enforcement, as the comment above
# always claimed; the launcher just starts it.
# LOGGED ONLY WHEN A START ACTUALLY SURVIVES. Cron runs this every minute, so on a healthy loop
# every one of those launches exits immediately against the loop's internal lock — an unconditional
# line here is ~1,440 entries a day reporting a start that never happened, on the one channel whose
# job is to make a real event visible.
#
# `... &` cannot tell us: the shell's status is "did the fork succeed", which is always true. So
# note the heartbeat's mtime, launch, and let the new loop write one. Only a launch that moves a
# heartbeat that was ABSENT OR STALE is a start; a launch that loses the lock leaves it untouched,
# because the holder advances it on its own clock either way.
BEFORE=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
NOW_S=$(date -u +%s)
WAS_DEAD=$(( BEFORE == 0 || NOW_S - BEFORE > STALE_S ))
setsid "$PY" -u "$MATCH" >> "$LOG" 2>&1 &
[ "$WAS_DEAD" -eq 1 ] || exit 0
sleep 5
AFTER=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
if [ "$AFTER" -gt "$BEFORE" ]; then
    log "started — heartbeat began advancing"
else
    log "start FAILED — no heartbeat within 5s of launch"
fi
exit 0
