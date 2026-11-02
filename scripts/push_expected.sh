#!/usr/bin/env bash
# usage: scripts/push_expected.sh <sha-prefix>...  (Alpha only.) Pushes HEAD to origin/main only if every commit in
# origin/main..HEAD (first-parent and merged) is one of the named prefixes. Refuses otherwise.
set -euo pipefail
cd /home/bigpapi/Desktop/Sniper
git fetch -q origin
git merge-base --is-ancestor origin/main HEAD || { echo "REFUSE: not a fast-forward"; exit 2; }
extra=0
for c in $(git rev-list origin/main..HEAD); do
  ok=0; for p in "$@"; do [[ "$c" == "$p"* ]] && ok=1; done
  [ $ok = 1 ] || { echo "UNEXPECTED $(git log -1 --format='%h %s' "$c")"; extra=1; }
done
[ $extra = 0 ] || { echo "REFUSE: unexpected commits above"; exit 3; }
S=$(git rev-parse HEAD); [ -n "$S" ] || exit 4
git push -q origin "$S:main" && git log --oneline -1 origin/main
