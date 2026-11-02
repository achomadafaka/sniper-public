"""Per-trader pump profile aggregated over matured outcomes — the front-run selection read."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _matured(c, uid, eid, ref, max_gain, marks, status="matured"):
    m.create_signal_outcome(c, fomo_event_id=eid, source_user_id=uid, handle=uid, mint="M"+eid,
                            ticker="T", buy_ts="2026-07-07T10:00:00Z", ref_price=ref,
                            their_size_usd=3000.0, now_iso="2026-07-07T10:00:00Z")
    m.save_outcome(c, eid, marks=marks, max_gain_pct=max_gain, peak_horizon_s=60,
                   last_price=ref, n_marks=len(marks), status=status, now_iso="2026-07-07T11:00:00Z")


def test_profile_aggregates_matured_only():
    c = _conn()
    gains = [10.0, 30.0, 50.0, -20.0, 100.0]
    for i, g in enumerate(gains):
        _matured(c, "u1", f"e{i}", 0.005, g, {60: {"price": 0.005 * (1 + g / 100), "age_s": 61}})
    _matured(c, "u1", "eopen", 0.005, 999.0, {60: {"price": 1.0}}, status="open")  # not counted
    prof = {p["source_user_id"]: p for p in m.trader_pump_profile(c, min_signals=1, hit_pct=20.0)}
    p = prof["u1"]
    assert p["n_signals"] == 5                        # open one excluded
    assert abs(p["median_max_gain_pct"] - 30.0) < 1e-6
    assert abs(p["mean_max_gain_pct"] - 34.0) < 1e-6
    assert abs(p["pump_hit_rate"] - 0.6) < 1e-6       # 30,50,100 >= 20 -> 3/5


def _dust(c, uid, eid):   # a matured outcome with no marks -> None gain (abandoned dust token)
    m.create_signal_outcome(c, fomo_event_id=eid, source_user_id=uid, handle=uid, mint="D" + eid,
                            ticker="T", buy_ts="2026-07-07T10:00:00Z", ref_price=0.005,
                            their_size_usd=3000.0, now_iso="2026-07-07T10:00:00Z")
    m.save_outcome(c, eid, marks={}, max_gain_pct=None, peak_horizon_s=None, last_price=None,
                   n_marks=0, status="matured", now_iso="2026-07-07T11:00:00Z")


def test_profile_n_signals_excludes_null_gain_dust():
    # 2 real-gain outcomes + 2 abandoned dust for u9: n_signals=2 (real gains only), n_matured=4
    # — dust must not pad the sample past the thin-n grey gate.
    c = _conn()
    _matured(c, "u9", "a", 0.005, 30.0, {60: {"price": 0.0065}})
    _matured(c, "u9", "b", 0.005, 50.0, {60: {"price": 0.0075}})
    _dust(c, "u9", "d1"); _dust(c, "u9", "d2")
    p = m.trader_pump_profile(c, min_signals=1)[0]
    assert p["n_signals"] == 2 and p["n_matured"] == 4


def test_profile_respects_min_signals():
    c = _conn()
    _matured(c, "u2", "a", 0.005, 40.0, {60: {"price": 0.007}})
    _matured(c, "u2", "b", 0.005, 60.0, {60: {"price": 0.008}})
    assert m.trader_pump_profile(c, min_signals=3) == []       # only 2 matured
    assert len(m.trader_pump_profile(c, min_signals=2)) == 1


def test_profile_early_move_from_marks():
    c = _conn()
    # 1m gain = (0.006/0.005-1)*100 = 20; another with 1m gain 40 -> median 30
    _matured(c, "u3", "a", 0.005, 20.0, {60: {"price": 0.006, "age_s": 61}})
    _matured(c, "u3", "b", 0.005, 40.0, {60: {"price": 0.007, "age_s": 61}})
    p = m.trader_pump_profile(c, min_signals=1)[0]
    assert abs(p["median_1m_gain_pct"] - 30.0) < 1e-6


def test_profile_can_be_bounded_to_requested_wallets():
    c = _conn()
    _matured(c, "wanted", "a", 0.005, 20.0, {60: {"price": 0.006}})
    _matured(c, "unrelated", "b", 0.005, 900.0, {60: {"price": 0.05}})

    rows = m.trader_pump_profile(c, min_signals=1, source_user_ids={"wanted"})

    assert [row["source_user_id"] for row in rows] == ["wanted"]
    assert m.trader_pump_profile(c, min_signals=1, source_user_ids=set()) == []
