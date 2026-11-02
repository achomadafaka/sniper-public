"""No module under `sniper/` may be reachable under a second name.

A module imported twice under two names becomes two module objects with two of every class it
defines. `except SomeError` then fails an identity check that reads as correct in the source, and
the exception sails past the handler written for it. There is no error, no warning, and the code
looks right.

THIS REPO CAN PRODUCE THAT TODAY:

    import sniper.feeds.fomo_feed as a
    import feeds.fomo_feed as b
    a.FomoThrottleBacklog is b.FomoThrottleBacklog   ->  False

WE ARE CURRENTLY SAFE BY ACCIDENT OF STYLE, NOT BY DESIGN. Of 235 named exception catches on the
funded path, 226 are `except Exception` — a builtin, immune to this. The single project-defined
exception caught by name (`GtRateLimited`) has its definition, raise sites and catch sites all
resolving through one identity. No production module imports through a shim.

Every one of those is a habit, not a rule. These tests make them rules, so the first
`except SomeCustomError` written across a shim boundary fails loudly here instead of silently in
production.
"""
import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# The root compatibility packages. Each is an `__init__.py` that re-points `__path__` at
# `sniper/<name>`, kept for external compatibility after the 2026-05-27 restructure.
SHIM_PACKAGES = sorted(
    p.name for p in REPO.iterdir()
    if p.is_dir() and (p / "__init__.py").is_file()
    and "__path__" in (p / "__init__.py").read_text(errors="replace")
)

# Modules that put their own package root on `sys.path`, which is what makes a bare `feeds.X`
# import resolvable in the first place. Frozen at the two that exist; a new one must be a
# deliberate decision, not a copy-paste.
KNOWN_PATH_MUTATORS = {
    "sniper/analysis/agent_calibration.py",
    "sniper/analysis/situation_model.py",
    "sniper/office/claude_compat.py",           # inserts the vendored SDK root (not a sniper package), pinned per release (5c40911bd)
    "sniper/office/seat_child.py",              # inserts the seat sandbox release root /opt/release, not a sniper package
    "sniper/office/codex_child.py",             # same: inserts the seat sandbox release root /opt/release, not a sniper package
    "sniper/feeds/hyperliquid_feed.py",
    "sniper/feeds/poly_binary_snapshot.py",
    "sniper/services/finance_analyst.py",
    "sniper/services/news_intel.py",
    "sniper/strategies/insider_copy_v1.py",
    "sniper/web/agent_pipeline.py",
}


def _source_files():
    for base in ("sniper", "scripts"):
        for path in (REPO / base).rglob("*.py"):
            if any(part in {".worktrees", ".runtime", "__pycache__", "archive"}
                   for part in path.parts):
                continue
            yield path


def test_the_hazard_is_real_and_this_file_is_not_theatre():
    """DEMONSTRATION. If this ever fails the shims were made identity-safe and these guards can go.

    Asserted rather than assumed, because a guard whose premise has quietly been fixed is a guard
    nobody can evaluate.
    """
    import sniper.feeds.fomo_feed as canonical
    shim = pytest.importorskip("feeds.fomo_feed")
    assert canonical is not shim
    assert canonical.FomoThrottleBacklog is not shim.FomoThrottleBacklog


def test_no_production_module_imports_through_a_shim():
    """The invariant that keeps us safe today, turned into a rule.

    A shim import is only dangerous in combination — one side canonical, one side shim — so the
    cheap enforceable version is that production never uses the shim at all. External callers may;
    that is what the shims are for.
    """
    if not SHIM_PACKAGES:
        pytest.skip("no shim packages")
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(SHIM_PACKAGES), re.M)
    offenders = []
    for path in _source_files():
        for m in pattern.finditer(path.read_text(errors="replace")):
            line = path.read_text(errors="replace")[:m.start()].count("\n") + 1
            offenders.append("%s:%d imports `%s`" % (path.relative_to(REPO), line, m.group(1)))
    assert not offenders, (
        "production code imported a module through a root shim; that module now has two "
        "identities and any `except` on its exceptions can silently miss:\n  "
        + "\n  ".join(offenders))


def test_importing_the_funded_path_leaves_no_package_root_on_sys_path():
    """THE INVARIANT, checked at runtime rather than guessed at with a regex.

    A directory on `sys.path` that is ITSELF an importable package makes every module beneath it
    reachable under a second name: with `sniper/` on the path, `feeds.fomo_feed` and
    `sniper.feeds.fomo_feed` are two modules of one file.

    Inserting the REPO root is the normal script bootstrap and is safe — the repo root has no
    `__init__.py`, so nothing beneath it gains a second name. My first version of this guard
    conflated the two and flagged 220 scripts doing the correct thing.
    """
    code = """
import json, pathlib, sys
import sniper.services.memecoin_real_runner
import sniper.services.memecoin_exits
import sniper.feeds.fomo_feed
repo = pathlib.Path.cwd().resolve()
offenders = []
for entry in sys.path:
    if entry:
        path = pathlib.Path(entry)
        try:
            if (path / '__init__.py').is_file() and repo in path.resolve().parents:
                offenders.append(str(path))
        except OSError:
            pass
print(json.dumps(offenders))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, text=True,
        capture_output=True, check=True,
    )
    offenders = json.loads(result.stdout)
    assert not offenders, (
        "a package root is on sys.path, so every module beneath it is importable under a second "
        "name and any `except` on its exceptions can silently miss: %s" % offenders)


def test_the_sniper_modules_that_mutate_sys_path_do_not_multiply():
    """Blast-radius freeze. Two of these insert a PACKAGE root (`sniper/`), which is the trigger;
    the rest insert harmless paths. Rather than re-classify each with a regex — the thing I got
    wrong first — this pins the set so a new one is a decision somebody makes on purpose.
    """
    found = {str(p.relative_to(REPO)) for p in (REPO / "sniper").rglob("*.py")
             if "sys.path.insert" in p.read_text(errors="replace")
             or "sys.path.append" in p.read_text(errors="replace")}
    new = found - KNOWN_PATH_MUTATORS
    assert not new, (
        "new module(s) under sniper/ mutate sys.path: %s. If the inserted directory is itself a "
        "package, every module beneath it gains a second identity." % sorted(new))


def test_no_funded_path_except_names_a_project_exception_that_could_split():
    """The failure mode itself: `except <ProjectException>` where the two sides may not agree.

    Builtins and stdlib are immune — `Exception` and `sqlite3.IntegrityError` are one object
    however they are reached. Only an exception defined in THIS repo can split, and only a BARE
    name can be imported through a shim; a dotted `sqlite3.IntegrityError` names its module
    explicitly. My first version flagged `sqlite3.IntegrityError`, which cannot split at all.
    """
    import builtins
    funded = ["sniper/services/memecoin_real_runner.py", "sniper/services/memecoin_exits.py",
              "sniper/services/memecoin_copy_trader.py"]
    repo_exceptions = set()
    for path in (REPO / "sniper").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(errors="replace"))):
            if isinstance(node, ast.ClassDef):
                repo_exceptions.add(node.name)
    # `GtRateLimited` is verified single-identity: defined, raised and caught through
    # `sniper.execution.geckoterminal_quote` only.
    ALLOWED = {"GtRateLimited"}
    caught = set()
    for rel in funded:
        path = REPO / rel
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(errors="replace"))):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            names = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            for n in names:
                if isinstance(n, ast.Name) and n.id not in dir(builtins) and n.id in repo_exceptions:
                    caught.add(n.id)
    unreviewed = caught - ALLOWED
    assert not unreviewed, (
        "a repo-defined exception is caught by BARE name on the funded path and has not been "
        "checked for identity splitting: %s. Verify its definition, every raise and every catch "
        "resolve through the same import path, then add it to ALLOWED." % sorted(unreviewed))
