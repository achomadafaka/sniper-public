#!/usr/bin/env bash
# Memecoin X source-graph watchdog. Keeps the DISPLAY-ONLY capture receiver (127.0.0.1:8787) alive
# and runs the correlator + signal-builder oneshots each tick (they process only new rows). Isolated
# store: data/memecoin_x.db. Reads sniper_tracker.db READ-ONLY for correlation. No trading imports or
# side effects; never touches wallets/positions/execution. Modeled on sniper_web_watchdog.sh.
#
# Capture itself is human-driven: it only produces data while the burner Chrome is open on x.com
# "Following" with the Tampermonkey userscript (scripts/memecoin_x_capture.user.js) running. This
# watchdog keeps the INGEST + correlation lane ready; it does NOT and cannot drive the browser.
set -u

ROOT=/home/bigpapi/Desktop/Sniper
cd "$ROOT" || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
XDB="$ROOT/data/memecoin_x.db"
FOMO_DB="$ROOT/sniper_tracker.db"
OFF="$ROOT/data/clock_offset.json"
RLOG="$ROOT/logs/memecoin_x_receiver.log"
WLOG="$ROOT/logs/memecoin_x_watchdog.log"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WLOG"; }
mkdir -p "$ROOT/logs" "$ROOT/data"

# 1) receiver alive on :8787? launch if not. Exact identity (scripts/lib/desk_proc_match.sh): a python
# whose argv[1] is the receiver, for this state root. `pgrep -f` matched any command line naming the
# file, so an open editor or a grep suppressed every restart.
PROC_ROOT="${X_WATCHDOG_PROC_ROOT:-/proc}"
source "$(dirname "$(readlink -f "$0")")/lib/desk_proc_match.sh"
if ! desk_proc_running "$PROC_ROOT" "$ROOT/scripts/run_memecoin_x_receiver.py" "$ROOT"; then
  log "receiver down; launching"
  nohup "$PY" "$ROOT/scripts/run_memecoin_x_receiver.py" --db "$XDB" --port 8787 >> "$RLOG" 2>&1 &
  log "receiver launched pid $!"
  sleep 3
fi

# 2) idempotent correlation + event build (only new observations/buys since last run).
"$PY" "$ROOT/scripts/run_memecoin_x_correlator.py" \
  --x-db "$XDB" --fomo-db "$FOMO_DB" --once --clock-offset-file "$OFF" >/dev/null 2>&1 \
  || log "correlator failed"
"$PY" "$ROOT/scripts/run_memecoin_x_signal_builder.py" \
  --db "$XDB" --once --clock-offset-file "$OFF" >/dev/null 2>&1 \
  || log "signal-builder failed"
