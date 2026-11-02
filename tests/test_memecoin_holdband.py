"""Hold-band forward-study read-out: copy-PnL by trader hold band / discovery pool / arm."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.analysis.memecoin_holdband import band_of, hold_band_readout, gates_met, decision


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _band_trader(c, uid, hold_s):   # analytics so open_position freezes the band
    m.upsert_trader_analytics(c, {"source_user_id": uid, "handle": uid, "median_hold_s": hold_s})


def _closed(c, uid, pnl, arm="control", is_open=False):
    pid = m.open_position(c, source_user_id=uid, source_handle=uid, mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00Z",
                          strategy=arm)
    if not is_open:
        m.close_position(c, pid, "2026-07-07T10:10:00Z", pnl, exit_reason="stop_loss")
    return pid


def test_band_of_cutoffs():
    assert band_of(None) == "unknown"
    assert band_of(600) == "fast"
    assert band_of(7200) == "med"
    assert band_of(200000) == "holder"


def test_readout_aggregates_by_band():
    c = _conn()
    # fast trader (hold 600) + holder (hold 200000); analytics supply the band
    m.upsert_trader_analytics(c, {"source_user_id": "fast1", "handle": "f", "median_hold_s": 600.0})
    m.upsert_trader_analytics(c, {"source_user_id": "hold1", "handle": "h", "median_hold_s": 200000.0})
    _closed(c, "fast1", 10.0); _closed(c, "fast1", 30.0)      # fast: mean +20
    _closed(c, "hold1", -5.0)                                  # holder: mean -5
    _closed(c, "nofollow", 99.0)                               # unknown band (no analytics)
    r = hold_band_readout(c, now_iso="2026-07-07T12:00:00Z")
    assert r["by_band"]["fast"]["n"] == 2
    assert abs(r["by_band"]["fast"]["mean"] - 20.0) < 1e-6
    assert r["by_band"]["holder"]["n"] == 1 and abs(r["by_band"]["holder"]["mean"] + 5.0) < 1e-6
    assert r["by_band"]["unknown"]["n"] == 1


def test_readout_splits_by_source_and_arm():
    c = _conn()
    m.upsert_trader_analytics(c, {"source_user_id": "fast1", "handle": "f", "median_hold_s": 600.0})
    m.add_wallet(c, "fast1", "f"); m.set_wallet_discovery_source(c, "fast1", "leaderboard")
    _closed(c, "fast1", 10.0, arm="momentum_a")
    _closed(c, "fast1", 20.0, arm="control")
    r = hold_band_readout(c, now_iso="2026-07-07T12:00:00Z")
    assert r["by_source"]["leaderboard"]["n"] == 2
    assert r["by_band_arm"]["fast|momentum_a"]["n"] == 1


def test_gates_not_met_below_thresholds():
    c = _conn()
    m.upsert_trader_analytics(c, {"source_user_id": "fast1", "handle": "f", "median_hold_s": 600.0})
    _closed(c, "fast1", 10.0)
    r = hold_band_readout(c, now_iso="2026-07-10T00:00:00Z")
    met, reasons = gates_met(r, today="2026-07-10", min_date="2026-07-21")
    assert met is False and any("calendar" in x or "band" in x for x in reasons)


def test_frozen_band_survives_analytics_rewrite():
    # band is FROZEN at open; re-running the analyzer (changing median_hold_s) must NOT
    # reclassify an already-closed position.
    c = _conn()
    _band_trader(c, "t1", 600.0)                 # fast at open time
    _closed(c, "t1", 10.0)
    m.upsert_trader_analytics(c, {"source_user_id": "t1", "handle": "t1", "median_hold_s": 200000.0})  # now "holder"
    r = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    assert r["by_band"].get("fast", {}).get("n") == 1     # still fast (frozen), not holder
    assert "holder" not in r["by_band"]


def test_open_positions_go_to_censoring_diagnostic_not_closed_agg():
    c = _conn()
    _band_trader(c, "h1", 200000.0)
    _closed(c, "h1", -5.0)                        # closed holder
    _closed(c, "h1", 0.0, is_open=True)          # still-open holder (censored)
    r = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    assert r["by_band"]["holder"]["n"] == 1      # only the closed one aggregated
    assert r["by_band_open"]["holder"] == 1      # the open one shows in the censoring diagnostic


def test_clustered_ci_needs_two_traders():
    c = _conn()
    _band_trader(c, "f1", 600.0)
    _closed(c, "f1", 10.0, arm="momentum_a"); _closed(c, "f1", 20.0, arm="momentum_a")
    r = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    assert r["by_band_arm"]["fast|momentum_a"]["ci_low"] is None    # 1 trader -> no clustered CI
    _band_trader(c, "f2", 700.0); _closed(c, "f2", 12.0, arm="momentum_a")
    r2 = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    assert r2["by_band_arm"]["fast|momentum_a"]["ci_low"] is not None   # 2 traders -> CI exists


def test_decision_executes_preregistered_rule():
    c = _conn()
    for i in range(5):                            # 5 fast traders, all +50 in Arm A
        _band_trader(c, f"f{i}", 600.0)
        for _ in range(4):
            _closed(c, f"f{i}", 50.0, arm="momentum_a")
    for i in range(5):                            # 5 holder traders, all -50 in Arm A
        _band_trader(c, f"h{i}", 200000.0)
        for _ in range(4):
            _closed(c, f"h{i}", -50.0, arm="momentum_a")
    r = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    d = decision(r)
    assert d["h1"] == "blocked_protocol" and d["direction"] is None
    # and the trader floor in gates is now satisfied for the Arm-A cells
    met, reasons = gates_met(r, today="2026-07-22")
    assert not any("n_traders" in x for x in reasons)   # 5 traders each -> trader floor ok


def test_gates_need_trader_floor():
    c = _conn()
    # 40 fast positions but from ONE trader -> position gate ok, trader floor fails
    _band_trader(c, "solo", 600.0)
    for _ in range(40):
        _closed(c, "solo", 5.0, arm="momentum_a")
    r = hold_band_readout(c, now_iso="2026-07-22T00:00:00Z")
    met, reasons = gates_met(r, today="2026-07-22")
    assert met is False and any("n_traders" in x for x in reasons)
