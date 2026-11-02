"""The Solana micro hourly turnover floor (spec 2026-09-28-memecoin-solana-micro-h1-turnover-floor-design.md).

The floor reads the gate's own ORGANIC h1 (the source's buy subtracted) over facts.effective_mcap, at
0.143 on activation: that refuses exactly the 27 micro fills Gamma's evidence graded (-$139.31). The
rows below are real Solana micro decisions from logs/acceptance/move_possible.jsonl (09-14..09-28):
(volume_h1, source_size_usd, effective_mcap)."""
import pytest

from sniper.services import memecoin_real_runner as R

BELOW = (16261.82, 364.172196, 117620.0)            # 36760094: organic 0.1352, filled
ABOVE = (12315.91, 1199.05, 76270.444513)           # 1597cd0f: organic 0.1458, +$62.74 (16.5% refuses it)
RAW_PASSES = (21316.81, 2468.178963, 138063.289223)  # f9fe55a5: raw 0.1544 >= 0.143, organic 0.1365


@pytest.fixture
def live_like(monkeypatch):
    """Production's hourly posture on Solana nano/micro: both global hourly legs off for the band."""
    for name, value in {"MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED": False,
                        "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.0,
                        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0,
                        "MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1": 0.143,
                        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 250_000.0,
                        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset()}.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *a: 0.0)
    monkeypatch.setattr(R.cfg, "micro_min_volume_m5_usd_for", lambda *a: 0.0)


def decide(row, band="micro", network="solana"):
    h1, src, mcap = row
    _, _, _, kw = R.volume_gate_args({}, mcap=mcap, network_id=network, coin_band=band,
                                     source_user_id="u1", signal_usd=src)
    return R.volume_alive_decision(h1, mcap, **kw), kw


@pytest.mark.parametrize("row,allowed", [(BELOW, False), (ABOVE, True), (RAW_PASSES, False)])
def test_the_micro_floor_judges_organic_h1_over_effective_mcap(live_like, row, allowed):
    (ok, need, reason), kw = decide(row)
    assert kw["turnover_floor"] == pytest.approx(0.143) and kw["abs_floor"] == 0.0
    assert ok is allowed
    if not allowed:
        assert reason == "turnover_under_floor" and need == pytest.approx(row[2] * 0.143)


@pytest.mark.parametrize("band,network,mcap", [("nano", "solana", 20_000.0), ("small", "solana", 400_000.0),
                                               ("micro", "4663", 60_000.0), ("micro", "solana", None)])
def test_the_floor_is_scoped_to_solana_micro_with_a_known_mcap(live_like, band, network, mcap):
    _, _, _, kw = R.volume_gate_args({}, mcap=mcap, network_id=network, coin_band=band,
                                     source_user_id="u1", signal_usd=100.0)
    assert kw["turnover_floor"] == 0.0


def test_zero_is_todays_behaviour(live_like, monkeypatch):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1", 0.0, raising=False)
    (ok, need, reason), kw = decide(BELOW)
    assert kw["turnover_floor"] == 0.0 and ok is True


def test_a_missing_h1_admits(live_like):
    _, _, _, kw = R.volume_gate_args({}, mcap=BELOW[2], network_id="solana", coin_band="micro",
                                     source_user_id="u1", signal_usd=BELOW[1])
    assert R.volume_alive_decision(None, BELOW[2], **kw)[0] is True


def test_the_default_is_off(tmp_path):
    # A fresh interpreter with the variable unset reads the code default (settings do not load .env).
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1"}
    out = subprocess.run([sys.executable, "-c", "from sniper.settings import memecoin as m; "
                          "print(m.MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1)"],
                         cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip() == "0.0", out.stderr[-300:]


def _hook(monkeypatch, volume_h1, fomo_id):
    import json
    import sqlite3
    from types import SimpleNamespace
    from sniper.persistence import db_memecoin as m
    for name, value in {"MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True, "MEMECOIN_REAL_MIN_TXNS_M5": 0,
                        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
                        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0}.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Tekkerrss")
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.append(True) or {"status": "test_reached"})
    now = "2026-09-16T07:45:21.226799+00:00"
    event = SimpleNamespace(
        mint="B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6", ticker="POKEMON", source_user_id="u1",
        source_handle="Tekkerrss", network_id="solana", side="buy", fomo_event_id=fomo_id,
        created_at=now, their_price=8.524864677105648e-05, their_size_usd=698.6,
        signal_source="follow_activity", is_first_buy=True)
    result = R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: now, trade_size=175, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.43995664542, "assumed_micro": False,
            "consensus_n_60m": 1, "market_stats": {"price": event.their_price, "market_cap": 79704,
                "volume_h1": volume_h1, "volume_m5": 50.0, "liquidity": 26573.83}})
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
    gate = [e for e in json.loads(row[0])["substage_events"] if e["name"] == "volume_gate"] if row else []
    return result, reached, gate


def test_a_missing_hourly_input_admits_as_unknown_and_says_so(live_like, monkeypatch):
    # Alpha 2026-09-28: absent h1 keeps the existing unknown handling (admit) and is recorded.
    result, reached, gate = _hook(monkeypatch, None, "micro-floor-missing")
    assert reached == [True], result
    assert gate and gate[0]["h1_floor_scope"] == "solana_micro" and gate[0]["h1_floor_input"] == "missing"


def test_the_hook_refuses_a_quiet_micro_coin_and_traces_the_scope(live_like, monkeypatch):
    """End to end through real_entry_hook (the incident harness of test_memecoin_hourly_volume_scope):
    a $79,675 micro coin with $22 of hourly volume is refused on the micro floor, before any
    reservation, and the volume_gate substage names the scope."""
    import json
    import sqlite3
    from types import SimpleNamespace
    from sniper.persistence import db_memecoin as m
    for name, value in {"MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True, "MEMECOIN_REAL_MIN_TXNS_M5": 0,
                        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
                        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0}.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Tekkerrss")
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.append(True) or {"status": "test_reached"})
    now = "2026-09-16T07:45:21.226799+00:00"
    event = SimpleNamespace(
        mint="B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6", ticker="POKEMON", source_user_id="u1",
        source_handle="Tekkerrss", network_id="solana", side="buy", fomo_event_id="micro-floor-1",
        created_at=now, their_price=8.524864677105648e-05, their_size_usd=698.6,
        signal_source="follow_activity", is_first_buy=True)
    result = R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: now, trade_size=175, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.43995664542, "assumed_micro": False,
            "consensus_n_60m": 1, "market_stats": {"price": event.their_price, "market_cap": 79704,
                "volume_h1": 22.08, "volume_m5": None, "liquidity": 26573.83}})
    assert result["status"] == "rejected" and "turnover_under_floor" in result["reason"], result
    assert "under floor $11394" in result["reason"] and reached == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
    gate = [e for e in json.loads(row[0])["substage_events"] if e["name"] == "volume_gate"]
    assert gate and gate[0]["h1_floor_scope"] == "solana_micro" and gate[0]["h1_scope"] == "all_market_caps"
    assert gate[0]["h1_floor_input"] == "provider_h1"
