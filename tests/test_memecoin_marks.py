import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_marks as mk


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_mark_positions_computes_unrealized():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="MINT",
                          ticker="T", entry_price=0.005, entry_notional=100.0,
                          entry_fill_source="jupiter", entry_lag_s=0.3,
                          their_size_usd=1000.0, opened_at="t")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.005, qty=20000.0,
                  notional=100.0, fill_source="jupiter", fraction=1.0, created_at="t")
    stats = lambda mint, http=None: {"price": 0.010, "market_cap": 2e6,
                                     "liquidity": 50000.0, "volume_24h": 1e5}
    rows = mk.mark_positions(c, stats_fn=stats)
    assert rows[0]["current_price"] == 0.010
    assert abs(rows[0]["unrealized_pnl"] - 100.0) < 1e-6  # (0.010-0.005)*20000
