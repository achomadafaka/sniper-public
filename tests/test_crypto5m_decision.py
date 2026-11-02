"""Shadow decision engine — fail-closed gates, independent Up/Down, edge selection."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import decision as D
from sniper.algos.polymarket_crypto5m import probability_model as M
from sniper.algos.polymarket_crypto5m.probability_model import ProbabilityPrediction

KNOWN_FEE = {"fee_status": "known", "fee_rate": 0.0}
GOOD_FEATURES = {"feature_status": "ok", "price_to_beat_quality": "validated_rtds_reconstruction"}


def _pred(q_up, *, calibrated, status="ok"):
    return ProbabilityPrediction(q_up=q_up, q_down=None if q_up is None else 1 - q_up,
                                 model_id="m", model_version="v", calibrated=calibrated,
                                 prediction_status=status)


def _ev(**over):
    kw = dict(features=GOOD_FEATURES, prediction=_pred(0.7, calibrated=True), fee=KNOWN_FEE,
              up_asks=[["0.50", "1000"]], down_asks=[["0.50", "1000"]], up_token_id="U",
              down_token_id="D", stake_usdc=10.0, min_order_size=1.0, min_edge=0.0,
              boundary_locked="last_at_or_before")
    kw.update(over)
    return D.evaluate(**kw)


# --- Phase 1A fail-closed (boundary unlocked / uncalibrated) ---------------------------------

def test_boundary_unlocked_is_shadow_prediction_never_entered():
    out = _ev(boundary_locked=None)
    assert out["status"] == "shadow_prediction" and out["reason"] == "boundary_rule_unvalidated"
    assert out["entered"] is False
    assert out["q_up"] == 0.7                              # forecast still recorded for scoring


def test_uncalibrated_model_is_shadow_prediction_not_entered():
    out = _ev(prediction=_pred(0.7, calibrated=False))
    assert out["status"] == "shadow_prediction" and out["reason"] == "probability_uncalibrated"
    assert out["entered"] is False


def test_real_baselines_never_enter():
    feats = {**GOOD_FEATURES, "up_midpoint_probability": 0.6}
    pred = M.MarketMidpointBaseline().predict_up_probability(feats)
    out = _ev(prediction=pred, boundary_locked="last_at_or_before")
    assert out["entered"] is False                         # calibrated=False -> shadow only
    assert out["status"] == "shadow_prediction"


def test_prediction_unavailable_blocks():
    out = _ev(prediction=_pred(None, calibrated=True, status="unavailable"))
    assert out["status"] == "blocked_decision" and out["reason"] == "probability_model_unavailable"


def test_incomplete_features_block():
    out = _ev(features={"feature_status": "boundary_unavailable_at_decision"})
    assert out["status"] == "blocked_decision" and out["reason"] == "boundary_unavailable_at_decision"


# --- ACTIVE path (test-only: boundary locked + calibrated) -----------------------------------

def test_active_paper_entry_when_edge_clears():
    out = _ev()  # q_up 0.7, ask 0.50, fee 0 -> up edge 0.20; down q 0.3 edge -0.20
    assert out["status"] == "paper_entry" and out["entered"] is True
    assert out["chosen_side"] == "Up" and out["token_id"] == "U"
    assert abs(out["expected_edge_per_share"] - 0.20) < 1e-9


def test_active_edge_below_threshold_is_no_trade():
    out = _ev(prediction=_pred(0.55, calibrated=True), min_edge=0.10)  # up edge 0.05 < 0.10
    assert out["status"] == "paper_no_trade" and out["reason"] == "edge_below_threshold"
    assert out["entered"] is False


def test_sides_evaluated_independently_no_complement():
    # Up has a terrible book (0.95), Down a great book (0.20). With q_up=0.7/q_down=0.3:
    # up edge = 0.7-0.95 = -0.25; down edge = 0.3-0.20 = +0.10. The Down side must win on ITS book.
    out = _ev(up_asks=[["0.95", "1000"]], down_asks=[["0.20", "1000"]])
    assert out["status"] == "paper_entry" and out["chosen_side"] == "Down"
    assert abs(out["down_edge"] - 0.10) < 1e-9 and abs(out["up_edge"] - (-0.25)) < 1e-9


def test_active_fee_unknown_blocks_entry():
    out = _ev(fee={"fee_status": "fee_unknown", "fee_rate": None})
    assert out["status"] == "paper_no_trade" and out["reason"] == "fee_unknown"
    assert out["entered"] is False


def test_per_side_book_integrity_is_independent():
    # Up book quarantined, Down book usable + attractive -> only Down is eligible (no cross-contam)
    out = _ev(up_book_integrity_status="quarantined", down_book_integrity_status="usable",
              up_asks=[["0.50", "1000"]], down_asks=[["0.20", "1000"]])
    assert out["status"] == "paper_entry" and out["chosen_side"] == "Down"
    assert out["up_edge"] is None              # Up's own quarantine rejected its fill


def test_missing_feature_status_fails_closed():
    out = _ev(features={})                      # no feature_status key
    assert out["status"] == "blocked_decision" and out["reason"] == "feature_window_incomplete"


def test_calibrated_but_diagnostic_prediction_is_blocked_not_entered():
    # contradictory flags (calibrated=True yet diagnostic_only) must NOT enter on the active path
    out = _ev(prediction=_pred(0.7, calibrated=True, status="diagnostic_only"))
    assert out["status"] == "blocked_decision" and out["reason"] == "probability_uncalibrated"
    assert out["entered"] is False


def test_no_live_order_or_key_imports():
    import inspect
    src = inspect.getsource(D)
    for bad in ("private_key", "post_order", "place_order", "api_key", "signer", "wallet"):
        assert bad not in src
