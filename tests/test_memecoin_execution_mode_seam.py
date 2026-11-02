"""Item 5 — execution_mode seam: the paper exit sweep must NEVER see (and therefore never
modeled-close) a real-money position, and vice versa. The blocker before any real row exists."""
import sqlite3
import pytest
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, uid, mint, mode):
    return m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                           entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                           execution_mode=mode)


def test_paper_exit_readers_never_see_real_rows():
    c = _c()
    _open(c, "up", "PAPER", "paper"); _open(c, "ur", "REAL", "real")
    assert {p["mint"] for p in m.list_open_positions(c)} == {"PAPER"}
    assert {p["mint"] for p in m.open_positions_by_strategy(c, "alpha_b")} == {"PAPER"}
    assert m.get_open_position(c, "ur", "REAL", "alpha_b") is None        # real invisible to paper reader
    assert m.get_open_position(c, "up", "PAPER", "alpha_b") is not None
    assert m.open_position_user_ids(c) == {"up"}


def test_real_mode_readers_see_only_real_rows():
    c = _c()
    _open(c, "up", "PAPER", "paper"); _open(c, "ur", "REAL", "real")
    assert {p["mint"] for p in m.list_open_positions(c, mode="real")} == {"REAL"}
    assert {p["mint"] for p in m.open_positions_by_strategy(c, "alpha_b", mode="real")} == {"REAL"}
    assert m.get_open_position(c, "ur", "REAL", "alpha_b", mode="real") is not None
    assert m.open_position_user_ids(c, mode="real") == {"ur"}


def test_open_position_rejects_invalid_execution_mode():
    c = _c()
    with pytest.raises(ValueError):
        _open(c, "u", "M", "live")   # only paper|real allowed


def test_eval_surfaces_exclude_real_positions():
    # the paper A/B verdict must not be polluted by real-money rows (they get their own read)
    c = _c()
    for mode, pnl in (("paper", 5.0), ("real", 99.0)):
        pid = _open(c, "u" + mode, "M" + mode, mode)
        m.close_position(c, pid, "t2", pnl, exit_reason="x")
    s = {r["strategy"]: r for r in m.strategy_summary(c)}      # default paper
    assert s["alpha_b"]["closed"] == 1 and abs(s["alpha_b"]["realized_pnl"] - 5.0) < 1e-9
    ss = {r["signal_source"]: r for r in m.signal_source_summary(c)}
    assert sum(r["closed"] for r in ss.values()) == 1
    assert sum(t["closed"] for t in m.trader_performance(c)) == 1
