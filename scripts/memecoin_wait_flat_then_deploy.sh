#!/usr/bin/env bash
# Usage: memecoin_wait_flat_then_deploy.sh <full-sha>
# Polls every 20 s until the funded book is flat AND the tree has no tracked changes, prints
# "flat and clean", then execs memecoin_deploy_pinned.sh (which re-checks both). Gives up after 2 h.
# Once "flat and clean" is logged the deploy is running: never kill the waiter after that line.
set -uo pipefail
: "${1:?full sha}"
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for i in $(seq 1 360); do
  dirty="$(git -C "$R" status --short | grep -v '^??' || true)"
  if [ -z "$dirty" ] && python3 "$R/scripts/memecoin_book_flat.py" --dip-watches >/dev/null 2>&1; then
    date -u +"flat and clean at %H:%M:%SZ"; exec "$R/scripts/memecoin_deploy_pinned.sh" "$1"
  fi
  [ $((i % 6)) -eq 0 ] && { date -u +"%H:%M:%SZ waiting:"; [ -n "$dirty" ] && echo "$dirty"; python3 "$R/scripts/memecoin_book_flat.py" --dip-watches; }
  sleep 20
done
echo "gave up after 2 h"; exit 3
