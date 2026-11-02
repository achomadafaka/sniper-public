#!/usr/bin/env bash
# Watchdog for the 4663 curve-phase forward-measurement research loop. Cron calls this once a
# minute; the flock permits exactly one loop process.
#
# This process measures. It never signs, submits, or touches a wallet key. The loop it starts
# reads the funded ledger READ-ONLY and writes only its own research database under data/.
#
# IT RESTARTS ON A STALE HEARTBEAT, NOT ONLY ON AN ABSENT PROCESS -- same reasoning as
# memecoin_refusal_capture_watchdog.sh: a restart-if-gone watchdog cannot see a wedge, and the
# loop is quiet between polls by design, so "no log output" looks exactly like a hung poll. The
# loop stamps data/memecoin_curve_phase_watch.heartbeat at the top of every pass; this kills and
# relaunches when that stops advancing, on the WATCHDOG'S clock, never the loop's own counter.
#
# Bounded, not permanent: the loop writes data/memecoin_curve_phase_watch.done at 72h and exits;
# this watchdog checks that marker FIRST and refuses to relaunch once it exists.
set -u

ROOT=/home/bigpapi/Desktop/Sniper
LOG="$ROOT/logs/memecoin_curve_phase_watch.log"
BEAT="$ROOT/data/memecoin_curve_phase_watch.heartbeat"
DONE="$ROOT/data/memecoin_curve_phase_watch.done"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
MATCH="$ROOT/scripts/research/memecoin_curve_phase_watch_loop.py"

# Generous against the loop's 20s poll. A margin tight against the poll turns one slow provider
# call into a kill, and killing a healthy loop costs more than a late restart.
STALE_S="${MEMECOIN_CURVE_PHASE_WATCH_HEARTBEAT_MAX_AGE_S:-300}"

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"

log() { printf '%s watchdog %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# The 72h research window is over -- never relaunch past it.
[ -f "$DONE" ] && exit 0

# --- wedge check -------------------------------------------------------------------------------
# `ps -eo args | grep -F` and never `pgrep -f`: pgrep matches every command line, so a cron
# wrapper or an editor holding the path reads as the process itself
# (finding: runtime-repoint-hazards-2026-08-03).
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
# Single-instance is enforced by the loop's own acquire_run_lock ALONE. The watchdog must NOT
# flock the same lock file itself: the loop acquires that path internally, so a launcher-held
# flock would be inherited by the child and the loop would read its own parent as "another
# instance" and exit -- every launch self-blocking (reproduced live, 2026-08-28).
#
# LOGGED ONLY WHEN A START ACTUALLY SURVIVES. Cron runs this every minute, so on a healthy loop
# every launch exits immediately against the loop's internal lock -- an unconditional log line
# here would be ~1,440 noise entries a day on the one channel whose job is to make a real event
# visible. `... &` cannot tell us whether the launch stuck (the shell's status is just "did the
# fork succeed"), so compare the heartbeat's mtime before and after instead.
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
