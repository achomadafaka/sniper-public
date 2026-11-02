#!/usr/bin/env bash
# Hyperliquid microstructure paper watchdog.
# Safe: public market-data runner only, paper DB only, no private keys or orders.
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE=data/hl_microstructure_paper.pid
LOGFILE=.logs/hl_microstructure_paper.log
WATCHDOG_LOG=.logs/hl_microstructure_paper_watchdog.log
MATCH="run_hl_microstructure_paper.py --db-path data/hl_microstructure_paper.sqlite"
LAUNCH="$PY -u scripts/run_hl_microstructure_paper.py --db-path data/hl_microstructure_paper.sqlite --auto-select-assets --max-assets 6 --decision-interval-ms 1000 --notional-usdc 25 --fee-rate 0.00045 --max-spread-bps 12"

log() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"
}

pid_matches_runner() {
  local pid="${1:-}"
  local cmd first
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  first=${cmd%% *}
  case "$(basename "$first")" in
    python|python3) ;;
    *) return 1 ;;
  esac
  printf '%s' "$cmd" | grep -Fq "$MATCH"
}

find_existing() {
  local pid
  pgrep -f "$MATCH" | while read -r pid; do
    [ "$pid" != "$$" ] || continue
    pid_matches_runner "$pid" && { echo "$pid"; break; }
  done
}

mkdir -p data .logs

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then
  exit 0
fi

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
if [ -n "${real:-}" ]; then
  log "RESTARTED pid=$real"
  exit 0
fi

log "FAILED_TO_START"
exit 1
