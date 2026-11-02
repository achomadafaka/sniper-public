"""Tests for scripts/execution_profile.py — Phase C (aggregation + calibration)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.execution_profile import (
    CALIB_DRIFT_MIN_N,
    SAMPLE_TIER_HIGH,
    SAMPLE_TIER_MEDIUM,
    FillResult,
    _aggregate,
    _calibration_signature,
    _confidence_tier,
    _dist_summary,
    _select_buys,
    compute_execution_profile,
    fill_result_from_probe_dict,
)

FIXTURE = Path(__file__).parent / "fixtures" / "execution_profile" / "arb_probe.json"


# ---------------------------------------------------------------------------
# _dist_summary
# ---------------------------------------------------------------------------


def test_dist_summary_empty_returns_null_dict():
    d = _dist_summary([])
    assert d["n"] == 0
    assert d["mean"] is None
    assert d["median"] is None
    assert d["stdev"] is None


def test_dist_summary_single_value_has_stdev_zero():
    d = _dist_summary([0.5])
    assert d["n"] == 1
    assert d["mean"] == 0.5
    assert d["median"] == 0.5
    assert d["stdev"] == 0.0


def test_dist_summary_rejects_none_entries():
    d = _dist_summary([0.1, None, 0.3])
    assert d["n"] == 2
    assert d["mean"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# _confidence_tier
# ---------------------------------------------------------------------------


def test_confidence_tier_boundaries():
    # both populated → min wins
    assert _confidence_tier(29, 500) == "unknown"
    assert _confidence_tier(SAMPLE_TIER_MEDIUM, 500) == "medium"
    assert _confidence_tier(99, 500) == "medium"
    assert _confidence_tier(SAMPLE_TIER_HIGH, 500) == "high"


def test_confidence_tier_single_side_populated_uses_max():
    assert _confidence_tier(0, 100) == "high"
    assert _confidence_tier(50, 0) == "medium"
    assert _confidence_tier(0, 0) == "unknown"


# ---------------------------------------------------------------------------
# _calibration_signature
# ---------------------------------------------------------------------------


def test_calibration_signature_pass():
    skew = {"n": 30, "mean": 0.01, "median": 0.005}
    drift = {"n": 20, "median": -0.005}
    sig = _calibration_signature(skew, drift)
    assert sig["central_tendency_near_zero"] is True
    assert sig["drift_near_zero"] is True


def test_calibration_signature_fails_on_wide_mean():
    skew = {"n": 30, "mean": 0.10, "median": 0.005}
    drift = {"n": 20, "median": 0.0}
    sig = _calibration_signature(skew, drift)
    assert sig["central_tendency_near_zero"] is False


def test_calibration_signature_fails_on_wide_median():
    skew = {"n": 30, "mean": 0.0, "median": 0.05}
    drift = {"n": 20, "median": 0.0}
    sig = _calibration_signature(skew, drift)
    assert sig["central_tendency_near_zero"] is False


def test_calibration_signature_drift_none_when_sample_too_small():
    skew = {"n": 30, "mean": 0.0, "median": 0.0}
    drift = {"n": CALIB_DRIFT_MIN_N - 1, "median": 0.5}  # value irrelevant
    sig = _calibration_signature(skew, drift)
    assert sig["drift_near_zero"] is None


def test_calibration_signature_central_none_when_empty_skew():
    skew = {"n": 0, "mean": None, "median": None}
    drift = {"n": 20, "median": 0.0}
    sig = _calibration_signature(skew, drift)
    assert sig["central_tendency_near_zero"] is None


# ---------------------------------------------------------------------------
# _aggregate gating rules
# ---------------------------------------------------------------------------


def _fill(
    *,
    fill_price=0.5,
    ref_prec=0.5,
    ref_tape=None,
    tape_n=0,
    drift_1h=None,
    drift_6h=None,
    drift_24h=None,
    in_play=False,
    thin_market=None,
    resolution_ts_unknown=False,
) -> FillResult:
    if thin_market is None:
        thin_market = tape_n < 3
    return FillResult(
        trade_ts=1000,
        condition_id="cid",
        asset="asset",
        title="t",
        fill_price=fill_price,
        ref_preceding=ref_prec,
        ref_tape=ref_tape,
        tape_n=tape_n,
        drift_1h=drift_1h,
        drift_6h=drift_6h,
        drift_24h=drift_24h,
        in_play=in_play,
        thin_market=thin_market,
        resolution_ts_unknown=resolution_ts_unknown,
    )


def test_aggregate_in_play_fills_excluded_from_skew_kept_for_drift():
    fills = [
        _fill(fill_price=0.50, ref_prec=0.50, tape_n=10, drift_1h=0.02, in_play=True),
        _fill(fill_price=0.40, ref_prec=0.45, tape_n=10, drift_1h=0.01, in_play=False),
    ]
    agg = _aggregate(fills)
    # skew gets only the non-in-play fill
    assert agg["skew_vs_preceding_bar"]["n"] == 1
    # drift_1h gets both
    assert agg["drift_1h"]["n"] == 2
    assert agg["n_trades_in_play"] == 1
    assert agg["data_quality"]["in_play_excluded_from_skew"] == 1


def test_aggregate_thin_market_fills_excluded_from_skew():
    fills = [
        _fill(fill_price=0.50, ref_prec=0.50, tape_n=2),  # thin
        _fill(fill_price=0.40, ref_prec=0.45, tape_n=5),  # liquid
    ]
    agg = _aggregate(fills)
    assert agg["skew_vs_preceding_bar"]["n"] == 1
    assert agg["data_quality"]["thin_market_excluded_from_skew"] == 1


def test_aggregate_tape_skew_requires_tape_n_ge_3():
    fills = [
        _fill(fill_price=0.50, ref_prec=0.50, ref_tape=0.48, tape_n=2),  # dropped
        _fill(fill_price=0.40, ref_prec=0.45, ref_tape=0.42, tape_n=3),  # kept
    ]
    agg = _aggregate(fills)
    assert agg["skew_vs_tape_5min"]["n"] == 1


def test_aggregate_empty_fills_returns_insufficient():
    agg = _aggregate([])
    assert agg["status"] == "insufficient_sample"


def test_aggregate_sets_confidence_tier_and_status():
    # 30 good fills → medium, status populated
    fills = [_fill(fill_price=0.50, ref_prec=0.51, tape_n=10, drift_1h=0.01) for _ in range(30)]
    agg = _aggregate(fills)
    assert agg["confidence_tier"] == "medium"
    assert agg["status"] == "populated"


def test_aggregate_insufficient_tier_marks_insufficient_status():
    fills = [_fill(fill_price=0.50, ref_prec=0.51, tape_n=10, drift_1h=0.01)]
    agg = _aggregate(fills)
    assert agg["confidence_tier"] == "unknown"
    assert agg["status"] == "insufficient_sample"


def test_aggregate_coverage_ratios():
    fills = [
        _fill(fill_price=0.5, ref_prec=0.5, tape_n=5, drift_1h=0.01, drift_6h=0.02),
        _fill(fill_price=0.4, ref_prec=None, tape_n=5, drift_1h=None, drift_6h=0.03),
    ]
    agg = _aggregate(fills)
    assert agg["coverage"]["preceding_bar"] == 0.5
    assert agg["coverage"]["drift_1h"] == 0.5
    assert agg["coverage"]["drift_6h"] == 1.0


# ---------------------------------------------------------------------------
# M6 — drifts_*_clusters parallel emission
# ---------------------------------------------------------------------------


def _fill_with_cid(cid: str, **kwargs) -> FillResult:
    """_fill helper that overrides condition_id."""
    f = _fill(**kwargs)
    f.condition_id = cid
    return f


def test_aggregate_emits_drifts_clusters_parallel_to_value_lists():
    """drifts_*_clusters must be the same length as drifts_* and aligned
    1:1 (index i in clusters is the condition_id of the fill that produced
    drifts_*[i])."""
    fills = [
        _fill_with_cid("cidA", fill_price=0.50, ref_prec=0.51, tape_n=5,
                       drift_1h=0.01, drift_6h=0.03, drift_24h=0.05),
        _fill_with_cid("cidA", fill_price=0.40, ref_prec=0.45, tape_n=5,
                       drift_1h=0.02, drift_6h=0.04, drift_24h=None),
        _fill_with_cid("cidB", fill_price=0.60, ref_prec=0.58, tape_n=5,
                       drift_1h=None, drift_6h=0.06, drift_24h=0.08),
    ]
    agg = _aggregate(fills)
    assert len(agg["drifts_1h"]) == len(agg["drifts_1h_clusters"]) == 2
    assert agg["drifts_1h_clusters"] == ["cidA", "cidA"]
    assert len(agg["drifts_6h"]) == len(agg["drifts_6h_clusters"]) == 3
    assert agg["drifts_6h_clusters"] == ["cidA", "cidA", "cidB"]
    assert len(agg["drifts_24h"]) == len(agg["drifts_24h_clusters"]) == 2
    assert agg["drifts_24h_clusters"] == ["cidA", "cidB"]


def test_aggregate_drifts_clusters_skip_nulls_consistently():
    """Fills with drift_Xh=None contribute neither a value nor a cluster id."""
    fills = [
        _fill_with_cid("cidA", fill_price=0.5, ref_prec=0.5, tape_n=5,
                       drift_1h=None, drift_6h=0.02),
        _fill_with_cid("cidB", fill_price=0.4, ref_prec=0.45, tape_n=5,
                       drift_1h=0.03, drift_6h=None),
    ]
    agg = _aggregate(fills)
    assert agg["drifts_1h"] == [0.03]
    assert agg["drifts_1h_clusters"] == ["cidB"]
    assert agg["drifts_6h"] == [0.02]
    assert agg["drifts_6h_clusters"] == ["cidA"]


# ---------------------------------------------------------------------------
# Arb fixture replay — spec §10.4 acceptance test
# ---------------------------------------------------------------------------


def test_arb_probe_replay_reproduces_committed_central_tendency():
    """Load committed arb fixture and replay through Phase C aggregator.

    The fixture's summary.skew_vs_preceding_bar numbers include both in-play
    and liquid-market fills (the fixture predates the gates). We assert
    that the ungated replay reproduces the committed numbers. Post-gate
    central tendency is asserted separately in test_*_after_gates.
    """
    payload = json.loads(FIXTURE.read_text())
    probes = list(payload.values())[0]["probes"]
    committed = list(payload.values())[0]["summary"]

    # Rehydrate with in_play=False for all (fixture doesn't carry it).
    fills = [fill_result_from_probe_dict(p) for p in probes]
    # The fixture's summary counts 40 fills in skew_vs_preceding_bar; that
    # matches an aggregator that treats all fills as non-in-play. Replay
    # to confirm our helpers reproduce the exact numbers.
    skew_values = [
        f.fill_price - f.ref_preceding
        for f in fills
        if f.ref_preceding is not None
    ]
    replay = _dist_summary(skew_values)
    assert replay["n"] == committed["skew_vs_preceding_bar"]["n"]
    assert replay["mean"] == pytest.approx(
        committed["skew_vs_preceding_bar"]["mean"], abs=1e-4
    )
    assert replay["median"] == pytest.approx(
        committed["skew_vs_preceding_bar"]["median"], abs=1e-4
    )


def test_arb_probe_replay_calibration_passes_after_thin_market_gate():
    """With the thin-market gate applied (tape_n < 3 excluded), the arb
    signature should read central-tendency-near-zero per the arb-control memo.
    """
    payload = json.loads(FIXTURE.read_text())
    probes = list(payload.values())[0]["probes"]
    fills = [fill_result_from_probe_dict(p) for p in probes]
    # Stamp in_play flags by market category (crude: LoL/Dota esports in-play
    # in the fixture are the two huge outliers documented in the arb memo).
    esports_prefixes = ("LoL:", "Dota 2:")
    for f in fills:
        if any(f.title.startswith(p) for p in esports_prefixes):
            f.in_play = True

    agg = _aggregate(fills)
    # Thin-market gate removes the 0-tape fills; in-play gate removes esports.
    assert agg["skew_vs_preceding_bar"]["n"] > 0
    sig = agg["calibration_signature"]
    # Arb control memo: median +0.005, mean +0.008 — both within thresholds.
    # After gates the numbers should be even tighter.
    assert sig["central_tendency_near_zero"] is True


# ---------------------------------------------------------------------------
# _select_buys
# ---------------------------------------------------------------------------


def test_select_buys_filters_non_trade_rows():
    rows = [
        {"type": "REDEEM", "side": "SELL", "conditionId": "c", "asset": "a",
         "timestamp": 100, "price": 0.5},
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": 200, "price": 0.5},
    ]
    assert len(_select_buys(rows)) == 1


def test_select_buys_filters_sells():
    rows = [
        {"type": "TRADE", "side": "SELL", "conditionId": "c", "asset": "a",
         "timestamp": 100, "price": 0.5},
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": 200, "price": 0.5},
    ]
    assert len(_select_buys(rows)) == 1


def test_select_buys_rejects_out_of_range_prices():
    rows = [
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": 100, "price": 0.0},
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": 200, "price": 1.0},
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": 300, "price": 0.5},
    ]
    fills = _select_buys(rows)
    assert [f[0] for f in fills] == [300]


def test_select_buys_sorts_ascending_and_caps():
    rows = [
        {"type": "TRADE", "side": "BUY", "conditionId": "c", "asset": "a",
         "timestamp": ts, "price": 0.5}
        for ts in (300, 100, 200)
    ]
    fills = _select_buys(rows, max_trades=2)
    assert [f[0] for f in fills] == [100, 200]


# ---------------------------------------------------------------------------
# compute_execution_profile end-to-end (mocked client)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_compute_execution_profile_populates_with_mock_client(monkeypatch):
    """End-to-end smoke with a mocked PolymarketClient — no network."""
    client = MagicMock()
    # 30 fills spaced 60s apart. For every fill we need (a) a preceding bar
    # within 30 min and (b) a bar within 300s of `trade_ts + 3600` for
    # drift_1h to register. Populate bars every 60s from -60 to +3600+1800.
    base = 1_000_000
    bars = []
    for dt in range(-60, 3600 + 1800, 60):
        bars.append({"timestamp": base + dt, "price": 0.50 + (0.02 if dt >= 3600 else 0.0)})
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset", "price": 0.50, "side": "BUY"}
    ] * 5  # tape_n = 5

    activity = [
        {"type": "TRADE", "side": "BUY", "conditionId": "cid",
         "asset": "asset", "timestamp": base + i * 60,
         "price": 0.50, "title": "Fake Market"}
        for i in range(30)
    ]

    # No network: force gamma fallback to return None instead of hitting API
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma",
        lambda cid: None,
    )

    out = compute_execution_profile(
        "0xWALLET",
        activity,
        client=client,
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 30},
        returns={"resolved_position_count": 10},
        db_path="/tmp/nonexistent/__no__.db",
    )
    assert out["status"] == "populated"
    assert out["confidence_tier"] == "medium"
    assert out["n_trades_skew"] == 30
    assert out["skew_vs_preceding_bar"]["median"] == 0.0
    assert out["drift_1h"]["median"] == pytest.approx(0.02)


def test_compute_execution_profile_api_failure_when_no_client():
    out = compute_execution_profile(
        "0xWALLET",
        [],
        client=None,
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 30},
        returns={"resolved_position_count": 10},
    )
    assert out["status"] == "api_failure"
    assert out["reason"] == "no_client_provided"


def test_compute_execution_profile_insufficient_when_no_buys():
    client = MagicMock()
    # Activity has no BUYs
    activity = [{"type": "REDEEM", "side": "SELL", "conditionId": "c",
                 "asset": "a", "timestamp": 100, "price": 0.5}]
    out = compute_execution_profile(
        "0xWALLET",
        activity,
        client=client,
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 30},
        returns={"resolved_position_count": 10},
    )
    assert out["status"] == "insufficient_sample"
    assert out["reason"] == "no_qualifying_buys"
