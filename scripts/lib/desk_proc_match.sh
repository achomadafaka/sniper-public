# Shared process identity for the desk watchdogs (Gamma (A), Alpha 2026-10-21). Sourced, never run.
#
# A process is THIS state root's copy of a worker only when all three hold:
#   1. argv[0]'s basename starts with "python" (an interpreter, not an editor or a grep);
#   2. basename(argv[1]) == basename(WORKER) (the script argument itself, never a substring);
#   3. its state root is STATE_ROOT: SNIPER_STATE_ROOT from /proc/<pid>/environ, unset there = the
#      default root, compared after realpath -m. These are Lambda's crawler-watchdog semantics
#      (22240ee7c), lifted here so every desk watchdog agrees.
# An unreadable environ is not ours: never counted alive, never signalled.
# PROC_ROOT is the tests' seam: a fake process table is never signalled (its pids are real host pids).

DESK_DEFAULT_STATE_ROOT="${DESK_DEFAULT_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"

desk_proc_state_root() {   # $1 = /proc/<pid>; prints the process's state root; fails when unreadable
  local env_root
  env_root="$(tr '\0' '\n' < "$1/environ" 2>/dev/null | sed -n 's/^SNIPER_STATE_ROOT=//p' | tail -n 1)" \
    || return 1
  printf '%s' "${env_root:-$DESK_DEFAULT_STATE_ROOT}"
}

# desk_proc_scan PROC_ROOT WORKER STATE_ROOT
# Sets DESK_ALIVE=1 when WORKER itself runs for STATE_ROOT, and DESK_SUPERSEDED to the pids of
# other paths of the same script (an older release) running for STATE_ROOT.
desk_proc_scan() {
  local proc_root="$1" worker="$2" want proc pid root arg0 arg1
  want="$(realpath -m -- "$3")"
  DESK_ALIVE=0
  DESK_SUPERSEDED=()
  for proc in "$proc_root"/[0-9]*; do
    pid="${proc##*/}"
    [[ -r "$proc/cmdline" ]] || continue
    local argv=()
    while IFS= read -r -d '' arg0; do argv+=("$arg0"); done < "$proc/cmdline" 2>/dev/null || true
    arg0="${argv[0]:-}" arg1="${argv[1]:-}"     # kernel threads have an empty cmdline
    [[ "${arg0##*/}" == python* && "${arg1##*/}" == "${worker##*/}" ]] || continue
    root="$(set -o pipefail; desk_proc_state_root "$proc")" || continue
    [[ "$(realpath -m -- "$root")" == "$want" ]] || continue
    if [[ "$arg1" == "$worker" ]]; then
      DESK_ALIVE=1
    else
      DESK_SUPERSEDED+=("$pid")
    fi
  done
}

# desk_proc_term PROC_ROOT PID: TERM a real process; returns 0 only when it signalled.
desk_proc_term() {
  [[ "$1" == /proc ]] || return 1
  kill -TERM "$2" 2>/dev/null
}

# desk_proc_running PROC_ROOT WORKER STATE_ROOT: 0 when any path of WORKER's script runs for
# STATE_ROOT (WORKER itself or another release's copy), for callers that only start, never stop.
desk_proc_running() {
  desk_proc_scan "$@"
  [[ "$DESK_ALIVE" -eq 1 || "${#DESK_SUPERSEDED[@]}" -gt 0 ]]
}
