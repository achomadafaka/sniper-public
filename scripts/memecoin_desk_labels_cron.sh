#!/usr/bin/env bash
# Pinned cron wrapper for the cohort U tape labeller (scripts/research/desk_u_ranker_labels.py --once), so
# H1/H2 labels accumulate without a hand run. The labeller is idempotent: it labels only candidates whose
# window (t0 + 65 s + 600 s + 30 s settle) has closed and never rewrites a label; it writes only
# data/desk/ranker_labels.db, under its own flock, and refuses a state root inside a release.
#
# NEVER DURING A FUNDED DEPLOY: the deploy holds an exclusive flock on <state>/data/memecoin_deploy.lock.
# This wrapper reads /proc/locks for that file's inode instead of taking the lock, so it can never make
# a deploy fail; while a deploy holds it, this pass is skipped (the next hour catches up).
#
# Cron runs in host local time (UTC-4 here), so the line runs hourly at :35; with an idempotent labeller
# that covers "after 00:30Z for the previous day" whatever the timezone. Cron line (operator installs):
#   35 * * * * /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current/scripts/memecoin_desk_labels_cron.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_labels.log 2>&1
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
LOCKS="${DESK_LABELS_PROC_LOCKS:-/proc/locks}"
export SNIPER_STATE_ROOT="$STATE_ROOT"

deploy_lock="$STATE_ROOT/data/memecoin_deploy.lock"
if [[ -e "$deploy_lock" ]]; then
  inode="$(stat -c %i "$deploy_lock")"
  if grep -Eq "FLOCK[[:space:]]+[A-Z]+[[:space:]]+WRITE[[:space:]]+[0-9]+[[:space:]]+[0-9a-f]+:[0-9a-f]+:${inode}[[:space:]]" "$LOCKS"; then
    echo "$(date -u +%FT%TZ) funded deploy in progress (deploy lock held): labeller skipped this pass"
    exit 0
  fi
fi
echo "$(date -u +%FT%TZ) labeller pass"
cd "$STATE_ROOT"
exec "$PY" "$REPO_ROOT/scripts/research/desk_u_ranker_labels.py" --once
