#!/usr/bin/env bash
# Watchdog for the signal copy-quote worker. Cron calls this once a minute; the flock permits
# exactly one worker process. DEFAULT OFF — it exits unless MEMECOIN_COPY_QUOTE_ENABLED=1.
#
# Cron cannot drive the capture itself: the schedule needs 5-second resolution. So cron's only
# job is to notice the worker is gone and start it again.
#
# This process measures. It never signs, submits, or touches a wallet key, and it never writes
# to the funded ledger.
set -u

ROOT=/home/bigpapi/Desktop/Sniper
LOG="$ROOT/logs/memecoin_copy_quote.log"
LOCK="$ROOT/data/memecoin_copy_quote.worker.lock"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"

# Read the flag from .env without exporting the whole file into this shell.
ENABLED=$(grep -E '^MEMECOIN_COPY_QUOTE_ENABLED=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
[ "${ENABLED:-0}" = "1" ] || exit 0

# Single-instance is enforced by the lock ALONE, deliberately.
#
# A `pgrep -f memecoin_copy_quote_worker.py` guard was here and was removed: pgrep matches the
# pattern against every command line, so a cron wrapper, an editor, or another agent's shell that
# merely CONTAINS the worker's path reads as "already running" and the watchdog declines to start
# it — forever, silently. That false positive was reproduced within a minute of writing it. flock
# has no such mode: it holds for exactly the lifetime of the process that owns it.
#
# `flock` exits 1 when the lock is taken, which is the normal quiet case, so the start is only
# logged when the lock was actually acquired.
if setsid flock -n "$LOCK" "$PY" -u "$ROOT/scripts/memecoin_copy_quote_worker.py" >> "$LOG" 2>&1 &
then
    printf '%s watchdog start attempted\n' "$(date -u +%FT%TZ)" >> "$LOG"
fi
exit 0
