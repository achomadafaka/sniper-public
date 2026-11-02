#!/usr/bin/env bash
# Pinned desk offchain-heat collector watchdog (cron, every minute). Same shape as the cluster-gate
# watchdog: the release's PHYSICAL path (through .runtime/memecoin/current a logical path is the same
# string for every release), identity from /proc argv exactly (argv[0] a python, argv[1] the pinned
# worker), a superseded release's collector is stopped, state lives in SNIPER_STATE_ROOT. The worker's
# own flock makes a second start exit. MEMECOIN_DESK_OFFCHAIN_ENABLED=1 in the state root's .env starts
# it (default off: the worker exits at once).
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_offchain_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_offchain_watchdog.log 2>&1
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/offchain.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_offchain.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_OFFCHAIN_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
# identity and state root: scripts/lib/desk_proc_match.sh (a different root is not ours)
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk offchain collector pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup "$PY" "$WORKER" --loop >> "$STATE_ROOT/logs/memecoin_desk_offchain.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
