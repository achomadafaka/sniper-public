"""Machine checks for evidence items (spec 2.1.2, R5; plan review findings 13-15).

Each item gets ok / unchecked / pending_push / broken. Only `broken` refuses a write, and `broken`
is used only when the check RAN and found the reference false:
  sha     ok if on origin/main; pending_push if on local HEAD or a local branch (builders commit
          locally and Alpha pushes later); broken if the commit is on neither, which means a tag
          or a dangling commit that was never merged, or if the object is unknown after a fetch.
  path    tracked files at origin/main; pending_push if only at HEAD or on disk; runtime paths
          (data/, logs/, .runtime/) on disk, where absent is unchecked because a .pid exists only
          while its process runs.
  knob    ok if any source file names it: the exact word, or its base followed by a per-chain
          template marker (`..._%s` % CHAIN, f"..._{chain}", "..._" + chain). A plain `grep -w`
          on the base misses those, because `_` is a word character. Rust, TOML, YAML and JS count
          as source. A known family with no exact hit is unchecked, not broken.
  column  the brain_index registry when it covers the table (authoritative there); else the
          CREATE TABLE statement (parenthesis-balanced) or an ALTER TABLE ... ADD COLUMN, and a
          miss is unchecked, never broken.
A tool, git or network error, or an unknown object when the fetch failed, is unchecked."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

RUNTIME_PREFIXES = ("data/", "logs/", ".runtime/")
SOURCE_GLOBS = ("*.py", "*.sh", "*.example", "*.rs", "*.toml", "*.yml", "*.yaml", "*.js")
_fetched: dict[str, bool] = {}


def _git(repo, *args, timeout=15):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def fetch_once(repo) -> bool:
    """One `git fetch origin` per process and repo, before any check can refuse. True if it ran."""
    key = str(repo)
    if key not in _fetched:
        try:
            _fetched[key] = _git(repo, "fetch", "-q", "origin", timeout=30).returncode == 0
        except Exception:
            _fetched[key] = False
    return _fetched[key]


def _ancestor(repo, sha, ref):
    rc = _git(repo, "merge-base", "--is-ancestor", sha, ref).returncode
    return {0: True, 1: False}.get(rc)                     # None: the check could not run


def _sha(repo, sha, ref, fetched):
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        return "broken", "not a hex SHA"
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if r.returncode != 0:
        if "ambiguous" in r.stderr:
            return "unchecked", f"{sha} is an ambiguous short SHA"
        return ("broken", f"{sha} is not a known commit") if fetched else \
               ("unchecked", f"{sha} is unknown and the fetch did not run")
    full = r.stdout.strip()
    on_ref = _ancestor(repo, full, ref)
    if on_ref is None:
        return "unchecked", f"could not compare with {ref}"
    if on_ref:
        return "ok", f"{sha} is on {ref}"
    if _ancestor(repo, full, "HEAD") or _git(repo, "branch", "--contains", full).stdout.strip():
        return "pending_push", f"{sha} is on a local branch, not yet on {ref}; the nightly check re-runs it"
    tags = _git(repo, "tag", "--contains", full, "--list", "archive/*")
    if tags.returncode != 0:
        return "unchecked", "could not inspect archive tags"
    if tags.stdout.split():
        return "unchecked", f"{sha} is only on archive tag {tags.stdout.split()[0]} (never merged under this SHA)"
    return "broken", f"{sha} is on neither {ref} nor a local branch (a tag or dangling commit: never merged)"


def _path(repo, raw, ref, fetched):
    path = re.sub(r":\d+(?:-\d+)?$", "", raw.strip())
    root = str(Path(repo).resolve()) + "/"
    if path.startswith(root):
        path = path[len(root):]
    if path.startswith(RUNTIME_PREFIXES):
        return ("ok", f"{path} exists on disk") if (Path(repo) / path).exists() else \
               ("unchecked", f"{path} is a runtime path and is absent now")
    if _git(repo, "cat-file", "-e", f"{ref}:{path}").returncode == 0:
        return "ok", f"{path} exists at {ref}"
    if _git(repo, "cat-file", "-e", f"HEAD:{path}").returncode == 0 or (Path(repo) / path).exists():
        return "pending_push", f"{path} exists locally, not yet at {ref}"
    return ("broken", f"{path} does not exist at {ref}, HEAD or on disk") if fetched else \
           ("unchecked", f"{path} is absent and the fetch did not run")


def _grep(repo, ref, *args):
    """True / False for a git grep over source files at ref; None if git grep failed."""
    # tests/ is excluded: a knob a test merely NAMES (a fixture, an invented name) is not read
    # anywhere, and counting it made an invented knob in a merged test read as `ok`.
    rc = _git(repo, "grep", "-q", *args, ref, "--", *SOURCE_GLOBS, ":!tests/").returncode
    return {0: True, 1: False}.get(rc)


def _knob(repo, name, ref):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", name or ""):
        return "broken", "not a knob name"
    hit = _grep(repo, ref, "-w", "-F", "-e", name)
    if hit:
        return "ok", f"{name} is named in code at {ref}"
    base = re.sub(r"(?:_\d+)+$", "", name)
    if base != name and hit is not None:
        hit = _grep(repo, ref, "-E", "-e", base + "_[{%<\"']")
        if hit:
            return "ok", f"{name} is read through the per-chain template {base}_<chain>"
    if hit is None:
        return "unchecked", "git grep failed"
    family = "_".join(name.split("_")[:2]) + "_"
    if name.count("_") >= 1 and _grep(repo, ref, "-F", "-e", family):
        return "unchecked", f"{name} is not named in code, but the {family} family is"
    return "broken", f"{name} is named nowhere in code at {ref}"


def _registry(repo):
    try:
        return json.loads((Path(repo) / "index" / "schema_registry.json").read_text()).get("tables", {})
    except (OSError, ValueError):
        return None


def _ddl_bodies(text, table):
    """The parenthesised column lists of every CREATE TABLE <table> ( ... ) in text."""
    out = []
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?%s\s*\(" % re.escape(table),
                         text, re.I):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.end():i - 1])
    return out


def _column(repo, ref_col, ref):
    table, _, col = (ref_col or "").partition(".")
    if not table or not col:
        return "broken", "expected table.column"
    reg = _registry(repo)
    if reg is not None and table in reg:
        names = {c.get("name") for c in reg[table].get("columns", [])}
        return ("ok", f"{ref_col} exists") if col in names else ("broken", f"{ref_col} is not a column")
    if _grep(repo, ref, "-i", "-E", "-e", rf"ALTER TABLE .*\b{table}\b.* ADD COLUMN (IF NOT EXISTS )?{col}\b"):
        return "ok", f"{ref_col} is added by an ALTER TABLE"
    r = _git(repo, "grep", "-h", "-i", "-A", "120", "-E", "-e", rf"CREATE TABLE .*\b{table}\b", ref, "--",
             "*.py", "*.sql")
    if any(re.search(rf"\b{re.escape(col)}\b", body) for body in _ddl_bodies(r.stdout, table)):
        return "ok", f"{ref_col} is in the {table} DDL"
    return "unchecked", f"{ref_col} was not found in code (Supabase, external, or added elsewhere)"


def check(item: dict, repo: Path, *, ref: str = "origin/main", fetched: bool = True) -> tuple[str, str]:
    t, r = item.get("type"), str(item.get("ref") or "").strip()
    try:
        if t == "sha":
            return _sha(repo, r, ref, fetched)
        if t == "path" or (t == "doc" and not r.startswith("http")):
            return _path(repo, r, ref, fetched)
        if t == "knob":
            return _knob(repo, r, ref)
        if t == "column":
            return _column(repo, r, ref)
    except Exception as exc:
        return "unchecked", f"check failed: {type(exc).__name__}"
    return "unchecked", f"{t} evidence is checked by a person, not by the gate"


def check_all(items, repo, *, ref="origin/main", fetched=True):
    """A path cited next to a sha is also looked up AT that sha (Beta 2026-09-29: a file that exists
    only on an unmerged branch was refused although the fact cited the branch commit)."""
    items = list(items or [])
    shas = [str(it.get("ref") or "").strip() for it in items if it.get("type") == "sha"]
    out = []
    for it in items:
        verdict, reason = check(it, repo, ref=ref, fetched=fetched)
        if verdict == "broken" and it.get("type") in ("path", "doc"):
            for sha in shas:
                if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
                    v2, r2 = check(it, repo, ref=sha, fetched=fetched)
                    if v2 == "ok":
                        verdict, reason = "pending_push", f"{r2} (the cited commit; not yet at {ref})"
                        break
        out.append({**it, "check": verdict, "check_reason": reason})
    return out
