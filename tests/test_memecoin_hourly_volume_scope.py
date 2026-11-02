"""The hourly floor applies below the m5 market-cap boundary."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


@pytest.fixture
def hourly_floor(monkeypatch):
    for name, value in {
        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0,
        "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.01,
        "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED": True,
        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 2_000_000.0,
        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset(),
        "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True,
        "MEMECOIN_REAL_MIN_TXNS_M5": 0,
        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0,
    }.items():
        monkeypatch.setattr(R.cfg, name, value)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *args: 0.0)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *args, **kwargs: None)


@pytest.mark.parametrize("mcap,boundary", [(79_675.43995664542, 2_000_000),
                                         (2_000_000, 2_000_000), (79_675, 0), (None, 2_000_000)])
def test_hourly_floor_does_not_depend_on_m5_scope(hourly_floor, monkeypatch, mcap, boundary):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", boundary)
    active, _, _, kwargs = R.volume_gate_args(
        {}, mcap=mcap, network_id="solana", coin_band="micro", source_user_id="u1", signal_usd=698.6)
    allowed, need, reason = R.volume_alive_decision(22.08, mcap, **kwargs)
    assert active is False
    assert allowed is False
    assert need == pytest.approx(mcap * 0.01 if mcap else 3000)
    assert reason == ("turnover_under_floor" if mcap else "under_abs_floor")
    assert kwargs["m5_required"] is False


@pytest.mark.parametrize("mode,m5_floor,exemption,blocked", [
    ("follow_activity", 0.0, None, True),
    ("follow_activity", 0.1, "m5_turnover_under_floor", True),
    ("follow_activity", 0.0, "turnover_under_floor", False),
    ("manual", 0.0, None, False),
])
def test_incident_refuses_before_real_entry(hourly_floor, monkeypatch, mode, m5_floor, exemption, blocked):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Tekkerrss")
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *args, **kwargs: frozenset([exemption] if exemption else []))
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: m5_floor)
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *args, **kwargs: reached.append(True) or {"status": "test_reached"})
    now = "2026-09-16T07:45:21.226799+00:00"
    event = SimpleNamespace(
        mint="B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6", ticker="POKEMON",
        source_user_id="u1", source_handle="Tekkerrss", network_id="solana", side="buy",
        fomo_event_id="incident-58593", created_at=now, their_price=8.524864677105648e-05,
        their_size_usd=698.6, signal_source=mode, is_first_buy=True)
    result = R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: now, trade_size=175, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.43995664542, "assumed_micro": False,
            "consensus_n_60m": 1, "market_stats": {"price": event.their_price,
                "market_cap": 79704, "volume_h1": 22.08,
                "volume_m5": 22.08 if m5_floor else None, "liquidity": 26573.83}})
    if blocked:
        assert result["status"] == "rejected", result
        assert "turnover_under_floor" in result["reason"]
        assert "h1 organic volume $22 under floor $797" in result["reason"]
        assert reached == []
        assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
        inputs = json.loads(result["gate_inputs_json"])
        assert inputs["required_usd"] == pytest.approx(796.7543995664542)
    else:
        assert result == {"status": "test_reached"}
        assert reached == [True]
    conn.close()


def test_revival_uses_hourly_floor_and_preserves_missing_data_policy(hourly_floor):
    event = SimpleNamespace(their_market_cap=79_675.43995664542, their_size_usd=698.6, source_user_id="u1")
    assert not R.revival_watch_volume_cleared({"volume_h1": 22.08}, event, network_id="solana", coin_band="micro")
    assert R.revival_watch_volume_cleared({"volume_h1": 2000}, event, network_id="solana", coin_band="micro")
    assert R.revival_watch_volume_cleared({}, event, network_id="solana", coin_band="micro")


def test_sol_small_coin_hourly_floor_switch_is_chain_and_band_scoped(hourly_floor, monkeypatch):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED", False, raising=False)
    for chain, band, mcap, expected_floor, expected_allowed in (
        ("solana", "nano", 23_732.0, 0.0, True),
        ("solana", "micro", 79_675.0, 0.0, True),
        ("solana", "small", 300_000.0, 3_000.0, False),
        ("solana", "nano", None, 3_000.0, False),
        ("solana", "nano", 0.0, 3_000.0, False),
        ("4663", "nano", 23_732.0, 3_000.0, True),
    ):
        _, _, _, kwargs = R.volume_gate_args(
            {"volume_m5": 201.35}, mcap=mcap, network_id=chain, coin_band=band,
            source_user_id="u1", signal_usd=800.0)
        assert kwargs["abs_floor"] == expected_floor
        assert kwargs["turnover_floor"] == (0.0 if expected_floor == 0 else 0.01)
        allowed, _, reason = R.volume_alive_decision(1_154.56, mcap, **kwargs)
        assert allowed == expected_allowed, (chain, band, reason)
