"""Pausing a position stops the reads without pretending it is closed.

Operator 2026-09-04: a dust play whose frozen policy has every rail at 0 could never produce an
exit decision, yet still cost 3,481 marks an hour in quotes and RPC.
"""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, pid, strategy="alpha_b"):
    c.execute("INSERT INTO memecoin_positions(id, mint, strategy, execution_mode, is_open,"
              " entry_price, entry_notional) VALUES(?,?,?,'real',1,1.0,100.0)",
              (pid, f"0x{pid}", strategy))
    c.commit()


def test_a_paused_position_leaves_the_sweep():
    c = _conn(); _open(c, 1); _open(c, 2)
    m.set_position_tracking_paused(c, 2, "2026-09-04T19:00:00+00:00")
    worked = {p["id"] for p in m.open_positions_by_strategy(c, "alpha_b", mode="real")}
    assert worked == {1}


def test_it_is_still_open_and_still_ours():
    """The tokens are still in the wallet. This is an attention switch, not a close."""
    c = _conn(); _open(c, 1)
    m.set_position_tracking_paused(c, 1, "2026-09-04T19:00:00+00:00")
    row = m.get_position(c, 1)
    assert row["is_open"] == 1
    assert row["entry_notional"] == 100.0
    assert row["closed_at"] is None


def test_accounting_still_sees_it():
    """Exposure, bankroll and slot maths must keep counting a paused position."""
    c = _conn(); _open(c, 1)
    m.set_position_tracking_paused(c, 1, "2026-09-04T19:00:00+00:00")
    assert [p["id"] for p in m.list_open_positions(c, mode="real")] == [1]


def test_include_paused_sees_the_whole_book():
    c = _conn(); _open(c, 1); _open(c, 2)
    m.set_position_tracking_paused(c, 2, "2026-09-04T19:00:00+00:00")
    seen = {p["id"] for p in m.open_positions_by_strategy(
        c, "alpha_b", mode="real", include_paused=True)}
    assert seen == {1, 2}


def test_resuming_puts_it_back_in_the_sweep():
    c = _conn(); _open(c, 1)
    m.set_position_tracking_paused(c, 1, "2026-09-04T19:00:00+00:00")
    m.set_position_tracking_paused(c, 1, None)
    assert [p["id"] for p in m.open_positions_by_strategy(c, "alpha_b", mode="real")] == [1]


# --- the fast lane is where the cost actually is (2026-09-04) ---------------------------------

def _fastlane_worked(c):
    from sniper.services.memecoin_fastlane import open_real_positions
    return [p["id"] for p in open_real_positions(c, limit=10)]


def test_the_fast_lane_skips_a_paused_position():
    """Filtering only the exit sweep did NOT stop the reads: the fast lane wrote 4,605 marks in
    two minutes for a single paused dust play, against 483 from DexScreener."""
    c = _conn(); _open(c, 1); _open(c, 2)
    m.set_position_tracking_paused(c, 2, "2026-09-04T20:36:17+00:00")
    assert _fastlane_worked(c) == [1]


def test_resuming_returns_it_to_the_fast_lane():
    c = _conn(); _open(c, 1)
    m.set_position_tracking_paused(c, 1, "2026-09-04T20:36:17+00:00")
    assert _fastlane_worked(c) == []
    m.set_position_tracking_paused(c, 1, None)
    assert _fastlane_worked(c) == [1]


def test_a_table_without_the_column_still_returns_work():
    """An exception here is swallowed and yields an EMPTY work set, which would silently stop the
    fast lane marking EVERY position rather than one paused one."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, mint TEXT,"
              " strategy TEXT, execution_mode TEXT, is_open INTEGER,"
              " entry_price REAL, entry_notional REAL)")
    c.execute("INSERT INTO memecoin_positions VALUES(1,'0x1','alpha_b','real',1,1.0,100.0)")
    c.commit()
    assert _fastlane_worked(c) == [1]
