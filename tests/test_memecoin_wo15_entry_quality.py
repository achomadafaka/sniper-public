import sqlite3

import pytest

from sniper.analysis import memecoin_entry_quality as eq
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_v5_replay_gate import FillModel


def _model(impact=0.10):
    return FillModel(
        impact_cap_pct=50.0, haircut_frac=0.0, calibrated=True,
        slippage_curve=((1_000.0, impact),))


def test_candidate_uses_fixed_notional_known_actual_lag_and_fill_modeled_exit():
    positions = [{
        "id": i, "source_user_id": "w", "network_id": "4663", "entry_notional": 100.0,
        "realized_pnl": 20.0, "entry_lag_s": 3.0, "entry_mcap": 100_000.0,
        "is_open": 0,
    } for i in range(20)]

    report = eq.evaluate_wallet_candidate(
        positions, {"4663": _model()}, fixed_notional=50.0, min_closes=20)

    # Actual copied entry already includes the 3s lag. Normalize its +20% return to $50, then
    # value the $60 exit through a 10% calibrated fill penalty: $54 - $50 = $4 per close.
    assert report["n_eligible"] == 20
    assert report["net_fill_modeled_pnl"] == pytest.approx(80.0)
    assert report["median_entry_lag_s"] == 3.0
    assert report["status"] == "READY"
    assert report["governance_authority"] is False


def test_candidate_reports_censoring_missing_lag_and_uncalibrated_chain_without_guessing():
    positions = [
        {"id": 1, "network_id": "4663", "entry_notional": 50, "realized_pnl": 5,
         "entry_lag_s": None, "entry_mcap": 1_000_000, "is_open": 0},
        {"id": 2, "network_id": "solana", "entry_notional": 50, "realized_pnl": 5,
         "entry_lag_s": 2, "entry_mcap": 1_000_000, "is_open": 0},
        {"id": 3, "network_id": "4663", "entry_notional": 50, "realized_pnl": None,
         "entry_lag_s": 2, "entry_mcap": 1_000_000, "is_open": 1},
    ]
    uncalibrated = FillModel(impact_cap_pct=50, haircut_frac=0.1, calibrated=False)

    report = eq.evaluate_wallet_candidate(
        positions, {"4663": _model(), "solana": uncalibrated}, min_closes=1)

    assert report["n_eligible"] == 0
    assert report["n_censored"] == 1
    assert report["n_missing_entry_lag"] == 1
    assert report["n_uncalibrated_fill_model"] == 1
    assert report["net_fill_modeled_pnl"] is None
    assert report["status"] == "BLOCKED_MISSING_EVIDENCE"


def test_candidate_stratifies_chain_band_and_surfaces_negative_decay_for_review_only():
    positions = [
        {"id": 1, "network_id": "4663", "entry_notional": 50, "realized_pnl": -10,
         "entry_lag_s": 1, "entry_mcap": 50_000, "is_open": 0},
        {"id": 2, "network_id": "4663", "entry_notional": 50, "realized_pnl": -10,
         "entry_lag_s": 20, "entry_mcap": 5_000_000, "is_open": 0},
    ]

    report = eq.evaluate_wallet_candidate(
        positions, {"4663": _model(0)}, min_closes=2, min_stratum_closes=1)

    assert {(x["chain"], x["coin_band"]) for x in report["by_chain_band"]} == {
        ("4663", "nano<1M"), ("4663", "mid5-30M")}
    assert report["review_flag"] == "NEGATIVE_CANDIDATE_REVIEW"
    assert report["governance_authority"] is False


def test_trader_performance_displays_candidate_beside_unchanged_alpha_b(monkeypatch):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    cur = c.execute("INSERT INTO memecoin_positions(source_user_id,source_handle,strategy,is_open,"
                    "execution_mode,entry_notional,realized_pnl,opened_at,closed_at) "
                    "VALUES('w','W','alpha_b',0,'paper',50,7,'2026-01-01','2026-01-02')")
    m.insert_fill(c, position_id=cur.lastrowid, kind="entry", price=1.0, qty=50.0,
                  notional=50.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-01-01")
    m.insert_fill(c, position_id=cur.lastrowid, kind="mirror_exit", price=1.14, qty=50.0,
                  notional=57.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-01-02")
    expected = {"w": {"status": "INSUFFICIENT_SAMPLE", "governance_authority": False}}
    monkeypatch.setattr(eq, "wallet_candidates_from_db", lambda conn: expected)

    row = m.trader_performance(c)[0]

    assert row["ab_pnl"] == 7.0
    assert row["entry_quality_candidate"] == expected["w"]
