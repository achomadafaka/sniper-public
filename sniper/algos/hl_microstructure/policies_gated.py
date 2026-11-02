"""Gated forward-A/B entry policies for the Hyperliquid Market Radar.

Shadow arms that differ from ``fusion_radar`` (control) ONLY in entry selectivity:

- B1 (``fusion_gated_b1``): regime selectivity + funding/premium demoted from initiators.
- B2 (``fusion_gated_b2``): B1 plus an entry cost gate (est edge must clear fee+spread+buffer).

Exits are NOT decided here — once a gated position is open the runner drives its exit with
the same control decay rule, so the experiment isolates entry. All thresholds below are
pre-registered and frozen (see docs/superpowers/specs/2026-06-21-hl-microstructure-entry-cost-gate-forward-ab-design.md).
"""
from __future__ import annotations

from sniper.algos.hl_microstructure.fusion import _winner, dynamic_exit_plan, source_votes
from sniper.algos.hl_microstructure.fusion import FusionContext
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision


FAST_FAMILIES = frozenset({"flow", "book", "momentum", "expert"})

# Lever 2 — regime selectivity (frozen).
GATED_MAX_SPREAD_BPS = 8.0
GATED_MIN_DEPTH_USDC = 10_000.0
GATED_MIN_TRADE_COUNT_10S = 8

# Lever 1 — entry cost gate (frozen, B2 only).
TAKER_FEE_BPS = 4.5
COST_BUFFER_BPS = 3.0
EDGE_K = 35.0
VOL_REF = 0.004
VOL_SCALE_MIN = 0.5
VOL_SCALE_MAX = 2.0


def evaluate_fusion_gated_policy(snapshot: FeatureSnapshot, context: FusionContext | None,
                                 *, arm: str) -> PolicyDecision:
    context = context or FusionContext()
    policy_id = f"fusion_gated_{arm}"
    if snapshot.status != "ok":
        return PolicyDecision(policy_id, "skip", None, snapshot.status, 0.0)
    f = snapshot.features
    spread = f.get("spread_bps")
    if spread is None:
        return PolicyDecision(policy_id, "skip", None, "book_missing", 0.0)
    if float(spread) > GATED_MAX_SPREAD_BPS:
        return PolicyDecision(policy_id, "skip", None, "spread_too_wide", 0.0)
    bid_depth = _float(f.get("bid_depth_5"))
    ask_depth = _float(f.get("ask_depth_5"))
    if bid_depth is None or ask_depth is None or min(bid_depth, ask_depth) < GATED_MIN_DEPTH_USDC:
        return PolicyDecision(policy_id, "skip", None, "shallow_book", 0.0)
    if int(_float(f.get("trade_count_10s")) or 0) < GATED_MIN_TRADE_COUNT_10S:
        return PolicyDecision(policy_id, "skip", None, "not_enough_flow", 0.0)

    votes = source_votes(snapshot, context)
    fast_votes = {family: vote for family, vote in votes.items() if family in FAST_FAMILIES}
    side, edge, count = _winner(fast_votes)
    if side is None or count < 2:
        return PolicyDecision(policy_id, "skip", None, "needs_two_fast_families", 0.0)
    confidence = min(1.0, edge / max(1.0, count * 0.85))
    if confidence < 0.35:
        return PolicyDecision(policy_id, "skip", None, "weak_fusion_score", confidence)

    if arm == "b2":
        round_trip_cost_bps = 2.0 * TAKER_FEE_BPS + float(spread)
        est_edge_bps = EDGE_K * (confidence - 0.35) * _vol_scale(f.get("realized_vol_60s"))
        if est_edge_bps < round_trip_cost_bps + COST_BUFFER_BPS:
            return PolicyDecision(policy_id, "skip", None, "cost_gate_not_cleared", confidence)

    vote_payload = {
        family: {"active": True, "side": vote_side, "score": score}
        for family, (vote_side, score) in votes.items()
    }
    return PolicyDecision(
        policy_id, "enter", side, f"gated_{arm}_{count}_fast_{side}", confidence,
        {
            "source_votes": vote_payload,
            "exit_plan": dynamic_exit_plan(snapshot, side),
            "thesis": f"{snapshot.asset} {side}: {count} fast families agree, gated arm {arm}.",
        },
    )


def _vol_scale(vol) -> float:
    v = _float(vol)
    if v is None or v <= 0:
        return VOL_SCALE_MIN
    return max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, v / VOL_REF))


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
