#!/usr/bin/env bash
# Pinned desk roster-history worker watchdog (cron, every minute). Same identity rules as the cluster
# watchdog (scripts/lib/desk_proc_match.sh); the worker runs nice 19 / ionice idle, writes
# <state>/data/desk/roster_history.db and roster_history.heartbeat (cohort C roster as-of history).
#
# Cron line (Alpha installs at merge):
#   * * * * * <pinned worktree>/scripts/memecoin_desk_roster_history_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_roster_history_watchdog.log 2>&1
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/roster_history_worker.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_roster_history_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_ROSTER_HISTORY_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
# identity and state root: scripts/lib/desk_proc_match.sh (a different root is not ours)
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk roster-history worker pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup nice -n 19 ionice -c 3 "$PY" "$WORKER" --state-dir "$STATE_ROOT/data/desk" >> "$STATE_ROOT/logs/memecoin_desk_roster_history.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
