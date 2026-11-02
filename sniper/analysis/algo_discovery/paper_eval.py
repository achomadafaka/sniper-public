"""Forward-paper evaluation rubric.

Once a routed experiment (copy_mirror or rebuild_rule) has run in paper and some
positions have RESOLVED, this scores the forward results with the SAME
pre-registered, prediction-market-correct gate used pre-deploy — so a
favorite-buyer that racks up a high paper win-rate is still rejected for having
no edge versus the price-implied null (WR baseline = mean entry price, not 0.5).

This is the honest verdict on a paper experiment. Forward semantics of the
gate's labels:
  proceed_to_paper  → paper edge CONFIRMED → eligible for operator live review
  reject            → no edge in paper (e.g. edgeless favorite-buyer)
  insufficient_data → not enough resolved positions yet (keep running)

Pure function. Reuses sniper.analysis.algo_discovery.decision_gate.evaluate by
shaping resolved paper positions into its input. Forward paper is the proof of
edge that historical replay cannot give (no liquidity/fill data) — see the v2
design memo.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sniper.analysis.algo_discovery.decision_gate import DecisionVerdict, evaluate

# strategy_positions / paper_trades resolution states that count as decided.
_WIN_STATES = {"resolution_win"}
_LOSS_STATES = {"resolution_loss"}


def _entry_price(p: Mapping[str, Any]) -> float | None:
    for k in ("entry_price", "avg_price", "avgPrice"):
        v = p.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def evaluate_paper_results(
    positions: Sequence[Mapping[str, Any]],
    *,
    rule: Mapping[str, Any] | None = None,
    stressed_positions: Sequence[Mapping[str, Any]] | None = None,
) -> DecisionVerdict:
    """Score a strategy's RESOLVED paper positions against the decision gate.

    Each position dict needs a resolution `status` (resolution_win /
    resolution_loss / open / ...), an `entry_price` (or avg_price/avgPrice), and
    a `pnl_usdc`. Open/unresolved positions are ignored (not yet decided).
    `stressed_positions` is unused here (slippage is a pre-deploy concern); kept
    for signature symmetry with the gate.
    """
    resolved = [p for p in (positions or []) if str(p.get("status") or "") in (_WIN_STATES | _LOSS_STATES)]
    wins = sum(1 for p in resolved if str(p.get("status")) in _WIN_STATES)
    losses = sum(1 for p in resolved if str(p.get("status")) in _LOSS_STATES)
    pnl = sum(float(p.get("pnl_usdc") or 0.0) for p in resolved)
    per_position = [
        {"sim_entry_price": ep,
         "final_price": (1.0 if str(p.get("status")) in _WIN_STATES else 0.0)}
        for p in resolved
        if (ep := _entry_price(p)) is not None
    ]
    bt_shaped = {
        "oos_simulated_wins": wins,
        "oos_simulated_losses": losses,
        "oos_match_count": len(resolved),
        "oos_simulated_pnl_usdc": pnl,
        "oos_actual_pnl_usdc_on_same_positions": pnl,  # forward == actual
        "caveats": [],
        "per_position_results": per_position,
    }
    return evaluate(bt_shaped, rule or {})
