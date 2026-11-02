"""Position reads are scoped per chain, so the page limit applies WITHIN a chain.

Before this, /positions/closed returned the last 200 rows globally and the client filtered
them, so a quiet chain got an arbitrary slice of whichever chain was busiest.
"""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _closed(c, mint, net, pnl, closed_at):
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
        "is_open,strategy,realized_pnl,closed_at) VALUES('u1',?,?,'paper',0,'control',?,?)",
        (mint, net, pnl, closed_at))


def test_closed_limit_applies_per_chain_not_globally():
    """A quiet chain must return ITS most recent rows, not its share of a global window."""
    c = _conn()
    for i in range(250):
        _closed(c, f"s{i}", "solana", 1.0, f"2026-07-30T00:{i // 60:02d}:{i % 60:02d}+00:00")
    _closed(c, "b1", "8453", 2.0, "2026-07-29T00:00:00+00:00")
    c.commit()

    base = m.list_closed_positions(c, limit=200, chain="base")
    assert len(base) == 1 and base[0]["mint"] == "b1"
    assert len(m.list_closed_positions(c, limit=200, chain="solana")) == 200


def test_totals_are_scoped_to_the_chain():
    c = _conn()
    _closed(c, "s", "solana", 10.0, "2026-07-30T00:00:00+00:00")
    _closed(c, "b", "8453", 99.0, "2026-07-30T00:00:00+00:00")
    c.commit()
    assert m.closed_position_totals(c, chain="solana")["total_pnl"] == 10.0
    assert m.closed_position_totals(c, chain="base")["total_pnl"] == 99.0


def test_open_positions_are_scoped_to_the_chain():
    c = _conn()
    for mint, net in (("s", "solana"), ("r", "4663")):
        c.execute(
            "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
            "is_open,strategy) VALUES('u1',?,?,'paper',1,'control')", (mint, net))
    c.commit()
    assert [p["mint"] for p in m.list_open_positions(c, chain="solana")] == ["s"]
    assert [p["mint"] for p in m.list_open_positions(c, chain="robin")] == ["r"]


def test_unknown_chain_raises_rather_than_widening_the_read():
    c = _conn()
    for fn in (m.list_open_positions, m.list_closed_positions, m.closed_position_totals):
        with pytest.raises(ValueError, match="never pooled"):
            fn(c, chain="all")


def test_omitting_chain_keeps_the_legacy_cross_chain_read():
    c = _conn()
    _closed(c, "s", "solana", 10.0, "2026-07-30T00:00:00+00:00")
    _closed(c, "b", "8453", 99.0, "2026-07-30T00:00:00+00:00")
    c.commit()
    assert m.closed_position_totals(c)["total_pnl"] == 109.0
