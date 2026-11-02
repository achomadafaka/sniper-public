#!/usr/bin/env bash
# Pinned desk cost-pricing worker watchdog (cron, every minute). Same shape as
# memecoin_desk_assembler_watchdog.sh: the release's real path, identity from /proc argv[1] exactly, a
# superseded release's process is stopped, state lives in SNIPER_STATE_ROOT. The worker prices unpriced
# desk execution costs off every fill path (receipt via the research Helius key, one Coinbase SOL-USD
# candle); it reads MEMECOIN_DESK_COST_OWNER (the funded PUBLIC address) and MEMECOIN_RESEARCH_HELIUS_RPC_URLS
# from the state root's .env and refuses a funded/read/tape/watcher endpoint. Kill: remove the cron line.
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_cost_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_cost_watchdog.log 2>&1
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/cost_worker.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_cost_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_COST_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk cost worker pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup "$PY" "$WORKER" --ledger-db "$STATE_ROOT/sniper_tracker.db" --from-env --loop \
    >> "$STATE_ROOT/logs/memecoin_desk_cost_worker.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
