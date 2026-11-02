#!/usr/bin/env bash
# Standalone exit-state materializer watchdog (Phase 2, 2026-07-23).
# Log-only, read-only to the money path; own process, own failure domain (NOT the funded runner).
# Gated: does NOTHING unless MEMECOIN_EXIT_STATE_ENABLED is truthy in .env — enabling is a STOP
# condition (block-time RPC spend). Not installed in cron by default; the operator wires it at enable.
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

# fail-safe gate: default OFF -> watchdog is a no-op (never spawn-exit churn on a disabled worker)
ENABLED=$(grep -E '^MEMECOIN_EXIT_STATE_ENABLED=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]')
case "$ENABLED" in 1|true|yes) ;; *) exit 0 ;; esac

mkdir -p .logs data

# single-flight: overlapping cron invocations must never both spawn (double worker = double RPC spend).
exec 9>data/memecoin_exit_state.watchdog.lock || exit 0
flock -n 9 || exit 0

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE=/home/bigpapi/Desktop/Sniper/data/memecoin_exit_state.pid
LOGFILE=.logs/memecoin_exit_state.log
WATCHDOG_LOG=.logs/memecoin_exit_state_watchdog.log
MATCH="scripts/memecoin_exit_state_worker.py"
LAUNCH="$PY -u scripts/memecoin_exit_state_worker.py"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

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


pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then exit 0; fi

existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then echo "$existing" > "$PIDFILE"; log "reattached pid=$existing"; exit 0; fi

# 9>&- is load-bearing: without it the worker inherits the single-flight lock and holds it for life,
# so every later cron tick fails `flock -n 9` and exits 0 before any check runs (measured 2026-08-12).
setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null 9>&- &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
