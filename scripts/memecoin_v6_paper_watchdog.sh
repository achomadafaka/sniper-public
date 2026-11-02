#!/usr/bin/env bash
# Default-OFF, single-flight watchdog for the dedicated V6 live-paper worker.
# Isolated from the funded runner: unique lock/pid/log, and it NEVER restarts the funded runner.
# Not wired into any deployed/pinned runtime manifest — manual/cron opt-in only.
set -u
DEFAULT_STATE_ROOT=/home/bigpapi/Desktop/Sniper
SCRIPT=$(readlink -f "$0")
DEFAULT_CODE_ROOT=$(cd "$(dirname "$SCRIPT")/.." && pwd -P)
STATE_ROOT=${SNIPER_STATE_ROOT:-$DEFAULT_STATE_ROOT}
CODE_ROOT=${SNIPER_V6_CODE_ROOT:-$DEFAULT_CODE_ROOT}
cd "$CODE_ROOT" || exit 1
enabled=$(grep -E '^MEMECOIN_V6_PAPER_ENABLED=' "$STATE_ROOT/.env" 2>/dev/null \
  | tail -1 | cut -d= -f2 | tr -d '"' | tr '[:upper:]' '[:lower:]')
case "$enabled" in
  1|true|yes|on) ;;
  *) exit 0 ;;
esac

mkdir -p "$STATE_ROOT/.logs" "$STATE_ROOT/data"
exec 9>"$STATE_ROOT/data/memecoin_v6_paper.watchdog.lock" || exit 0
flock -n 9 || exit 0

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE="$STATE_ROOT/data/memecoin_v6_paper.pid"
LOGFILE="$STATE_ROOT/.logs/memecoin_v6_paper.log"
WATCHDOG_LOG="$STATE_ROOT/.logs/memecoin_v6_paper_watchdog.log"
# DISCOVERY is release-INDEPENDENT and LAUNCH is release-PINNED. A discovery match carrying
# $CODE_ROOT stops recognizing the running worker as soon as a deploy repoints `current`, so the next
# cron tick starts a SECOND worker. Cutting a worker over to a new release is the deploy's job.
MATCH="scripts/memecoin_v6_paper_worker.py"
WORKER="$CODE_ROOT/scripts/memecoin_v6_paper_worker.py"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

pid_matches_worker() {
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
    pid_matches_worker "$pid" && { echo "$pid"; break; }
  done
}

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_worker "$pid"; then exit 0; fi
existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then
  echo "$existing" > "$PIDFILE"
  log "reattached pid=$existing"
  exit 0
fi

# 9>&- is load-bearing: without it the worker inherits the single-flight lock and holds it for life,
# so every later cron tick fails `flock -n 9` and exits 0 before any check runs (measured 2026-08-12).
setsid nohup env SNIPER_STATE_ROOT="$STATE_ROOT" PYTHONPATH="$CODE_ROOT" \
  "$PY" -u "$WORKER" >>"$LOGFILE" 2>&1 </dev/null 9>&- &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
