#!/usr/bin/env bash
# HL alt run-up short paper ledger. Read-only public endpoints; no keys, orders, or funded path.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CODE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
export SNIPER_STATE_ROOT="$STATE_ROOT"
cd "$CODE_ROOT" || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=$STATE_ROOT/data/hl_altshort_paper.db
PIDFILE=$STATE_ROOT/data/hl_altshort_paper.pid
HEARTBEAT=$STATE_ROOT/data/hl_altshort_paper.heartbeat
FUNDING_CACHE=$STATE_ROOT/data/research/hl_full_history/funding
FUNDING_TAPE=$STATE_ROOT/data/hl_squeeze_tape.db
LOGFILE=$STATE_ROOT/.logs/hl_altshort_paper.log
WATCHDOG_LOG=$STATE_ROOT/.logs/hl_altshort_paper_watchdog.log
ARGS="--db-path $DB --heartbeat $HEARTBEAT --funding-cache-dir $FUNDING_CACHE --funding-tape $FUNDING_TAPE --clip-usd 40 --max-concurrent 5 --manage-seconds 60 --scan-seconds 600 --pace-seconds 0.35"
# MATCH is deliberately RELEASE-INDEPENDENT (no $CODE_ROOT); LAUNCH stays pinned.
# CODE_ROOT resolves through .runtime/hyperliquid/current, so a release-scoped match
# cannot see a worker started before a repoint: the watchdog then relaunches every
# minute, the runner's own lock refuses the duplicate, and it logs FAILED_TO_START
# forever while the deploy silently never takes effect (verified 2026-08-03).
# Keyed on the script path plus the absolute --db-path, so it stays scoped to THIS
# state root. hl_copy cannot use $ARGS here: that embeds a $CODE_ROOT manifest path.
MATCH="scripts/run_hl_altshort_paper.py --db-path $DB"
LAUNCH="$PY -u $CODE_ROOT/scripts/run_hl_altshort_paper.py $ARGS"

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

mkdir -p "$STATE_ROOT/.logs"
pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then exit 0; fi
existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then
  echo "$existing" > "$PIDFILE"
  log "reattached pid=$existing"
  exit 0
fi

setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
