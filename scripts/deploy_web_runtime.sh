#!/usr/bin/env bash
# Deploy the web runtime to a pinned release, atomically, and prove it afterwards.
#
# WHY THIS EXISTS. On 2026-08-26 the web was restarted to clear a stale settings snapshot and came
# back on a release 73 commits old: fresh ENV, old CODE. A fix reported as live -- the web must not
# build funded contracts -- was simply absent from the tree being served, and so was the
# unreadable-USDC fix whose entire point was to protect the web path after a restart.
#
# A RESTART IS NOT A DEPLOY. The procedure existed only inside one plan document's Step 2, which is
# why hands reached for the watchdog instead. It lives here now.
set -euo pipefail

usage() { echo "usage: $0 <full-sha> [--dry-run] [--no-cycle] [--allow-local-commit]" >&2; exit 2; }
[ $# -ge 1 ] || usage
SHA="$1"; shift
DRY_RUN=0; CYCLE=1; ALLOW_LOCAL_COMMIT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-cycle) CYCLE=0 ;;
    --allow-local-commit) ALLOW_LOCAL_COMMIT=1 ;;
    *) usage ;;
  esac
  shift
done
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "deploy requires a full lowercase commit SHA" >&2; exit 1; }

SCRIPT="$(readlink -f "$0")"
SCRIPT_ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
SOURCE_ROOT="${SNIPER_SOURCE_ROOT:-$SCRIPT_ROOT}"
RUNTIME="${SNIPER_WEB_RUNTIME:-${SNIPER_STATE_ROOT:-$SOURCE_ROOT}/.runtime/web}"
RELEASES="$RUNTIME/releases"
CURRENT="$RUNTIME/current"
RELEASE="$RELEASES/$SHA"
PY="${SNIPER_PY:-$(command -v python3)}"
ENTRY="sniper/web/server.py"
# THE LIVE WEB AND NOTHING ELSE (2026-09-27). A bare `pgrep -f` on the entry matched every process
# whose command line merely NAMED it: session shells (a harness `bash -c` carries its whole command in
# argv), greps, tails. The cycle SIGTERMed them all (the harness reports exit 144; 6 session waiters
# today), and the come-back loop and the started-after-flip proof could read one as the web. The web
# runs as `<python> <tree>/<ENTRY>` (sniper-web.service ExecStart, both units' drop-ins), so match
# exactly that shape: a python binary as the FIRST argv word, the entry as the SECOND and last.
WEB_ARGV_RE="^[^ ]*/python[0-9.]* [^ ]*/${ENTRY//./\\.}\$"
web_pids() { pgrep -f -- "$WEB_ARGV_RE" || true; }

fail() { echo "deploy refused: $*" >&2; exit 1; }

if [ "$ALLOW_LOCAL_COMMIT" -eq 1 ]; then
  git -C "$SOURCE_ROOT" cat-file -e "$SHA^{commit}" 2>/dev/null || fail "commit does not exist"
  [ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ] \
    || fail "source repository has tracked changes"
  echo "operator-authorized local commit: $SHA (origin/main ancestry not required)"
else
  git -C "$SOURCE_ROOT" merge-base --is-ancestor "$SHA" origin/main 2>/dev/null \
    || fail "$SHA is not reachable from origin/main"
fi

OLD_RELEASE="$(readlink -f "$CURRENT" 2>/dev/null || true)"
OLD_SHA="$([ -n "$OLD_RELEASE" ] && basename "$OLD_RELEASE" || echo none)"
echo "web deploy: $OLD_SHA -> $SHA"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry run: would materialize $RELEASE, flip $CURRENT, cycle=$CYCLE"
  exit 0
fi

mkdir -p "$RELEASES"
if [ -e "$RELEASE" ]; then
  [ "$(git -C "$RELEASE" rev-parse HEAD 2>/dev/null || true)" = "$SHA" ] \
    || fail "existing release path has the wrong SHA"
  [ -z "$(git -C "$RELEASE" status --porcelain --untracked-files=no)" ] \
    || fail "existing release worktree is dirty"
else
  git -C "$SOURCE_ROOT" worktree add --detach "$RELEASE" "$SHA" >/dev/null \
    || fail "could not create the release worktree"
fi
[ -f "$RELEASE/$ENTRY" ] || fail "release has no $ENTRY"

# Import-level smoke test against the CANDIDATE, before anything is flipped. Catches a syntax error
# or a missing module while the old release is still serving.
env PYTHONPATH="$RELEASE" "$PY" -c "
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
ast.parse(src)
import sniper.routes.api_memecoin, sniper.services.memecoin_manual_intents  # noqa: F401
print('candidate imports clean')
" "$RELEASE/$ENTRY" || fail "candidate release failed its import smoke test"

# Snapshot ancestry before startup. The web caches this pair-bound file; requests never run git.
# Unknown git/heartbeat evidence is recorded as unknown and keeps the release alarm.
env PYTHONPATH="$SCRIPT_ROOT" "$PY" "$SCRIPT_ROOT/sniper/util/web_release_relation.py" \
  --source-root "$SOURCE_ROOT" --state-root "${SNIPER_STATE_ROOT:-$SOURCE_ROOT}" \
  --web-sha "$SHA" --output "$RELEASE/.web_release_relation.json" \
  || fail "could not record web/runner release relationship"

# ATOMIC FLIP. `ln` then `mv -Tf` replaces the symlink in one rename; a reader never sees it absent.
SWITCHED=0
rollback() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$SWITCHED" -eq 1 ] && [ -n "$OLD_RELEASE" ]; then
    echo "post-switch failure; restoring $OLD_SHA" >&2
    ln -s "$OLD_RELEASE" "$CURRENT.rollback.$$" && mv -Tf "$CURRENT.rollback.$$" "$CURRENT"
  fi
  exit "$rc"
}
trap rollback EXIT
ln -s "$RELEASE" "$CURRENT.next.$$"
mv -Tf "$CURRENT.next.$$" "$CURRENT"
SWITCHED=1
FLIPPED_AT="$(date +%s)"

if [ "$CYCLE" -eq 1 ]; then
  # `systemctl restart` times out unprivileged here and sudo is interactive-only, so the cycle is
  # kill-the-MainPID and let the supervisor bring it back THROUGH the flipped symlink. Sound
  # because the flip is atomic and already happened: whatever restarts reads the new target.
  OLD_PIDS="$(web_pids)"
  for PID in $OLD_PIDS; do
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done
  done
  for _ in $(seq 1 60); do
    NEW_PIDS="$(web_pids)"
    [ -z "$NEW_PIDS" ] || break
    sleep 1
  done
  [ -n "${NEW_PIDS:-}" ] || fail "web did not come back after the cycle"
fi

# PROVE IT FROM PRODUCTION, never from branch state. Two facts, because either alone lies:
#   1. what `current` resolves to -- that is what a NEWLY started process picks up;
#   2. that the serving process started AFTER the flip -- argv points at the SYMLINK, so a process
#      running from before the flip still holds the code it imported at ITS start.
RESOLVED="$(readlink -f "$CURRENT")"
[ "$(basename "$RESOLVED")" = "$SHA" ] || fail "current resolves to $(basename "$RESOLVED"), not $SHA"
if [ "$CYCLE" -eq 1 ]; then
  for PID in $(web_pids); do
    STARTED="$(date -d "$(ps -o lstart= -p "$PID")" +%s 2>/dev/null || echo 0)"
    [ "$STARTED" -ge "$FLIPPED_AT" ] \
      || fail "pid $PID started before the flip and is still serving the old code"
  done
fi
trap - EXIT

# Release retention (2026-09-28): the web deploy never pruned, and 121 release worktrees held 27 GB.
# Same rule as the memecoin deploy's prune_old_releases: keep current, the previous release (the
# rollback target) and the newest WEB_RELEASE_KEEP-2 others; git-worktree-remove the rest. Nothing
# is lost: this script re-materializes releases by SHA; retain unpushed local branches until
# published. Runs only after SUCCESS with the rollback trap cleared, so a prune failure can
# never undo a good deploy.
prune_old_releases() {
  local keep="${WEB_RELEASE_KEEP:-5}"
  case "$keep" in ''|*[!0-9]*) keep=5 ;; esac
  [ "$keep" -lt 2 ] && keep=2
  local cur_real prev_real real dir kept=0
  cur_real="$(readlink -f "$CURRENT" 2>/dev/null || true)"
  prev_real="$(readlink -f "$OLD_RELEASE" 2>/dev/null || true)"
  while IFS= read -r dir; do
    real="$(readlink -f "$dir" 2>/dev/null || true)"
    [ -n "$real" ] || continue
    if [ "$real" = "$cur_real" ] || [ "$real" = "$prev_real" ]; then
      continue
    fi
    kept=$((kept + 1))
    [ "$kept" -le "$((keep - 2))" ] && continue
    git -C "$SOURCE_ROOT" worktree remove "$real" 2>/dev/null \
      || git -C "$SOURCE_ROOT" worktree remove --force "$real" 2>/dev/null || true
  done < <(ls -1dt "$RELEASES"/*/ 2>/dev/null)
  git -C "$SOURCE_ROOT" worktree prune 2>/dev/null || true
}
prune_old_releases || true

echo "web deploy OK: current -> $SHA"
echo "verify independently:"
echo "  readlink -f $CURRENT"
echo "  ps -eo pid,lstart,args | grep -F $ENTRY"
