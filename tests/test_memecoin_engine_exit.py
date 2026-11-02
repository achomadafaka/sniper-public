import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services import memecoin_copy_trader as eng


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _ev(side, uid="u1", mint="MINT"):
    return CopyEvent(uid, "alice", mint, "PEPE", side, 0.005, 1e6, 3318.0, True,
                     "e", "t", "t", 0.3)


def test_sell_mirrors_exit_and_realizes_pnl():
    c = _conn()
    eng.on_copy_event(c, _ev("buy"), 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))
    # exit route quote 0.010 -> qty=20000, pnl=(0.010-0.005)*20000 = 100
    r = eng.on_copy_event(c, _ev("sell"), 100.0, "t2",
                          exit_quote_fn=lambda mint, net, qty: (0.010, "route_quote"))
    assert r == "exit"
    closed = m.list_closed_positions(c)[0]
    assert abs(closed["realized_pnl"] - 100.0) < 1e-6


def test_sell_without_position_is_ignored():
    c = _conn()
    assert eng.on_copy_event(c, _ev("sell"), 100.0, "t",
                             stats_fn=lambda mint: {"price": 0.01, "liquidity": 1e15}) == "ignored"
