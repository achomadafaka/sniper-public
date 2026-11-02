#!/usr/bin/env bash
# Pinned desk JUDGE worker watchdog (cron, every minute). Same shape as
# memecoin_cluster_gate_watchdog.sh: identity from /proc by script basename, a worker from a
# superseded release is stopped, state lives in SNIPER_STATE_ROOT, never the release directory.
#
# The worker runs on the Claude subscription (Agent SDK CLI login). ANTHROPIC_API_KEY and
# ANTHROPIC_BASE_URL are unset at launch so a key in the environment can never turn the seat
# into per-token API billing. MEMECOIN_DESK_JUDGE_ENABLED != 1 makes the worker exit at once.
# The worker's own flock (desk_judge_worker.lock) keeps a second copy from racing recovery.
#
# Cron line (operator installs):
#   * * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_judge_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_judge_watchdog.log 2>&1
set -euo pipefail

# the release's REAL directory: through the .runtime/memecoin/current symlink a logical path is the
# same string for every release, and a superseded worker would never be stopped (Gamma FAIL a).
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PID_FILE="$STATE_ROOT/data/desk/judge_worker.pid"
WORKER="$REPO_ROOT/scripts/memecoin_desk_judge_worker.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
# MEMECOIN_DESK_JUDGE_PLAYBOOK (cron environment, else the last assignment in <state root>/.env) picks
# the playbook, relative to the release; a new playbook is a new cohort (provision it first). Unset =
# the v18 file. A missing file starts nothing: the worker never runs a prompt the operator did not name.
playbook_rel="${MEMECOIN_DESK_JUDGE_PLAYBOOK:-}"
if [[ -z "$playbook_rel" && -r "$STATE_ROOT/.env" ]]; then
  playbook_rel="$(sed -n 's/^[[:space:]]*MEMECOIN_DESK_JUDGE_PLAYBOOK=[[:space:]]*//p' "$STATE_ROOT/.env" | tail -n 1 | tr -d "\"' \r")"
fi
PLAYBOOK="$REPO_ROOT/${playbook_rel:-docs/superpowers/specs/2026-10-21-desk-judge-playbook.md}"

export SNIPER_STATE_ROOT="$STATE_ROOT"

PROC_ROOT="${DESK_JUDGE_WATCHDOG_PROC_ROOT:-/proc}"   # tests point this at a fake process table
# identity and state root: scripts/lib/desk_proc_match.sh (a different root is not ours)
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
alive=$DESK_ALIVE
for pid in "${DESK_SUPERSEDED[@]}"; do
  echo "$(date -u +%FT%TZ) stopping superseded desk judge worker pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done
# the worker drains on SIGTERM (finishes its in-flight call and records it); wait, then SIGKILL so a hang never blocks.
# The grace covers the LARGEST deadline the old worker may run under (contract.JUDGE_DEADLINE_MAX_S = 60 s, whatever the
# new .env says) plus 15 s to record: a valid in-flight call is never cut (Codex eCat on 91218c703).
DESK_DRAIN_GRACE_S=75
for pid in "${DESK_SUPERSEDED[@]}"; do
  [[ "$PROC_ROOT" == /proc ]] || continue
  for _ in $(seq 1 $((DESK_DRAIN_GRACE_S * 4))); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) superseded desk judge worker pid=$pid did not drain in ${DESK_DRAIN_GRACE_S} s; SIGKILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
  fi
done

if [[ "$alive" -eq 0 && ! -r "$PLAYBOOK" ]]; then
  echo "$(date -u +%FT%TZ) desk judge playbook missing: $PLAYBOOK; not starting" >&2
elif [[ "$alive" -eq 0 ]]; then
  mkdir -p "$STATE_ROOT/data/desk" "$STATE_ROOT/logs"
  cd "$STATE_ROOT"
  nohup env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL "$PY" "$WORKER" \
    --desk-db "$STATE_ROOT/data/memecoin_desk.db" --ledger-db "$STATE_ROOT/sniper_tracker.db" \
    --playbook "$PLAYBOOK" --loop >> "$STATE_ROOT/logs/memecoin_desk_judge.log" 2>&1 &
  echo "$!" > "$PID_FILE"
fi
