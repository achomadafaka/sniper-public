#!/usr/bin/env bash
# Pinned desk narrative-guesser worker watchdog (cron, every minute), J7-ONLY launcher: the worker is started with --no-model, so the Opus arm cannot
# start from here whatever the .env knob says (a model launcher is a separate operator decision).
#
# Identity is NARRATIVE-SPECIFIC and stricter than scripts/lib/desk_proc_match.sh (the shared helper is not weakened; it only supplies the state-root rule). A process counts as ALIVE only if
#   1. python, argv[1] == this pinned WORKER path, SNIPER_STATE_ROOT == STATE_ROOT (shared rule),
#   2. argv carries --no-model (a same-path worker WITHOUT it could spend with the knob on: it is never alive, and it is sent TERM),
#   3. argv's --state-dir resolves to STATE_ROOT/data/desk (a worker serving another directory does not serve this state). A RELATIVE --state-dir is resolved against that
#      process's own /proc/<pid>/cwd; an unreadable cwd or a missing --state-dir is "unknown" = not ours.
# NOTHING is signalled before state ownership (3) is proven: a model-capable same-path worker or another release's copy is sent TERM only if it actually serves STATE_ROOT/data/desk;
# one serving a foreign directory is neither counted nor touched.
#
# MODE (env DESK_NARRATIVE_GUESSER_MODE, default j7_only = everything above). MODE=model (G4b, Alpha 2026-10-28) inverts rule 2: the worker is launched WITHOUT --no-model, so the model arm runs
# whenever MEMECOIN_DESK_NARRATIVE_GUESSER_MODEL=1 in the funded .env (default OFF: the worker then does J7 baseline only and spends nothing); a --no-model copy serving this state dir is
# superseded (TERM). Only ONE mode's cron line may exist (the two would stop each other's worker every minute). Model mode also pages Telegram when the heartbeat is stale > 120 s and on recovery.
#
# Cron line (intended, Alpha installs):
#   * * * * * <pinned worktree>/scripts/memecoin_desk_narrative_guesser_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_narrative_guesser_watchdog.log 2>&1
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/narrative_guesser_worker.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_narrative_guesser_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
MODE="${DESK_NARRATIVE_GUESSER_MODE:-j7_only}"
case "$MODE" in j7_only | model) ;; *) echo "unknown DESK_NARRATIVE_GUESSER_MODE=$MODE" >&2; exit 64 ;; esac

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_NARRATIVE_GUESSER_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"

# narrative_scan: sets ALIVE (0/1), STOP (pids to TERM: model-capable same-path workers and other releases' copies)
# resolve_sdir PROC SDIR: prints the absolute, normalised --state-dir of that process; fails (unknown) for an empty value or a relative one whose cwd cannot be read
resolve_sdir() {
  local proc="$1" s="$2" cwd
  [[ -n "$s" ]] || return 1
  if [[ "$s" == /* ]]; then
    realpath -m -- "$s"
  else
    cwd="$(readlink "$proc/cwd" 2>/dev/null)" || return 1
    [[ -n "$cwd" ]] || return 1
    realpath -m -- "$cwd/$s"
  fi
}

# narrative_scan: sets ALIVE (0/1), STOP (pids to TERM: model-capable same-path workers and other releases' copies, ONLY when they serve this state dir)
narrative_scan() {
  local proc pid root arg0 arg1 i nomodel sdir resolved want_state want_dir
  want_state="$(realpath -m -- "$STATE_ROOT")"
  want_dir="$(realpath -m -- "$STATE_ROOT/data/desk")"
  ALIVE=0
  STOP=()
  for proc in "$PROC_ROOT"/[0-9]*; do
    pid="${proc##*/}"
    [[ -r "$proc/cmdline" ]] || continue
    local argv=()
    while IFS= read -r -d '' arg0; do argv+=("$arg0"); done < "$proc/cmdline" 2>/dev/null || true
    arg0="${argv[0]:-}" arg1="${argv[1]:-}"
    [[ "${arg0##*/}" == python* && "${arg1##*/}" == "${WORKER##*/}" ]] || continue
    root="$(set -o pipefail; desk_proc_state_root "$proc")" || continue
    [[ "$(realpath -m -- "$root")" == "$want_state" ]] || continue
    nomodel=0
    sdir=""
    for ((i = 2; i < ${#argv[@]}; i++)); do
      case "${argv[$i]}" in
        --no-model) nomodel=1 ;;
        --state-dir) sdir="${argv[$((i + 1))]:-}" ;;
        --state-dir=*) sdir="${argv[$i]#--state-dir=}" ;;
      esac
    done
    resolved="$(resolve_sdir "$proc" "$sdir")" || continue          # unknown state dir: not ours, never counted, never signalled
    [[ "$resolved" == "$want_dir" ]] || continue                    # serves another directory: ignored (ownership is proven BEFORE any signal)
    if [[ "$arg1" != "$WORKER" ]]; then
      STOP+=("$pid")                                               # another release's copy serving THIS state dir
    elif [[ "$MODE" == "model" ]]; then
      if [[ "$nomodel" -eq 1 ]]; then
        STOP+=("$pid")                                             # a J7-only copy serving this state dir is superseded by the model-capable launcher
      else
        ALIVE=1
      fi
    elif [[ "$nomodel" -eq 0 ]]; then
      STOP+=("$pid")                                               # model-capable same-path worker serving this state dir
    else
      ALIVE=1
    fi
  done
}

STARTED=""
narrative_scan
for pid in "${STOP[@]}"; do
  echo "$(date -u +%FT%TZ) stopping non-J7-only or superseded narrative-guesser worker pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done

if [[ "$ALIVE" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  if [[ "$MODE" == "model" ]]; then
    nohup nice -n 19 ionice -c 3 "$PY" "$WORKER" --state-dir "$STATE_ROOT/data/desk" >> "$STATE_ROOT/logs/memecoin_desk_narrative_guesser.log" 2>&1 &
  else
    nohup nice -n 19 ionice -c 3 "$PY" "$WORKER" --state-dir "$STATE_ROOT/data/desk" --no-model >> "$STATE_ROOT/logs/memecoin_desk_narrative_guesser.log" 2>&1 &
  fi
  echo "$!" > "$PID_FILE"
  STARTED=1
fi
if [[ "$MODE" == "model" ]]; then
  nice -n 19 "$PY" "$REPO_ROOT/scripts/memecoin_desk_narrative_guesser_page.py" --state-dir "$STATE_ROOT/data/desk" ${STARTED:+--started-this-run} >> "$STATE_ROOT/logs/memecoin_desk_narrative_guesser_watchdog.log" 2>&1 || true
fi
