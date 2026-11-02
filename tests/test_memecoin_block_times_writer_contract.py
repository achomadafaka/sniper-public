"""A release and the table it writes must agree about `block_times`, in both directions.

N1 of the 2026-09-23 recheck. `block_times` gained a fifth column, `block_hash`, on 2026-09-23
(6baf398e), so the window sum could join on canonical identity. Adding a column breaks any writer
that inserts POSITIONALLY: SQLite refuses four values for five columns at prepare time.

⚠️ THE RECHECK'S POINTER WAS WRONG, and it matters because the two files have different owners.
It names `db_ticks.py:204`; that writer named its columns BEFORE the column existed and still does,
so it was never affected. The two broken statements are both in `memecoin_volume_writer.py` in
pre-6baf398e code -- the live newHeads path, which runs on EVERY header, and the backfill's
`executemany`. A rollback there does not degrade the tape, it stops it on the first block.

Neither half of this can be fixed in the old code, which is shipped and uneditable. So the two
guards bracket it instead: the deploy refuses a RELEASE that cannot express the table's shape, and
the writer refuses a TABLE that cannot accept the release's statements.
"""
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from sniper.services import memecoin_volume_writer as w

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_positional_block_times_writes.py"
# Assembled, never a literal: this file is scanned by the guard it tests.
TABLE = "block_" + "times"
QUOTE_TABLE = "volume_" + "quote_prices"


def _run(target):
    return subprocess.run([sys.executable, str(GUARD), str(target)],
                          capture_output=True, text=True)


def test_the_guard_refuses_the_release_that_actually_breaks(tmp_path):
    """Built from the real pre-6baf398e file, not from a hand-written imitation of it.

    A guard tested only against a fixture someone wrote to match it proves the fixture.
    """
    old = subprocess.run(["git", "show", "6baf398e~1:sniper/services/memecoin_volume_writer.py"],
                         cwd=ROOT, capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("6baf398e~1 not reachable in this checkout")
    target = tmp_path / "sniper" / "services"
    target.mkdir(parents=True)
    (target / "memecoin_volume_writer.py").write_text(old.stdout, encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    # Two on the guard's original table -- the live header path and the backfill -- plus the two
    # `set_quote_price` statements the 2026-09-24 widening brought into scope. The count is spelled
    # per table rather than as a total, so a change to either reason fails for the right one.
    assert result.stderr.count("positional write to %s:" % TABLE) == 2, result.stderr
    assert result.stderr.count("positional write to %s:" % QUOTE_TABLE) == 2, result.stderr
    assert "deploy refused" in result.stderr


def test_the_guard_passes_the_release_that_ships():
    """The other direction, and the one a rule that matches everything would fail.

    The first version of this guard used `\\s*(?!\\()`, which backtracks to zero width and then sees
    a space rather than the paren, so it flagged all four of the current writers' correct
    statements. A guard is only evidence once it has been shown to say yes as well as no.
    """
    result = _run(ROOT / "sniper")
    assert result.returncode == 0, result.stderr


def test_prose_about_the_statement_is_not_the_statement(tmp_path):
    """A source-scanning guard that reads its own explanation fails on itself (2026-08-27).

    The reach value is WHERE the text sits: as a docstring and a comment it must be invisible, as
    the argument of an execute call it must be caught. Same characters both times.
    """
    prose = tmp_path / "prose.py"
    statement = "INSERT OR REPLACE INTO %s VALUES (?,?,?,?)" % TABLE
    prose.write_text(
        '"""A module that only TALKS about %s."""\n# %s\nTEXT = "%s"\n' % (statement, statement, statement),
        encoding="utf-8")
    assert _run(tmp_path).returncode == 0

    real = tmp_path / "real.py"
    real.write_text('def go(conn):\n    conn.execute("%s", ())\n' % statement, encoding="utf-8")
    assert _run(tmp_path).returncode == 1


def test_every_guarded_table_is_actually_guarded(tmp_path):
    """One reachable offender per guarded table, so widening the list cannot be a no-op.

    The widening on 2026-09-24 added `volume_quote_prices` and the guard refused the tree it ships
    with until the three live writers named their columns; this keeps that proof runnable after
    those writers are correct and can no longer supply it.
    """
    from importlib import util as _util
    spec = _util.spec_from_file_location("posguard", GUARD)
    guard = _util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert set(guard._TABLES) == {TABLE, QUOTE_TABLE}, \
        "a table added to the guard needs its own offender below"

    for table in (TABLE, QUOTE_TABLE):
        # Its own DIRECTORY, because the guard walks `rglob` from its root and a file path yields
        # nothing -- a per-file target would have passed for every table, listed or not.
        where = tmp_path / ("bad_%s" % table)
        where.mkdir()
        (where / "w.py").write_text(
            'def go(conn):\n    conn.execute("INSERT OR REPLACE INTO %s VALUES (?,?)", ())\n' % table,
            encoding="utf-8")
        result = _run(where)
        assert result.returncode == 1, "%s is listed but not caught: %s" % (table, result.stderr)
        assert "positional write to %s:" % table in result.stderr


def test_naming_the_columns_satisfies_the_guard(tmp_path):
    """The fix the widening demanded, in both directions, on the table it demanded it for.

    Without this the suite proves the guard says no and never that the repair says yes, so a
    pattern that rejected every `volume_quote_prices` write would still pass.
    """
    good = tmp_path / "good"
    good.mkdir()
    (good / "w.py").write_text(
        'def go(conn):\n'
        '    conn.execute("INSERT OR REPLACE INTO %s (chain_id, token, usd, observed)'
        ' VALUES (?,?,?,?)", ())\n' % QUOTE_TABLE, encoding="utf-8")
    assert _run(good).returncode == 0


def test_the_guard_does_not_flag_itself():
    result = _run(GUARD.parent)
    assert result.returncode == 0, result.stderr


def test_the_writer_refuses_a_table_it_cannot_write(tmp_path):
    """The mirror: a database left behind by an older release, or a half-run migration.

    Without this the first symptom is an OperationalError per header on a thread whose exceptions
    are swallowed, which reads as a dark feed rather than a broken one.
    """
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE block_times (chain_id TEXT NOT NULL, block_no INTEGER NOT NULL,"
                 " block_ts TEXT NOT NULL, fetched_at TEXT NOT NULL,"
                 " PRIMARY KEY (chain_id, block_no))")
    conn.commit()
    with pytest.raises(RuntimeError) as caught:
        w.assert_block_times_shape(conn)
    assert "block_hash" in str(caught.value), "the error names the column that is missing"
    conn.close()


def test_the_writer_accepts_the_table_it_creates(tmp_path):
    conn = w.connect(tmp_path / "v.db")
    w.assert_block_times_shape(conn)          # must not raise
    w.VolumeTape(conn, w.MANAGERS and "4663", 1000.0)
    conn.close()
