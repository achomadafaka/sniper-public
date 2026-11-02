"""BTC 5m forward paper engine — pure step + kill-criteria gate."""
from __future__ import annotations

import pytest

from sniper.algos.btc5m import forward as FW


def _bars(closes):
    return [{"symbol": "BTCUSDT", "open_ms": i * 300000, "open": c, "high": c + 1, "low": c - 1, "close": c,
             "close_ms": i * 300000 + 299999, "volume": 1.0} for i, c in enumerate(closes)]


CFG = {"signal": "meanrev_zscore", "params": {"n": 5, "entry": 1.5, "exit_z": 1.0}, "per_side": 0.0002}


def test_target_position_uses_signal():
    bars = _bars([100, 100, 100, 100, 100, 80])      # sharp dip -> oversold -> long
    assert FW.target_position(bars, CFG) == 1


def test_step_lag_decides_on_prior_bar_fills_on_last():
    flat = FW.new_state()
    # dip at index 5 (close 80) -> decided on bars[:-1] (ends at the dip); FILLED at bars[-1]=81,
    # NOT at the extreme 80. That one-bar lag is the anti-artifact fix.
    s1, closed1 = FW.step(flat, _bars([100, 100, 100, 100, 100, 80, 81]), CFG)
    assert s1["position"] == 1 and s1["entry_px"] == 81 and closed1 == []


def test_step_opens_then_closes_with_pnl():
    s1 = {"position": 1, "entry_px": 81.0, "entry_ms": 1500000}
    # prices revert to ~mean: decided on bars[:-1] (z back inside exit band) -> close; fill at bars[-1]=100
    s2, closed2 = FW.step(s1, _bars([100, 100, 100, 100, 100, 80, 81] + [100, 100, 100, 100]), CFG)
    assert s2["position"] == 0
    assert len(closed2) == 1
    t = closed2[0]
    assert t["side"] == 1 and t["entry_px"] == 81 and t["exit_px"] == 100
    assert t["pnl_pct"] == pytest.approx((100 / 81 - 1) - 2 * 0.0002)


def test_advance_replays_gap_no_undercount():
    # The naive single-bar poll only looks at the LATEST bar's signal, so a fire+revert that happens
    # BETWEEN infrequent polls is missed. advance() replays every closed bar since `since_ms` so the
    # forward book is correct regardless of poll cadence (and survives a skipped cron run).
    closes = [100.0] * 5 + [80.0, 100.0, 100.0, 100.0, 100.0]   # dip at idx5, reverts after
    bars = _bars(closes)
    flat = FW.new_state()
    # naive poll on only the latest bar -> latest z~0 -> sees nothing, misses the mid-gap dip/revert
    _, closed_latest = FW.step(flat, bars, CFG)
    assert closed_latest == []
    # advance replays the whole gap -> captures the long entry + its exit
    st, closed_adv = FW.advance(flat, bars, since_ms=-1, config=CFG)
    assert len(closed_adv) >= 1
    assert closed_adv[0]["side"] == 1            # longed the dip
    assert st["position"] == 0                    # flat again after the revert


def test_forward_script_bootstraps_state_without_backfilling_paper(monkeypatch):
    import sqlite3
    import db
    import scripts.btc5m_forward as S
    from sniper.algos.btc5m import data as D

    conn = sqlite3.connect(":memory:")
    bars = _bars([100.0] * 20 + [60.0, 100.0] + [100.0] * 10)
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(S.D, "fetch_klines", lambda symbol, limit=300: bars)
    monkeypatch.setattr(S.D, "server_time_ms", lambda: bars[-1]["close_ms"])

    assert S.main([]) == 0
    assert D.load_paper_trades(conn) == []
    assert D.load_state(conn)["last_bar_ms"] == bars[-1]["close_ms"]


def test_advance_since_ms_skips_already_processed_bars():
    bars = _bars([100.0] * 5 + [80.0, 100.0])
    # since_ms at the last bar -> nothing new to process -> no trades, state unchanged
    st, closed = FW.advance({"position": 0, "entry_px": None, "entry_ms": None},
                            bars, since_ms=bars[-1]["close_ms"], config=CFG)
    assert closed == [] and st["position"] == 0


def test_step_no_change_emits_nothing():
    s = {"position": 1, "entry_px": 81.0, "entry_ms": 1500000}
    # still oversold on the decision bar (no revert) -> hold, no closed trade
    s2, closed = FW.step(s, _bars([100, 100, 100, 100, 100, 80, 80, 80]), CFG)
    assert closed == [] and s2["position"] == 1


def test_kill_criteria_not_met_until_enough_trades():
    trades = [{"pnl_pct": 0.01} for _ in range(10)]
    verdict = FW.evaluate(trades, min_trades=200)
    assert verdict["passed"] is False
    assert verdict["n"] == 10
    assert "gathering" in verdict["status"].lower()


def test_kill_criteria_pass_shape():
    trades = [{"pnl_pct": 0.01} for _ in range(150)] + [{"pnl_pct": -0.005} for _ in range(60)]
    verdict = FW.evaluate(trades, min_trades=200)
    assert verdict["n"] == 210
    assert verdict["profit_factor"] > 1.0
    assert isinstance(verdict["passed"], bool)
