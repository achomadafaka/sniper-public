#!/usr/bin/env python3
"""Host janitor: remove stale git worktrees and unreferenced runtime releases.

Dry run by default; --apply deletes. Never touches data/, databases, unmerged branches or
anything a running process, a cron line or a current/previous symlink points at.

Worktree: removed only when clean (git refuses dirty or untracked), unlocked,
HEAD already in origin/main, and untouched for --worktree-idle-h hours. Release dirs
under .runtime are git worktrees too; only the release rule below handles them.
Release: .runtime/<component>/releases/<sha> removed only when unreferenced,
older than --release-min-age-d days, and not among the newest --keep per component.
Branch: local branch deleted (git branch -d) only when already in origin/main and its tip is
older than --worktree-idle-h; unmerged branches older than --branch-flag-d are listed, not deleted.
Tmp tree: a directory /tmp/<crew seat>-* of ours, idle --tmp-idle-h hours, and no process working in it.
"""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / ".runtime"
TMP_CREW = re.compile(r"(alpha|beta|gamma|delta|epsilon|zeta|lambda)-")


def git(*args, cwd=REPO, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout


def tree_mtime(path):
    # Newest mtime among top-level entries; enough to see recent agent activity.
    newest = path.stat().st_mtime
    for entry in os.scandir(path):
        if entry.name != ".git":
            try:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                pass
    return newest


def references():
    """Every absolute path string a process, the crontab or a symlink names."""
    text = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                text.append(Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
                text.append(os.readlink(f"/proc/{pid}/cwd"))
            except OSError:
                pass
    text.append(subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout)
    for link in RUNTIME.rglob("*"):
        if link.is_symlink() and "releases" not in link.parts:
            text.append(os.path.realpath(link))
    return "\n".join(text)


def stale_worktrees(idle_h, now):
    main = git("rev-parse", "origin/main").strip()
    out, cur = [], {}
    for line in git("worktree", "list", "--porcelain").splitlines() + [""]:
        if line:
            key, _, value = line.partition(" ")
            cur[key] = value or True
            continue
        path = Path(cur.get("worktree", ""))
        if cur and path != REPO and "locked" not in cur and path.is_dir() and RUNTIME not in path.parents:
            head = cur.get("HEAD", "")
            merged = subprocess.run(["git", "merge-base", "--is-ancestor", head, main], cwd=REPO).returncode == 0
            idle = (now - tree_mtime(path)) / 3600
            if merged and idle >= idle_h and not git("status", "--porcelain", cwd=path, check=False).strip():
                out.append((path, head[:9], round(idle)))
        cur = {}
    return out


def stale_releases(keep, min_age_d, now, refs):
    out = []
    for releases in sorted(RUNTIME.glob("*/releases")):
        dirs = sorted((d for d in releases.iterdir() if d.is_dir() and not d.is_symlink()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep:]:
            age_d = (now - d.stat().st_mtime) / 86400
            if age_d >= min_age_d and str(d) not in refs and d.name not in refs:
                out.append((d, round(age_d)))
    return out


def stale_branches(main_ref, idle_h, flag_d, now, cwd=REPO):
    """Local branches: (delete) merged into main_ref and idle; (flag) unmerged and older than flag_d.

    Idle uses the tip committer date, so a branch just cut from main is spared. git branch -d
    still refuses a branch that is checked out in any worktree."""
    main = git("rev-parse", main_ref, cwd=cwd).strip()
    delete, flag = [], []
    for line in git("for-each-ref", "--format=%(refname:short) %(objectname) %(committerdate:unix)",
                    "refs/heads", cwd=cwd).splitlines():
        name, sha, at = line.split()
        if name == "main":
            continue
        age_h = (now - int(at)) / 3600
        merged = subprocess.run(["git", "merge-base", "--is-ancestor", sha, main], cwd=cwd).returncode == 0
        if merged and age_h >= idle_h:
            delete.append(name)
        elif not merged and age_h >= flag_d * 24:
            flag.append((name, round(age_h / 24)))
    return delete, flag


def stale_tmp_trees(root, idle_h, now):
    """/tmp is RAM here. Crew scratch copies are not git worktrees, so the rule above never sees them."""
    in_use = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            in_use.add(os.path.realpath(proc / "cwd"))
        except OSError:
            pass
    out = []
    for path in Path(root).iterdir():
        if not TMP_CREW.match(path.name) or path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            continue
        if (path / ".git").exists():  # a git worktree may hold uncommitted work; the worktree rule decides those
            continue
        idle = (now - tree_mtime(path)) / 3600
        if idle >= idle_h and not any(u == str(path) or u.startswith(f"{path}/") for u in in_use):
            out.append((path, round(idle)))
    return out


def size(path):
    return int(subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True).stdout.split()[0] or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--worktree-idle-h", type=float, default=48)
    ap.add_argument("--keep", type=int, default=5)
    ap.add_argument("--release-min-age-d", type=float, default=7)
    ap.add_argument("--branch-flag-d", type=float, default=3)
    ap.add_argument("--tmp-idle-h", type=float, default=12)
    args = ap.parse_args()
    now = time.time()
    report = {"apply": args.apply, "worktrees": [], "releases": [], "branches_deleted": [], "branches_stale_unmerged": [], "freed_bytes": 0, "errors": []}

    for path, head, idle in stale_worktrees(args.worktree_idle_h, now):
        nbytes = size(path)
        report["worktrees"].append({"path": str(path), "head": head, "idle_h": idle, "bytes": nbytes})
        if args.apply:
            r = subprocess.run(["git", "worktree", "remove", str(path)], cwd=REPO, capture_output=True, text=True)
            if r.returncode:
                report["errors"].append(f"{path}: {r.stderr.strip()}")
            else:
                report["freed_bytes"] += nbytes

    report["tmp_trees"] = []
    for path, idle in stale_tmp_trees("/tmp", args.tmp_idle_h, now):
        nbytes = size(path)
        report["tmp_trees"].append({"path": str(path), "idle_h": idle, "bytes": nbytes})
        if args.apply:
            try:
                shutil.rmtree(path)
                report["freed_bytes"] += nbytes
            except OSError as exc:
                report["errors"].append(f"{path}: {exc}")

    refs = references()
    for d, age in stale_releases(args.keep, args.release_min_age_d, now, refs):
        nbytes = size(d)
        report["releases"].append({"path": str(d), "age_d": age, "bytes": nbytes})
        if args.apply:
            try:
                shutil.rmtree(d)
                report["freed_bytes"] += nbytes
            except OSError as exc:
                report["errors"].append(f"{d}: {exc}")

    if args.apply:
        git("worktree", "prune", check=False)
    # After worktree removal, so branches freed by it qualify in the same pass.
    delete, flag = stale_branches("origin/main", args.worktree_idle_h, args.branch_flag_d, now)
    report["branches_stale_unmerged"] = [{"branch": b, "age_d": d} for b, d in flag]
    for name in delete:
        if args.apply:
            r = subprocess.run(["git", "branch", "-d", name], cwd=REPO, capture_output=True, text=True)
            if r.returncode:
                continue  # checked out in a worktree, or not merged into HEAD: leave it
        report["branches_deleted"].append(name)
    report["candidate_bytes"] = sum(x["bytes"] for x in report["worktrees"] + report["releases"] + report["tmp_trees"])
    print(json.dumps(report, indent=1))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
