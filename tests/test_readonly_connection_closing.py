# tests/test_readonly_connection_closing.py
"""`get_readonly_connection()` returns a bare sqlite3.Connection, so a plain `with` does NOT
close it — sqlite3's own context manager ends the TRANSACTION and leaves the handle open.

Two scripts held a read handle on the funded ledger for their whole run because of that shape:
`scripts/refresh_pool_depth.py` (cron every 5 minutes, most of the interval spent in RPC calls)
and `scripts/paper_promotion_read.py` (the handle survived a 4,000-draw bootstrap). Gamma found
it, 2026-09-23.

The pinning risk is small by construction — `get_readonly_connection` opens with
`isolation_level=None` and `PRAGMA query_only=1`, so an IDLE handle begins no transaction and
holds no WAL read mark. The defect is that the close is not deterministic, which is exactly the
property a funded read path should not leave to the interpreter.
"""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# The scripts that read the funded ledger through this helper and then do slow work.
GUARDED = ("scripts/refresh_pool_depth.py", "scripts/paper_promotion_read.py")

# Built by concatenation on purpose. Spelled literally, this pattern would match THIS file and the
# guard would fail on its own prose — the trap in
# feedback-source-scanning-guards-must-not-read-their-own-prose-2026-08-27.
BARE = re.compile(r"with\s+" + "get_readonly" + r"_connection\s*\(")


def test_sqlite3_context_manager_does_not_close():
    """The premise. If this ever fails, the fix below is no longer needed."""
    conn = sqlite3.connect(":memory:")
    with conn:
        conn.execute("SELECT 1")
    conn.execute("SELECT 1")          # still open — a bare `with` only ended the transaction
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_closing_closes_the_handle():
    """The fix. `contextlib.closing` is what makes the close deterministic."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_the_guard_can_actually_fail():
    """A guard that cannot fire is not a guard —
    feedback-a-verification-that-cannot-fail-2026-08-27. Both strings are concatenated so this
    file never contains the pattern it forbids."""
    assert BARE.search("    with " + "get_readonly" + "_connection() as conn:")
    assert BARE.search("with  " + "get_readonly" + "_connection (db)")
    assert not BARE.search("with contextlib.closing(" + "get_readonly" + "_connection()) as conn:")


@pytest.mark.parametrize("rel", GUARDED)
def test_guarded_scripts_do_not_hold_a_bare_read_handle(rel):
    """Regression. A bare `with` here leaves the funded-ledger handle open for the whole run."""
    src = (ROOT / rel).read_text()
    assert "get_readonly" + "_connection" in src, (
        "%s no longer uses the read-only helper; drop it from GUARDED or re-point this guard" % rel)
    offenders = [line.strip() for line in src.splitlines() if BARE.search(line)]
    assert not offenders, (
        "%s opens the read handle without contextlib.closing: %r" % (rel, offenders))
