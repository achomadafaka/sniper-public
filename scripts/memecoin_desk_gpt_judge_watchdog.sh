#!/usr/bin/env bash
# Pinned GPT blind second JUDGE shadow seat watchdog (cron, every minute). Same shape as
# memecoin_desk_attention_watchdog.sh: the release's real path, identity from /proc argv[1] exactly, a superseded
# release's process is stopped, state lives in SNIPER_STATE_ROOT. MEMECOIN_DESK_GPT_JUDGE_ENABLED must be 1 (the
# cron line's environment, else the last assignment in <state root>/.env); anything else is the KILL SWITCH: no
# start, and an alive seat is TERMed. The seat pauses itself on a tool event / error rate / rejected auth
# write-back; a paused seat exits 3 and this watchdog would restart it every minute, where it refuses and exits again
# (data/desk/gpt_judge.db seat_state), so the loop is cheap and loud only through the seat's own page.
# It never runs two codex calls at once: that is codex_slot (data/desk/codex_call.lock), shared with the CHECKER cron.
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_gpt_judge_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_gpt_judge_watchdog.log 2>&1
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/gpt_judge_shadow.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_gpt_judge_shadow.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_UNIVERSE_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE

enabled="${MEMECOIN_DESK_GPT_JUDGE_ENABLED:-}"
if [[ -z "$enabled" && -r "$STATE_ROOT/.env" ]]; then
  enabled="$(sed -n 's/^[[:space:]]*MEMECOIN_DESK_GPT_JUDGE_ENABLED=[[:space:]]*//p' "$STATE_ROOT/.env" | tail -n 1 | tr -d "\"' \r")"
fi
if [[ "$enabled" != "1" ]]; then
  # kill switch: stop every copy of the seat for this state root, start nothing
  for pid in "${DESK_SUPERSEDED[@]}"; do
    echo "$(date -u +%FT%TZ) GPT judge seat disabled; stopping pid=$pid" >&2
    desk_proc_term "$PROC_ROOT" "$pid" || true
  done
  if [[ "$alive" -eq 1 && -r "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && echo "$(date -u +%FT%TZ) GPT judge seat disabled; stopping pid=$pid" >&2 && desk_proc_term "$PROC_ROOT" "$pid" || true
  fi
  exit 0
fi

stopped=()
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded GPT judge seat pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" && stopped+=("$pid") || true
done

# never two writers of gpt_judge.db at once: wait up to 10 s for a stopped copy to exit; if one is still alive, the
# next cron minute starts the release copy
left=0
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
[[ "$left" -eq 0 ]] || { echo "$(date -u +%FT%TZ) superseded GPT judge seat still exiting; start next minute" >&2; exit 0; }

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup nice -n 10 "$PY" "$WORKER" \
    >> "$STATE_ROOT/logs/desk_gpt_judge_shadow.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
