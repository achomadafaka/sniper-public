#!/usr/bin/env bash
# HL forward paper-follow watchdog. Public read-only data; paper only; no keys, no orders.
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite
PIDFILE=/home/bigpapi/Desktop/Sniper/data/hl_follow.pid
LOGFILE=.logs/hl_follow.log
WATCHDOG_LOG=.logs/hl_follow_watchdog.log
MATCH="run_hl_follow.py --db-path $DB"
LAUNCH="$PY -u scripts/run_hl_follow.py --db-path $DB --interval-seconds 180"

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

mkdir -p .logs

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then exit 0; fi

existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then echo "$existing" > "$PIDFILE"; log "reattached pid=$existing"; exit 0; fi

setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
