"""news_intel marks its analysed rows processed without holding the funded ledger's write lock.

news_items lives in sniper_tracker.db, the funded ledger. The lock sampler caught
`python3 -m sniper.services.news_intel` holding the WAL write lock for at least 2,493 ms at
2026-09-27 00:07:33Z; the cron runs at minute 7 every 4 h, so a funded entry or exit booking can
wait behind it. The log placed the hold exactly: "Marked 457 news items as processed" came 2.5 s
after the previous line. It was `executemany(UPDATE ... WHERE title = ?)` with 457 titles, and the
only index holding `title` is UNIQUE(source, title), so each update was a full scan of ~38k rows,
all inside one write transaction.

The bound is counted in SQLite VM instructions, not wall time, so it cannot flake on a busy box.
"""
import ast
import inspect
import sqlite3
import textwrap

import pytest

from brain import _ensure_tables
from sniper.services import news_intel as N

ROWS, NEW = 20_000, 400


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ledger.db"))
    _ensure_tables(c)
    c.executemany("INSERT INTO news_items (source, title, processed) VALUES (?, ?, 1)",
                  [("old", "old title %d" % i) for i in range(ROWS)])
    c.executemany("INSERT INTO news_items (source, title, processed) VALUES (?, ?, 0)",
                  [("new", "new title %d" % i) for i in range(NEW)])
    c.execute("INSERT INTO news_items (source, title, processed) VALUES ('other', 'new title 7', 0)")
    c.execute("INSERT INTO news_items (source, title, processed) VALUES ('left', 'not this run', 0)")
    c.commit()
    yield c
    c.close()


def _mark(conn, titles):
    ops, statements = [0], []

    def tick():
        ops[0] += 1
        return 0

    conn.set_progress_handler(tick, 1000)
    conn.set_trace_callback(statements.append)
    try:
        N.mark_news_processed(conn, titles)
    finally:
        conn.set_progress_handler(None, 1000)
        conn.set_trace_callback(None)
    return ops[0] * 1000, [s for s in statements if s.lstrip().upper().startswith("UPDATE")]


def test_the_end_state_is_what_the_old_per_title_update_left(conn):
    titles = ["new title %d" % i for i in range(NEW)] + ["new title 3", "never stored"]
    _mark(conn, titles)
    rows = dict(((s, t), p) for s, t, p in conn.execute(
        "SELECT source, title, processed FROM news_items WHERE processed = 0 OR source != 'old'"))
    assert all(rows[("new", "new title %d" % i)] == 1 for i in range(NEW))
    assert rows[("other", "new title 7")] == 1, "same title from another source: marked, as before"
    assert rows[("left", "not this run")] == 0, "a title not analysed this run stays unprocessed"
    assert conn.execute("SELECT COUNT(*) FROM news_items WHERE source = 'old' AND processed = 1"
                        ).fetchone()[0] == ROWS


def test_the_write_is_one_statement_bounded_by_the_new_rows_not_the_table(conn):
    """The old shape costs titles x rows (~8M row visits here); the bound is far below it."""
    titles = ["new title %d" % i for i in range(NEW)]
    ops, updates = _mark(conn, titles)
    assert len(updates) == 1, "one UPDATE statement, one short transaction"
    assert ops < 2_000_000, "the write transaction ran %d VM instructions" % ops


def test_an_empty_run_writes_nothing(conn):
    ops, updates = _mark(conn, [])
    assert conn.execute("SELECT COUNT(*) FROM news_items WHERE processed = 0").fetchone()[0] == NEW + 2


def test_the_pipeline_marks_through_the_bounded_function():
    tree = ast.parse(textwrap.dedent(inspect.getsource(N.NewsIntel.run_once)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(getattr(c.func, "id", None) == "mark_news_processed" for c in calls)
    assert not any(getattr(c.func, "attr", None) == "executemany" for c in calls)
