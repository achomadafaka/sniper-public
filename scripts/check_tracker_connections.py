#!/usr/bin/env python3
"""Flag raw `sqlite3.connect` against the funded ledger outside the persistence layer.

WHY THIS EXISTS. sniper_tracker.db-wal reached 1.4 GB on 2026-08-26 because long-lived readers held
snapshots that stopped every checkpoint halfway. `get_readonly_connection` fixes that
(isolation_level=None, query_only=1) and had already fixed it once, in 2026-07-25 -- the fix did not
survive, because five NEW scripts were written with a raw connect. The helper was correct and
nothing routed callers to it. That is a missing guardrail, not five bugs.

NO CENTRAL ALLOWLIST, DELIBERATELY. A waiver is an inline `# wal-ok: <reason>` comment on the call
itself. A hand-maintained list of exempt files rots the moment a file is renamed or deleted and
nobody notices, which is exactly how a guard ends up checking names that no longer exist. A comment
cannot name a file that is not there: it lives on the line it exempts or it does not exist.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import sys

LEDGER = "sniper_tracker.db"
WAIVER = "wal-ok"
PATH_WAIVER = "path-ok"
# The persistence layer IS the right place for a raw handle: it is what the helper is built from.
EXEMPT_DIRS = ("sniper/persistence/", "archive/", ".runtime/", ".claude/", "tests/")
SKIP_DIRS = {"archive", "node_modules", "venv", ".venv", "__pycache__", "site-packages"}


def tracker_names(tree, src):
    """Names bound to the ledger path, so `sqlite3.connect(f"file:{TRACKER}...")` is caught too."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            try:
                seg = ast.get_source_segment(src, node.value) or ""
            except Exception:
                continue
            if LEDGER in seg:
                found.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return found


def closed_in_scope(tree, src, node):
    """True when the connection opened at `node` is closed (or `with`-managed) in the same scope."""
    var = None
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if child is not node:
                continue
            if isinstance(parent, ast.withitem):
                return True                      # context-managed: closed on exit
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
                t = parent.targets[0]
                if isinstance(t, ast.Name):
                    var = t.id
    if var is None:
        return False
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)):
            continue
        for c in ast.walk(fn):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "close" and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == var):
                return True
    return False


def long_lived(tree, src, node):
    """Only a LONG-LIVED handle can pin the WAL, so only those are worth failing a build over.

    A one-shot migration or report opens, queries and exits in seconds; its snapshot dies with it.
    Flagging those too produced 89 hits on first run (2026-08-26), which is a gate that gets
    switched off rather than fixed -- the same inert-check shape as a threshold whose condition is
    satisfied almost always.

    The three shapes that outlive a statement, all DERIVED from the file rather than a list someone
    maintains by hand:
      1. the file runs a daemon loop, so anything it opens can live for days
      2. the handle is stored on an instance (`self.x = connect(...)`), living with the object
    A bare module-level connect in a one-shot script is deliberately NOT one of them.
    """
    # A handle that is CLOSED in the function that opened it cannot outlive that call, however
    # long the process runs. Without this the gate flags correct open/query/close code inside any
    # daemon -- which trains people to ignore it, the worst outcome for a guardrail.
    if closed_in_scope(tree, src, node):
        return None
    if "while True" in src:
        return "daemon loop in this file"
    for parent in ast.walk(tree):
        for field in ast.iter_child_nodes(parent):
            if field is not node:
                continue
            if isinstance(parent, ast.Assign):
                if any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                       and t.value.id == "self" for t in parent.targets):
                    return "stored on self, outlives the call"
    # NOT flagged: a bare module-level connect in a one-shot script. Its handle lives exactly as
    # long as the script does, which is seconds -- module level means process lifetime, and for a
    # report or a migration that is not long. Including it took the gate from 9 hits to 15, all of
    # the extras being scripts that cannot pin anything. Precision matters more than recall here:
    # a gate that fails a build over a harmless line is a gate someone deletes.
    return None


def violations(path, src):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    names, lines, out = tracker_names(tree, src), src.splitlines(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "connect"
                and isinstance(f.value, ast.Name) and f.value.id == "sqlite3"):
            continue
        args = " ".join((ast.get_source_segment(src, a) or "") for a in node.args)
        hits_ledger = LEDGER in args or any(n in args for n in names)
        if not hits_ledger:
            continue
        # waiver on the call line or the line above it
        ctx = " ".join(lines[max(0, node.lineno - 2):node.lineno])
        if WAIVER in ctx:
            continue
        why = long_lived(tree, src, node)
        if not why:
            continue                            # short-lived: cannot hold a snapshot long enough
        out.append((node.lineno, (lines[node.lineno - 1] or "").strip()[:88], why))
    return out



def bootstrap_missing(path, src):
    """A subdirectory entrypoint that imports `sniper.*` before putting the repo root on sys.path.

    `python3 scripts/research/X.py` sets sys.path[0] to scripts/research, so the package is not
    importable and the script dies at import. On 2026-08-26 adding such an import took the funded
    real_hook feeder dark for ~90 seconds on relaunch.

    CHANGED FILES ONLY, deliberately. Eleven pre-existing entrypoints trip this, and some are
    launched by wrappers that set PYTHONPATH -- unverified, so blanket-waiving them would assert
    something not checked. Scoped to what a commit touches, it catches the case that caused the
    outage (a file being modified) without failing a build over code nobody is changing. This is a
    stated scope limit, not coverage.
    """
    rel = path.as_posix().lstrip("./")
    if "/" not in rel or path.name == "__init__.py":
        return None                       # repo root: the package is importable from there
    if rel.startswith("sniper/"):
        # PACKAGE-INTERNAL MODULES ARE NOT ENTRYPOINTS. They are imported through the package, so
        # a bare-launch sys.path is not their launch condition — flagging them here blocked a live
        # rails hotfix on a false positive within hours of shipping (2026-08-26). A package module
        # that IS also run bare (python3 sniper/x/y.py) dies loudly at import and is its own bug.
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    line = None
    for node in tree.body:                # module level only; a function-local import is lazy
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or " ".join(a.name for a in node.names)
            if str(mod).startswith("sniper"):
                line = node.lineno
                break
    if line is None:
        return None
    head = src.splitlines()[:line - 1]
    if any("sys.path" in l and "insert" in l for l in head):
        return None
    if any(PATH_WAIVER in l for l in src.splitlines()[:line]):
        return None
    return line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*")
    a = ap.parse_args()
    if a.files:
        files = [pathlib.Path(f) for f in a.files]
    else:
        # PRUNE at the directory level, never filter after a full walk: .runtime/memecoin/releases
        # holds a complete repo copy PER RELEASE, so rglob("*.py") re-scans the whole codebase once
        # per release. Measured 2026-08-26: over 120s before pruning.
        import os
        files = []
        for dirpath, dirnames, filenames in os.walk("."):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            files += [pathlib.Path(dirpath) / fn for fn in filenames if fn.endswith(".py")]
    bad = conn_bad = 0
    for p in sorted(files):
        if not p.exists() or p.suffix != ".py":
            continue
        if any(d in p.as_posix() for d in EXEMPT_DIRS):
            continue
        try:
            src = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if a.files:                       # see bootstrap_missing: changed files only
            bad_line = bootstrap_missing(p, src)
            if bad_line:
                print(f"{p}:{bad_line}: imports sniper.* before any sys.path bootstrap. A bare "
                      f"`python3 {p}` cannot import the package and dies at startup. Add the repo "
                      f"root to sys.path above this import (or mark `# {PATH_WAIVER}: <reason>`).")
                bad += 1
        for lineno, text, why in violations(p, src):
            print(f"{p}:{lineno}: long-lived raw sqlite3.connect on {LEDGER} ({why}) -- use "
                  f"get_readonly_connection() (or mark `# {WAIVER}: <reason>` if a raw handle is "
                  f"deliberate)\n    {text}")
            bad += 1
            conn_bad += 1
    if conn_bad:
        print(f"\n{conn_bad} raw connection(s) to the funded ledger. A long-lived one pins the WAL: "
              f"isolation_level defaults to leaving a read snapshot open, and a checkpoint then "
              f"stops at it and never truncates.")
    if bad and not conn_bad:
        print(f"\n{bad} import-bootstrap fault(s). A bare launch of these files dies at import.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
