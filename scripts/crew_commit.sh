#!/usr/bin/env bash
# Exact owned files only; every crew writer must use the same persistent lockfile.
set -euo pipefail
if [ "$#" -lt 2 ]; then
    echo 'usage: scripts/crew_commit.sh <message> <owned files...>' >&2
    exit 2
fi
status=0
flock -w 30 -E 75 /home/bigpapi/.cache/sniper-crew-git.lock python3 - "$@" <<'PY' || status=$?
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


def refuse(message, paths=()):
    print(f"crew_commit: {message}; report to Alpha. Index left in place.", file=sys.stderr)
    for path in paths:
        print(f"  {path!r}", file=sys.stderr)
    raise SystemExit(1)


added = committed = False
owned = []
try:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).rstrip("\n"))

    def git(*args):
        return subprocess.check_output(["git", "--literal-pathspecs", *args], cwd=root)

    def names(*args):
        return [os.fsdecode(p) for p in git(*args).split(b"\0") if p]

    def staged():
        return set(names("diff", "--cached", "--no-renames", "--name-only", "-z"))

    def generated(path):
        p = PurePosixPath(path)
        return p.parent == PurePosixPath("index") and p.suffix == ".json"

    def unstage_owned():
        try:
            mine = sorted(set(owned) & staged())
            if mine:
                git("restore", "--staged", "--", *mine)
        except subprocess.CalledProcessError:
            print("crew_commit: could not unstage owned paths; leave the index and report to Alpha.", file=sys.stderr)
            return
        print("crew_commit: commit failed, your files are unstaged and untouched", file=sys.stderr)

    initial = staged()
    if initial:
        refuse("staging index is not empty; never clear another agent's residue", sorted(initial))
    message, *requested = sys.argv[1:]
    if not message.strip():
        refuse("commit message is empty")
    for raw in requested:
        p = PurePosixPath(raw)
        path = str(p)
        target = root / path
        if not p.parts or p.is_absolute() or ".." in p.parts or any(c in raw for c in "*?[") or target.is_dir():
            refuse("use exact repository-relative files, without directories, globs or '..'", [raw])
        if not target.is_file() and not target.is_symlink():
            if names("ls-files", "-z", "--", path) != [path]:
                refuse("path is not a file or an exact tracked deletion", [raw])
        if path not in owned:
            owned.append(path)
    added = True
    git("add", "--", *owned)
    selected = staged()
    if not selected:
        print("crew_commit: no change to commit", file=sys.stderr)
        raise SystemExit(1)
    if set(owned) - selected or any(p not in owned and not generated(p) for p in selected):
        unstage_owned()
        refuse("staged scope differs from owned files plus generated index/*.json", sorted(selected))
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True)
    committed = True
    sha = git("rev-parse", "HEAD").decode().strip()
    changed = names("diff-tree", "--root", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", "HEAD")
    if any(p not in owned and not generated(p) for p in changed):
        refuse(f"commit {sha} contains unexpected paths; do not amend or reset", changed)
    checked = set(owned) | {p for p in changed if generated(p)} | {"index/schema_registry.json", "index/dependency_index.json"}
    # Git compares HEAD/index/working content and modes, including deletions and symlinks.
    dirty = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *sorted(checked))
    if dirty or staged():
        refuse(f"commit {sha} completed but post-commit index/working checks differ", [os.fsdecode(dirty)])
    print(sha)
except subprocess.CalledProcessError as exc:
    if added and not committed:
        unstage_owned()
    print(f"crew_commit: Git failed ({exc.returncode}); leave the index in place and report to Alpha.", file=sys.stderr)
    raise SystemExit(exc.returncode)
PY
if [ "$status" -eq 75 ]; then
    echo 'crew_commit: timed out after 30s waiting for the crew Git lock; report to Alpha.' >&2
fi
exit "$status"
