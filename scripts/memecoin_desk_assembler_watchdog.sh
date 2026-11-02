#!/usr/bin/env bash
# Pinned desk U assembler watchdog (cron, every minute). Same shape as
# memecoin_desk_judge_watchdog.sh: the release's real path, identity from /proc argv[1] exactly, a
# superseded release's process is stopped, state lives in SNIPER_STATE_ROOT. The assembler reads its
# knobs (MEMECOIN_DESK_ASSEMBLER_ENABLED, MEMECOIN_DESK_U_COHORT_ID, the research key) from the state
# root's .env in main(); not 1 or no cohort = it exits at once.
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_assembler_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_assembler_watchdog.log 2>&1
set -euo pipefail

# the release's REAL directory: through the .runtime/memecoin/current symlink a logical path is the
# same string for every release, and a superseded worker would never be stopped (Gamma FAIL a).
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/assembler.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_assembler.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_ASSEMBLER_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
# identity and state root: scripts/lib/desk_proc_match.sh (a different root is not ours)
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk assembler pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup "$PY" "$WORKER" --desk-db "$STATE_ROOT/data/memecoin_desk.db" --loop \
    >> "$STATE_ROOT/logs/memecoin_desk_assembler.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
