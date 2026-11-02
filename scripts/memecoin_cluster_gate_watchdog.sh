#!/usr/bin/env bash
# Pinned cluster-gate worker watchdog.
#
# Two hazards this guards against, both found in the 2026-08-22 audit:
#
#  1. STALE WORKER AFTER A PINNED UPDATE. The aliveness check used to grep for the CURRENT
#     release's absolute worker path. After a deploy that path changes, so a worker still
#     running from the PREVIOUS release no longer matched, the watchdog started a second
#     one, and the old release kept serving admission decisions with stale code. Detection
#     is now by script BASENAME across all processes, and any worker whose path is not the
#     pinned one is stopped before the pinned worker starts.
#
#  2. STATE IN THE RELEASE DIRECTORY. SNIPER_STATE_ROOT must never fall back to the repo
#     root: the worker runs from the pinned release, so the funder cache, socket, and
#     heartbeat would live inside a directory the next deploy replaces.
set -euo pipefail

# PHYSICAL paths (Gamma 2026-10-21): through .runtime/memecoin/current/scripts the logical
# `cd && pwd` resolved to the same ".../current" string in every release, so a release switch never
# stopped the old worker. Same pattern as memecoin_watchdog.sh.
SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/memecoin_cluster_gate.pid"
WORKER="$REPO_ROOT/scripts/memecoin_cluster_gate_worker.py"
BASENAME="memecoin_cluster_gate_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

# Identity comes from /proc, never from a command-line pattern search. A worker is a process whose
# argv[1] has exactly the worker's basename; it is ours only when argv[1] IS the pinned path (a
# substring match counted an editor, a grep or a log tail naming the file as a worker).
PROC_ROOT="${CLUSTER_WATCHDOG_PROC_ROOT:-/proc}"      # tests point this at a fake process table
alive=0
for proc in "$PROC_ROOT"/[0-9]*; do
  pid="${proc##*/}"
  [[ -r "$proc/cmdline" ]] || continue
  argv=()
  while IFS= read -r -d '' word; do argv+=("$word"); done < "$proc/cmdline" 2>/dev/null || true
  arg0="${argv[0]:-}" arg1="${argv[1]:-}"
  [[ "${arg0##*/}" == python* && "${arg1##*/}" == "$BASENAME" ]] || continue
  if [[ "$arg1" == "$WORKER" ]]; then
    alive=1
  else
    # a worker from a superseded release — stop it so it cannot keep serving decisions
    echo "$(date -u +%FT%TZ) stopping superseded cluster worker pid=$pid" >&2
    # a faked process table never signals: its pids name real host processes (the runner, say)
    [[ "$PROC_ROOT" == /proc ]] && kill -TERM "$pid" 2>/dev/null || true
  fi
done

if [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data" "$STATE_ROOT/logs"
  nohup "$PY" "$WORKER" >> "$STATE_ROOT/logs/memecoin_cluster_gate.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
