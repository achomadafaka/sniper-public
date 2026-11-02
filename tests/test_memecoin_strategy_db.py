import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, strat):
    return m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                           entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                           entry_lag_s=0.1, their_size_usd=0.0, opened_at="t0", strategy=strat)


def test_three_arms_hold_independent_positions_on_same_token():
    c = _conn()
    ids = [_open(c, s) for s in ("control", "momentum_a", "alpha_b")]
    assert len(set(ids)) == 3
    assert m.get_open_position(c, "u1", "M", "momentum_a")["id"] == ids[1]
    assert m.get_open_position(c, "u1", "M", "control")["id"] == ids[0]
    assert m.get_open_position(c, "u1", "M")["strategy"] == "control"


def test_update_peak_and_instrumentation():
    c = _conn(); pid = _open(c, "momentum_a")
    m.update_position_peak(c, pid, 1.8, "t5", 0)
    p = m.get_position(c, pid)
    assert p["peak_price"] == 1.8 and p["no_new_high_sweeps"] == 0
    m.set_position_instrumentation(c, pid, time_to_peak_s=42.0, max_gain_pct=80.0)
    p = m.get_position(c, pid)
    assert p["time_to_peak_s"] == 42.0 and p["max_gain_pct"] == 80.0
