"""Forward pump capture: fill horizon marks as they come due, recompute max-gain, mature."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_pump as pump

H = [60, 300, 900, 3600]


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _outcome(c, eid="e1", ref=0.005, buy="2026-07-07T10:00:00Z"):
    m.create_signal_outcome(c, fomo_event_id=eid, source_user_id="u1", handle="alice", mint="MINT",
                            ticker="T", buy_ts=buy, ref_price=ref, their_size_usd=3000.0, now_iso=buy)


def _batch(price):
    return lambda mints: {mn: {"price": price} for mn in mints}


def test_due_marks_only_after_horizon_elapsed():
    o = {"buy_ts": "2026-07-07T10:00:00Z", "marks": {}}
    assert pump.due_marks(o, pump._epoch("2026-07-07T10:00:30Z"), H) == []          # 30s: none due
    assert pump.due_marks(o, pump._epoch("2026-07-07T10:01:05Z"), H) == [60]        # 65s: 60 due
    assert pump.due_marks(o, pump._epoch("2026-07-07T10:05:05Z"), H) == [60, 300]   # 305s: 60,300


def test_capture_fills_due_mark_and_computes_gain():
    c = _conn(); _outcome(c, ref=0.005)
    # +65s, price pumped to 0.0075 -> +50%
    r = pump.pump_capture_pass(c, "2026-07-07T10:01:05Z", _batch(0.0075), horizons=H, abandon_s=1800)
    assert r["filled"] == 1
    o = dict(c.execute("SELECT * FROM memecoin_signal_outcomes WHERE fomo_event_id='e1'").fetchone())
    assert abs(o["max_gain_pct"] - 50.0) < 1e-6 and o["peak_horizon_s"] == 60
    assert o["n_marks"] == 1 and o["status"] == "open"


def test_missing_quote_leaves_mark_for_retry():
    c = _conn(); _outcome(c)
    r = pump.pump_capture_pass(c, "2026-07-07T10:01:05Z", lambda mints: {}, horizons=H, abandon_s=1800)
    o = dict(c.execute("SELECT * FROM memecoin_signal_outcomes WHERE fomo_event_id='e1'").fetchone())
    assert o["n_marks"] == 0 and o["status"] == "open"   # nothing filled, still open to retry


def test_matures_when_all_marks_filled():
    c = _conn(); _outcome(c, ref=0.005)
    # long after the last horizon -> all 4 marks fill at once -> matured
    pump.pump_capture_pass(c, "2026-07-07T11:05:00Z", _batch(0.006), horizons=H, abandon_s=1800)
    o = dict(c.execute("SELECT * FROM memecoin_signal_outcomes WHERE fomo_event_id='e1'").fetchone())
    assert o["n_marks"] == 4 and o["status"] == "matured"
    assert abs(o["max_gain_pct"] - 20.0) < 1e-6


def test_recompute_ignores_unit_mismatch_marks():
    # a mark whose price/ref is 1e7 (multichain unit mismatch) must be excluded from max_gain,
    # while a sane 2x mark counts. ref 0.005: sane 0.01 (+100%), garbage 50000 (1e7 ratio).
    mg, ph, last, n = pump.recompute(0.005, {60: {"price": 0.01}, 300: {"price": 50000.0}})
    assert abs(mg - 100.0) < 1e-6 and ph == 60      # garbage 300-mark ignored, sane 60-mark wins
    # all-garbage HIGH -> no gain (None), not a fabricated moonshot
    mg2, ph2, _l, _n = pump.recompute(0.005, {60: {"price": 90000.0}})
    assert mg2 is None and ph2 is None


def test_recompute_counts_rug_as_loss_not_dropped():
    # REGRESSION (math review): a rugged token (>99.99% loss) must be COUNTED as ~−99.99%,
    # not dropped as a bad quote (dropping the worst outcomes = optimism bias). ref 0.005,
    # price 1e-9 -> ratio 2e-7 < 1e-4 -> clamp to −99.99%.
    mg, ph, _l, _n = pump.recompute(0.005, {60: {"price": 1e-9}})
    assert mg is not None and abs(mg - (-99.99)) < 0.01


def test_matures_overdue_even_with_missing_marks():
    c = _conn(); _outcome(c)
    # final horizon (3600) overdue by > abandon_s, but quotes keep failing -> mature with what we have
    pump.pump_capture_pass(c, "2026-07-07T12:00:00Z", lambda mints: {}, horizons=H, abandon_s=1800)
    o = dict(c.execute("SELECT * FROM memecoin_signal_outcomes WHERE fomo_event_id='e1'").fetchone())
    assert o["status"] == "matured"    # abandoned (dust/dead token), not stuck open forever
