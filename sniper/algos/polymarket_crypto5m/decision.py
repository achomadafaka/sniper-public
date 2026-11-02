"""Shadow decision engine — fail-closed, evaluates Up and Down independently.

Per side: ``expected_edge_per_share = q_side - fee_inclusive_average_cost`` (the FOK fill's average
cost). Never uses complement arithmetic — each side uses ITS OWN ask ladder. A paper *entry* is
possible only when the boundary rule is locked AND the model is calibrated AND the Price-to-Beat
quality is authoritative AND the book is usable AND the fee is known AND the edge clears the floor.
In Phase 1A (boundary unlocked / diagnostic models) every outcome is ``shadow_prediction`` (a forecast
recorded for later scoring, no entry) or ``blocked_decision`` (a forecast could not even be made).
"""
from __future__ import annotations

from . import fee_model
from . import price_to_beat_resolver as P2B
from . import taker_sim

ACTIVATABLE_QUALITY = {"exact_chainlink_report", "validated_rtds_reconstruction"}
_SENTINEL = object()


def _side_fill(asks, fee, *, stake, min_order, book_integrity_status, market_closed):
    if fee is None or not fee_model.is_known(fee):
        return None
    fill = taker_sim.estimate_taker_buy(
        asks=asks, fee_rate=fee.get("fee_rate"), cash_budget=stake, min_order_size=min_order,
        book_integrity_status=book_integrity_status, market_closed=market_closed, fee_known=True)
    return fill if fill["filled"] else None


def evaluate(*, features: dict, prediction, fee, up_asks, down_asks, up_token_id=None,
             down_token_id=None, stake_usdc=10.0, min_order_size=5.0, min_edge=0.0,
             book_integrity_status="usable", up_book_integrity_status=None,
             down_book_integrity_status=None, market_closed=False, strategy_version="phase1a-v0",
             boundary_locked=_SENTINEL) -> dict:
    if boundary_locked is _SENTINEL:
        boundary_locked = P2B.LOCKED_BOUNDARY_RULE
    up_integ = up_book_integrity_status or book_integrity_status      # each side its OWN integrity
    down_integ = down_book_integrity_status or book_integrity_status
    base = {"strategy_version": strategy_version, "entered": False, "chosen_side": None,
            "q_up": prediction.q_up, "q_down": prediction.q_down,
            "model_id": prediction.model_id, "model_version": prediction.model_version,
            "expected_edge_per_share": None, "expected_net_pnl": None}

    # --- hard forecast blocks ---------------------------------------------------------------
    if prediction.prediction_status == "unavailable" or prediction.q_up is None:
        reason = "probability_model_unavailable" if prediction.prediction_status == "unavailable" \
            else "probability_uncalibrated"
        return {**base, "status": "blocked_decision", "reason": reason}
    fstatus = features.get("feature_status")
    if fstatus != "ok":                                              # require explicit ok (fail-closed)
        return {**base, "status": "blocked_decision", "reason": fstatus or "feature_window_incomplete"}

    q_up, q_down = prediction.q_up, prediction.q_down

    # --- diagnostic per-side fills + edges (each side uses its OWN book + OWN integrity) -----
    up_fill = _side_fill(up_asks, fee, stake=stake_usdc, min_order=min_order_size,
                         book_integrity_status=up_integ, market_closed=market_closed)
    down_fill = _side_fill(down_asks, fee, stake=stake_usdc, min_order=min_order_size,
                           book_integrity_status=down_integ, market_closed=market_closed)
    up_edge = (q_up - up_fill["fee_inclusive_average_cost"]) if up_fill else None
    down_edge = (q_down - down_fill["fee_inclusive_average_cost"]) if down_fill else None
    sides = [("Up", up_edge, up_fill, q_up, up_token_id),
             ("Down", down_edge, down_fill, q_down, down_token_id)]
    with_edge = [s for s in sides if s[1] is not None]
    diag_best = max(with_edge, key=lambda s: s[1]) if with_edge else None
    diag = {"q_up": q_up, "q_down": q_down, "up_edge": up_edge, "down_edge": down_edge,
            "chosen_side": diag_best[0] if diag_best else None,
            "expected_edge_per_share": diag_best[1] if diag_best else None}

    # --- activation gates (Phase 1A always stops here) --------------------------------------
    if boundary_locked is None:
        return {**base, **diag, "status": "shadow_prediction", "reason": "boundary_rule_unvalidated"}
    if not prediction.calibrated:
        return {**base, **diag, "status": "shadow_prediction", "reason": "probability_uncalibrated"}

    # --- ACTIVE path (boundary locked + calibrated model) — not reached in Phase 1A ----------
    # defense-in-depth: a calibrated model must ALSO emit a real (not diagnostic) prediction
    if prediction.prediction_status != "ok":
        return {**base, **diag, "status": "blocked_decision", "reason": "probability_uncalibrated"}
    if features.get("price_to_beat_quality") not in ACTIVATABLE_QUALITY:
        return {**base, **diag, "status": "blocked_decision", "reason": "boundary_unavailable_at_decision"}
    fillable = [s for s in sides if s[2] is not None]
    if not fillable:
        reason = "fee_unknown" if (fee is None or fee.get("fee_status") != "known") else "insufficient_depth"
        return {**base, **diag, "status": "paper_no_trade", "reason": reason}
    eligible = [s for s in fillable if s[1] >= min_edge]
    if not eligible:
        return {**base, **diag, "status": "paper_no_trade", "reason": "edge_below_threshold"}
    # choose the eligible side with the larger expected NET pnl (independent books)
    side, edge, fill, q, token = max(eligible, key=lambda s: s[3] * s[2]["shares"] - s[2]["total_cash_cost"])
    net = q * fill["shares"] - fill["total_cash_cost"]
    return {**base, **diag, "status": "paper_entry", "reason": None, "entered": True,
            "chosen_side": side, "token_id": token, "expected_edge_per_share": edge,
            "expected_net_pnl": net, "fill": fill}
