#!/usr/bin/env bash
# Usage: memecoin_deploy_pinned.sh <full-sha>
# Deploys runner + web together for <full-sha> (the runtime deploy does both after --restart). Refuses unless origin/main == sha, the tree has no
# tracked changes (checked twice, 15 s apart) and the funded book is flat. Log: logs/deploy_<sha8>_<HHMMSS>.log.
# Normally exec'd by memecoin_wait_flat_then_deploy.sh. Never type the sha by hand:
#   SHA=$(git rev-parse origin/main)
set -euo pipefail
SHA="${1:?full sha}"
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$R"
mkdir -p "$R/logs"
LOG="$R/logs/deploy_${SHA:0:8}_$(date -u +%H%M%S).log"
git fetch -q origin
[ "$(git rev-parse origin/main)" = "$SHA" ] || { echo "REFUSE: origin/main is $(git rev-parse --short origin/main), not $SHA"; exit 2; }
if [ -n "$(git status --short | grep -v '^??' || true)" ]; then sleep 15; fi
[ -z "$(git status --short | grep -v '^??' || true)" ] || { echo "REFUSE: tracked changes in the tree (checked twice)"; git status --short | grep -v '^??'; exit 2; }
python3 "$R/scripts/memecoin_book_flat.py" --dip-watches
date -u +"deploy start %H:%M:%SZ  log=$LOG"
# The runtime deploy deploys the web itself after a --restart (2026-10-02), so it is not called here
# a second time. Its exit 4 means the runner is deployed and only the web failed.
RC=0
scripts/deploy_memecoin_runtime.sh "$SHA" --restart --defer-backup --require-flat >"$LOG" 2>&1 || RC=$?
if [ "$RC" -eq 4 ]; then
  echo "RUNNER DEPLOYED, WEB DEPLOY FAILED; last 20 lines of $LOG:"; tail -n 20 "$LOG"; exit 4
elif [ "$RC" -ne 0 ]; then
  echo "RUNNER DEPLOY FAILED; last 40 lines of $LOG:"; tail -n 40 "$LOG"; exit 3
fi
grep -E "deployed|activated|rollback|refuse|FAIL" "$LOG" | tail -n 8 || true
sleep 20
python3 -c "import json,time;h=json.load(open('$R/data/memecoin_runner.heartbeat'));print('hb age',round(time.time()-float(h['ts']),1),'auth_ok',h.get('auth_ok'))"
readlink -f "$R/.runtime/memecoin/current" "$R/.runtime/web/current"
date -u +"deploy end %H:%M:%SZ"
