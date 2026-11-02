#!/usr/bin/env bash
# Default-OFF, single-flight watchdog for the Solana cohort stream (spec 2026-10-09 Solana pre-signal feed, step 1). Runs only while MEMECOIN_SOL_COHORT_HELIUS_URL is set in .env; the process itself refuses a shared or keyless key.
set -u
STATE_ROOT=/home/bigpapi/Desktop/Sniper
CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
export SNIPER_STATE_ROOT="$STATE_ROOT"
cd "$STATE_ROOT" || exit 1
enabled=$(grep -E '^MEMECOIN_SOL_COHORT_HELIUS_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')
case "$enabled" in
  "") exit 0 ;;
  *) ;;
esac

mkdir -p .logs data
# A key the process refused (keyless, option C's, shared) leaves this marker; relaunching every minute
# would only log FAILED_TO_START. Editing .env makes it older than .env and the next tick tries again.
if [ -f data/memecoin_sol_cohort.refused ] && [ data/memecoin_sol_cohort.refused -nt .env ]; then
  exit 0
fi
exec 9>data/memecoin_sol_cohort.watchdog.lock || exit 0
flock -n 9 || exit 0

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE=data/memecoin_sol_cohort.pid
LOGFILE=.logs/memecoin_sol_cohort.log
WATCHDOG_LOG=.logs/memecoin_sol_cohort_watchdog.log
# DISCOVERY is release-INDEPENDENT and LAUNCH is release-PINNED, and they must stay that way.
# A discovery match carrying $CODE_ROOT stops recognizing the running worker the moment a deploy
# repoints `current`, so the next cron tick starts a SECOND worker on the same tape database.
# Cutting a worker over to a new release is the deploy's job, not this watchdog's.
MATCH="scripts/memecoin_sol_cohort_stream.py"
LAUNCH=("$PY" -u "$CODE_ROOT/scripts/memecoin_sol_cohort_stream.py")

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

# 9>&- is load-bearing. Without it the worker INHERITS the single-flight lock and holds it for its
# whole life, so every later cron tick fails `flock -n 9` and exits 0 before any check runs. That
# silently retires the watchdog: a hung-but-alive worker is never restarted. Measured 2026-08-12 —
# the lock file was held by the worker, not by any watchdog.
setsid nohup "${LAUNCH[@]}" >>"$LOGFILE" 2>&1 </dev/null 9>&- &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
