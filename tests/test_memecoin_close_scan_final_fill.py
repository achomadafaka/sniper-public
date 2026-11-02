"""The close scans must read the FINAL close fill, never an earlier partial (BR4).

Two lanes share this contract: the Solana residual tail worker, which is live, and the post-exit
follow-up, which is behind a disabled flag. A query-level test keeps one fix covering both.
"""
import sqlite3

from sniper.persistence import db_memecoin as m


def _ledger_with_partial():
    """One funded take-profit that sold 25 tokens for $20, then closed 75 more for $90."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id, mint, ticker, network_id, execution_mode, "
                 "is_open, opened_at, closed_at, exit_reason, entry_notional, realized_pnl) "
                 "VALUES(1,'MINT1','T1','solana','real',0,?,?,'real_tp',100.0,10.0)",
                 ("2026-09-18T09:00:00+00:00", "2026-09-18T09:02:00+00:00"))
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, qty, price, notional, created_at) "
                 "VALUES(1,'partial_exit',25.0,0.8,20.0,'2026-09-18T09:01:00+00:00')")
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, qty, price, notional, created_at) "
                 "VALUES(1,'bracket_exit',75.0,1.2,90.0,'2026-09-18T09:02:00+00:00')")
    conn.commit()
    return conn


def test_both_close_scans_read_the_final_fill():
    conn = _ledger_with_partial()
    for fn in (m.recent_real_tp_closes, m.recent_real_tp_closes_with_principal):
        rows = fn(conn, since_iso="2026-09-18T00:00:00+00:00")
        assert len(rows) == 1, (fn.__name__, rows)
        assert rows[0]["exit_qty"] == 75.0, fn.__name__
        assert rows[0]["exit_usdc"] == 90.0, fn.__name__


def test_the_old_join_shape_returned_both_fills():
    """NEGATIVE CONTROL. Without it this test could pass on a query that never had the defect.

    The old join was ONE-TO-MANY with no ordering: the position returns BOTH fills and whichever
    row the consumer reads first wins. That is the bug, not a wrong choice by SQLite.
    """
    conn = _ledger_with_partial()
    rows = conn.execute(
        "SELECT f.qty, f.notional FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id = p.id AND f.kind LIKE '%exit%' "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.exit_reason='real_tp'").fetchall()
    assert [(r["qty"], r["notional"]) for r in rows] == [(25.0, 20.0), (75.0, 90.0)]


def test_a_single_fill_position_is_unchanged():
    """The common case: one exit fill, and the binding must not alter it."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id, mint, ticker, network_id, execution_mode, "
                 "is_open, opened_at, closed_at, exit_reason, entry_notional, realized_pnl) "
                 "VALUES(2,'MINT2','T2','solana','real',0,?,?,'real_tp',100.0,5.0)",
                 ("2026-09-18T09:00:00+00:00", "2026-09-18T09:02:00+00:00"))
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, qty, price, notional, created_at) "
                 "VALUES(2,'bracket_exit',50.0,1.0,50.0,'2026-09-18T09:02:00+00:00')")
    conn.commit()
    rows = m.recent_real_tp_closes_with_principal(conn, since_iso="2026-09-18T00:00:00+00:00")
    assert [(r["exit_qty"], r["exit_usdc"]) for r in rows] == [(50.0, 50.0)]
