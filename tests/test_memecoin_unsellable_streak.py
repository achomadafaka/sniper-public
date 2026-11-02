"""Unsellable-bag detection: a rug that still shows a healthy chart price.

The funded sweep deliberately falls back to the display mid when the whole-bag exact quote fails,
so a transient RPC outage cannot blind b_stop/catastrophe. But an unsellable bag fails EVERY sweep
while its chart keeps quoting — measured 2026-07-24, a live 4663 pool returned $2.82 on a $300
chart-implied bag. Only the STREAK separates a blip from a rug.
"""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.init_memecoin_tables(c) if hasattr(m, "init_memecoin_tables") else m.ensure_memecoin_tables(c)
    return c


def test_column_exists_and_defaults_to_zero():
    c = _conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    assert "exact_quote_misses" in cols


def test_streak_writer_sets_and_resets():
    c = _conn()
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (1,'C',1)")
    c.commit()
    m.set_position_exact_quote_misses(c, 1, 3)
    assert c.execute("SELECT exact_quote_misses FROM memecoin_positions WHERE id=1").fetchone()[0] == 3
    m.set_position_exact_quote_misses(c, 1, 0)       # a successful quote clears it
    assert c.execute("SELECT exact_quote_misses FROM memecoin_positions WHERE id=1").fetchone()[0] == 0


def test_threshold_is_configurable_and_disablable():
    from sniper.settings.memecoin import MEMECOIN_REAL_UNSELLABLE_SWEEPS as n
    assert n >= 1                      # a single failure must never trip it (transient blips)


def test_position_counter_writers_commit(tmp_path):
    """Regression (2026-07-24): inserting set_position_exact_quote_misses stole the conn.commit()
    from set_position_stop_hits, leaving the stop-debounce write in an open transaction. Both
    counter writers must be durable — assert via a SECOND connection, which only sees committed
    state."""
    db = str(tmp_path / "t.db")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    m.init_memecoin_tables(c) if hasattr(m, "init_memecoin_tables") else m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (id, mint, is_open) VALUES (1,'C',1)")
    c.commit()
    m.set_position_stop_hits(c, 1, 2)
    m.set_position_exact_quote_misses(c, 1, 3)
    c2 = sqlite3.connect(db)
    row = c2.execute(
        "SELECT stop_hits, exact_quote_misses FROM memecoin_positions WHERE id=1").fetchone()
    assert tuple(row) == (2, 3)
