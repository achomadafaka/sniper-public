#!/usr/bin/env bash
# HL liquidation-cascade observation capture watchdog.
# Public market data only (trades WS + asset-ctx OI); forward-only; no keys, no orders.
# Runs from the MAIN worktree (cascade code is merged to main).
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite
PIDFILE=/home/bigpapi/Desktop/Sniper/data/cascade_capture.pid
LOGFILE=logs/cascade_capture.log
WATCHDOG_LOG=logs/cascade_capture_watchdog.log
MATCH="run_cascade_capture.py --db $DB"
LAUNCH="$PY -u scripts/run_cascade_capture.py --db $DB"
HEARTBEAT=/home/bigpapi/Desktop/Sniper/data/cascade_capture.heartbeat
STALE_SEC=300     # obs heartbeat older than this (past grace) => zombie: alive but not collecting
GRACE_SEC=180     # don't zombie-kill a freshly (re)started process before it can flush its first obs

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

hb_age() {  # seconds since the capture last flushed obs (wall clock; obs ts_ms is +7h exchange-skewed)
  local m; m=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
  echo $(( $(date +%s) - m ))
}

is_zombie() {  # pid alive but not flushing obs, and past its startup grace period
  local pid="${1:-}" age
  age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' '); age=${age:-0}
  [ "$age" -ge "$GRACE_SEC" ] && [ "$(hb_age)" -ge "$STALE_SEC" ]
}

pid_matches_runner() {
  local pid="${1:-}" cmd first
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  first=${cmd%% *}
  case "$(basename "$first")" in python|python3) ;; *) return 1 ;; esac
  printf '%s' "$cmd" | grep -Fq "$MATCH"
}

find_existing() {
  local pid
  pgrep -f "$MATCH" | while read -r pid; do
    [ "$pid" != "$$" ] || continue
    pid_matches_runner "$pid" && { echo "$pid"; break; }
  done
}

mkdir -p logs

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then
  if is_zombie "$pid"; then
    log "ZOMBIE pid=$pid alive but obs stale $(hb_age)s -> kill+restart"
    kill "$pid" 2>/dev/null; sleep 2; kill -9 "$pid" 2>/dev/null || true
  else
    exit 0
  fi
fi

existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then
  if is_zombie "$existing"; then
    log "ZOMBIE pid=$existing alive but obs stale -> kill+restart"
    kill "$existing" 2>/dev/null; sleep 2; kill -9 "$existing" 2>/dev/null || true
  else
    echo "$existing" > "$PIDFILE"; log "reattached pid=$existing"; exit 0
  fi
fi

setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
