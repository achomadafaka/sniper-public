#!/usr/bin/env bash
# Pinned desk X producer watchdog (cron, every minute). Same shape as
# memecoin_desk_judge_watchdog.sh: the release's real path, identity from /proc argv[1] exactly, a
# superseded release's process is stopped, state lives in SNIPER_STATE_ROOT. The producer reads
# MEMECOIN_DESK_X_PRODUCER_ENABLED from the state root's .env and J7_TOKEN_2 from its .j7.env in main();
# not 1 or no token = it exits at once. A refused token writes data/desk/x/auth_backoff.json and the
# producer itself exits before connecting until .j7.env's J7_TOKEN_2 changes or 15 min pass (Gamma M1).
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_x_producer_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_x_producer_watchdog.log 2>&1
set -euo pipefail

# the release's REAL directory: through the .runtime/memecoin/current symlink a logical path is the
# same string for every release, and a superseded worker would never be stopped (Gamma FAIL a).
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/x_producer.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_x_producer.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_X_PRODUCER_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
# identity and state root: scripts/lib/desk_proc_match.sh (a different root is not ours)
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
stopped=()
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk X producer pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" && stopped+=("$pid") || true
done

# never two sessions on J7_TOKEN_2 at once (the funded relay also uses the account): wait up to 10 s
# for a stopped producer to exit; if one is still alive, the next cron minute starts the release copy
for _ in $(seq 1 20); do
  left=0
  for pid in "${stopped[@]}"; do
    stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || continue      # gone
    state="${stat##*) }"; [[ "${state%% *}" == Z ]] && continue      # exited, not yet reaped
    left=1
  done
  [[ "$left" -eq 0 ]] && break
  sleep 0.5
done
[[ "$left" -eq 0 ]] || { echo "$(date -u +%FT%TZ) superseded desk X producer still exiting; start next minute" >&2; exit 0; }

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup "$PY" "$WORKER" \
    >> "$STATE_ROOT/logs/memecoin_desk_x_producer.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
