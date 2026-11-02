"""Probability-model contract + diagnostic baselines."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import probability_model as M


def test_midpoint_baseline_uses_up_midpoint():
    p = M.MarketMidpointBaseline().predict_up_probability({"up_midpoint_probability": 0.62})
    assert p.q_up == 0.62 and abs(p.q_down - 0.38) < 1e-12
    assert p.calibrated is False and p.prediction_status == "diagnostic_only"


def test_midpoint_missing_is_unavailable():
    p = M.MarketMidpointBaseline().predict_up_probability({})
    assert p.prediction_status == "unavailable" and p.q_up is None


def test_threshold_diffusion_flat_is_half_and_above_is_higher():
    flat = M.ThresholdDiffusionV0().predict_up_probability(
        {"chainlink_price_now": 100.0, "price_to_beat": 100.0, "vol_per_s": 0.001,
         "seconds_remaining": 60})
    assert abs(flat.q_up - 0.5) < 1e-9
    above = M.ThresholdDiffusionV0().predict_up_probability(
        {"chainlink_price_now": 100.5, "price_to_beat": 100.0, "vol_per_s": 0.001,
         "seconds_remaining": 60})
    assert above.q_up > 0.5
    assert above.calibrated is False and above.prediction_status == "diagnostic_only"


def test_threshold_diffusion_bounded_and_q_down_complements():
    p = M.ThresholdDiffusionV0().predict_up_probability(
        {"chainlink_price_now": 200.0, "price_to_beat": 100.0, "vol_per_s": 0.0001,
         "seconds_remaining": 1})
    assert 0.0 <= p.q_up <= 1.0 and abs((p.q_up + p.q_down) - 1.0) < 1e-12


def test_threshold_diffusion_insufficient_inputs_unavailable():
    for bad in ({"chainlink_price_now": 100.0, "price_to_beat": 100.0, "vol_per_s": 0.0,
                 "seconds_remaining": 60},
                {"chainlink_price_now": 100.0, "price_to_beat": 100.0, "vol_per_s": 0.001,
                 "seconds_remaining": 0},
                {}):
        assert M.ThresholdDiffusionV0().predict_up_probability(bad).prediction_status == "unavailable"


def test_all_baselines_are_uncalibrated_cannot_activate():
    feats = {"up_midpoint_probability": 0.6, "chainlink_price_now": 100.0, "price_to_beat": 100.0,
             "vol_per_s": 0.001, "seconds_remaining": 60, "legacy_spot_signal": 1}
    for model in M.BASELINES.values():
        assert model.predict_up_probability(feats).calibrated is False


def test_legacy_signal_is_diagnostic_not_a_probability():
    p = M.LegacySpotSignalDiagnostic().predict_up_probability({"legacy_spot_signal": 1})
    assert p.q_up is None and p.calibrated is False           # LONG is never translated to a prob
    assert "legacy_spot_signal=1" in p.reason
