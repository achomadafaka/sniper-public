"""Scripts must anchor filesystem paths at the repo root, not the process CWD (2026-09-02).

`scripts/memecoin_position_pool_reconcile.py` opened `sqlite3.connect("sniper_tracker.db")`. Run
from anywhere but the repo root that does not raise — sqlite CREATES an empty ledger, the sweep
finds no positions, and it prints a clean "nothing to reconcile". It fails toward absent, which is
the direction nobody investigates. An empty `/tmp/sniper_tracker.db` dated 2026-08-31 already
existed on this host, so the class has bitten before.

Cron makes it likelier, not less: cron runs with a home-directory CWD, so a relative path is wrong
by default there. Carrying `cd /repo &&` in the cron line puts a property the script should own
into the invocation instead.

`db_ticks.ticks_db_path()` already resolves through `state_root()` and its docstring names this
exact trap — the pinned runner's CWD is its release directory, discarded on the next deploy.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# A bare relative literal passed to sqlite3.connect, e.g. sqlite3.connect("sniper_tracker.db").
# Anchored forms — str(ROOT / ...), os.path.join(...), an absolute literal — do not match.
_RELATIVE_CONNECT = re.compile(r'sqlite3\.connect\(\s*["\'](?!/|file:/)[^"\']*\.db["\']')
# load_env_files(".") reads no .env from another CWD, so the process runs on defaults silently.
_RELATIVE_ENV = re.compile(r'load_env_files\(\s*["\']\.["\']\s*\)')


def _offenders(pattern):
    out = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        text = path.read_text(errors="ignore")
        if pattern.search(text):
            out.append(str(path.relative_to(ROOT)))
    return out


def test_no_script_opens_a_database_by_relative_path():
    assert not _offenders(_RELATIVE_CONNECT), (
        "these create an EMPTY database instead of failing when run from another CWD:\n  "
        + "\n  ".join(_offenders(_RELATIVE_CONNECT)))


def test_no_script_loads_env_from_the_process_cwd():
    assert not _offenders(_RELATIVE_ENV), (
        "these silently read no .env when run from another CWD:\n  "
        + "\n  ".join(_offenders(_RELATIVE_ENV)))
