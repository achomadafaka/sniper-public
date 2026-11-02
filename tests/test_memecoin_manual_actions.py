"""P1 manual actions: engine partial-exit (keeps position open, reduced qty) and
manual-buy (kind manual_buy). Deterministic price/fill injection — no network."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services.memecoin_marks import mark_positions


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, entry=0.01, notional=100.0, uid="u1", mint="MINT"):
    pid = m.open_position(c, source_user_id=uid, source_handle="a", mint=mint, ticker="T",
                          entry_price=entry, entry_notional=notional, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1000.0, opened_at="t")
    qty = notional / entry
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=qty, notional=notional,
                  fill_source="jupiter", fraction=1.0, created_at="t")
    return pid, qty


def test_partial_exit_half_keeps_open():
    c = _conn()
    pid, qty = _open(c)               # qty = 10000
    r = ct.partial_exit(c, pid, 0.5, "t2", price_fn=lambda mint: {"price": 0.02, "liquidity": 1e15})
    assert r["closed"] is False
    assert abs(r["remaining"] - qty * 0.5) < 1e-6
    assert abs(r["realized_delta"] - (0.02 - 0.01) * qty * 0.5) < 1e-6   # +50
    assert abs(m.open_qty(c, pid) - qty * 0.5) < 1e-6
    assert m.get_position(c, pid)["is_open"] == 1


def test_partial_then_full_close_accumulates_realized():
    c = _conn()
    pid, qty = _open(c)               # qty = 10000
    ct.partial_exit(c, pid, 0.5, "t2", price_fn=lambda mint: {"price": 0.02, "liquidity": 1e15})  # +50 on 5000
    ct.partial_exit(c, pid, 1.0, "t3", price_fn=lambda mint: {"price": 0.03, "liquidity": 1e15})  # +100 on 5000
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0
    assert abs(pos["realized_pnl"] - 150.0) < 1e-6


def test_partial_exit_full_fraction_closes():
    c = _conn()
    pid, qty = _open(c)
    r = ct.partial_exit(c, pid, 1.0, "t2", price_fn=lambda mint: {"price": 0.015, "liquidity": 1e15})
    assert r["closed"] is True
    assert m.get_position(c, pid)["is_open"] == 0
    assert abs(m.get_position(c, pid)["realized_pnl"] - (0.015 - 0.01) * qty) < 1e-6


def test_partial_exit_none_when_not_open():
    c = _conn()
    pid, qty = _open(c)
    m.close_position(c, pid, "t2", 0.0)
    assert ct.partial_exit(c, pid, 0.5, "t3", price_fn=lambda mint: {"price": 0.02, "liquidity": 1e15}) is None


def test_marks_reflect_remaining_after_partial():
    c = _conn()
    pid, qty = _open(c)
    ct.partial_exit(c, pid, 0.5, "t2", price_fn=lambda mint: {"price": 0.02, "liquidity": 1e15})
    marks = mark_positions(c, stats_fn=lambda mint: {"price": 0.02, "liquidity": 1.0})
    assert len(marks) == 1
    # uPnL marks only the remaining 5000 qty: (0.02-0.01)*5000 = 50
    assert abs(marks[0]["unrealized_pnl"] - 50.0) < 1e-6


def test_manual_buy_opens_position():
    c = _conn()
    pid = ct.manual_buy(c, "u9", "MINT2", "TKR", 100.0, "t",
                        fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert pid
    pos = m.get_open_position(c, "u9", "MINT2")
    assert pos and pos["entry_fill_source"] == "jupiter"
    assert abs(m.open_qty(c, pid) - 100.0 / 0.005) < 1e-6   # manual_buy counts as entry qty


def test_manual_buy_noop_if_already_open():
    c = _conn()
    ct.manual_buy(c, "u9", "MINT2", "TKR", 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert ct.manual_buy(c, "u9", "MINT2", "TKR", 100.0, "t",
                         fill_fn=lambda *a, **k: (0.005, "jupiter")) is None


def test_manual_buy_ignores_bad_fill():
    c = _conn()
    assert ct.manual_buy(c, "u9", "MINT3", "TKR", 100.0, "t",
                         fill_fn=lambda *a, **k: (0.0, "fomo")) is None


def test_on_copy_event_leaves_entry_liquidity_unknown_when_stats_fn_given():
    """Paper step 3 (spec 2026-10-08): the provider's liquidity is no longer the entry's; the
    funded decision's own liquidity rides funded_facts under its own name."""
    from sniper.feeds.fomo_feed import CopyEvent
    from sniper.services import memecoin_copy_trader as ct
    c = _conn()
    ev = CopyEvent("u1", "a", "MINT", "T", "buy", 0.005, 1e6, 1000.0, True, "e1", "t", "t", 0.3)
    ct.on_copy_event(c, ev, 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"),
                     stats_fn=lambda mint: {"price": 0.005, "liquidity": 42000.0})
    assert m.get_open_position(c, "u1", "MINT")["entry_liquidity"] is None


def test_on_copy_event_no_stats_fn_leaves_liquidity_null_and_still_opens():
    from sniper.feeds.fomo_feed import CopyEvent
    from sniper.services import memecoin_copy_trader as ct
    c = _conn()
    ev = CopyEvent("u1", "a", "MINT", "T", "buy", 0.005, 1e6, 1000.0, True, "e1", "t", "t", 0.3)
    ct.on_copy_event(c, ev, 100.0, "t", fill_fn=lambda *a, **k: (0.005, "jupiter"))  # no stats_fn
    p = m.get_open_position(c, "u1", "MINT")
    assert p is not None and p["entry_liquidity"] is None
