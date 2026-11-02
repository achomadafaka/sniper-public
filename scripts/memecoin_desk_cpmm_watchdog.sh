#!/usr/bin/env bash
# Install only after review; invoke through .runtime/memecoin/current. No activation here.
set -euo pipefail
CODE_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
export SNIPER_STATE_ROOT="$STATE_ROOT"
WORKER="$CODE_ROOT/scripts/memecoin_desk_cpmm_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
source "$CODE_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "${DESK_CPMM_PROC_ROOT:-/proc}" "$WORKER" "$STATE_ROOT"
[[ "$DESK_ALIVE" -eq 0 ]] || exit 0
# A release handoff must stop the old process explicitly; this watchdog never kills a service.
[[ "${#DESK_SUPERSEDED[@]}" -eq 0 ]] || exit 1
mkdir -p "$STATE_ROOT/data/desk/cpmm" "$STATE_ROOT/logs"
cd "$STATE_ROOT"
nohup flock -n "$STATE_ROOT/data/desk/cpmm/worker.lock" "$PY" "$WORKER" \
  >> "$STATE_ROOT/logs/memecoin_desk_cpmm.log" 2>&1 &
