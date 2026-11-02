from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def sizing_skill(ctx: ClusterContext, *, min_n: int = 30) -> MethodFinding:
    """Rare/lower-priority (1/200 admitted, 0 forward historically). Requires a positive
    base edge first; effect = dollar-weighted minus equal-weighted calibration."""
    eps = ctx.resolved_episodes
    if len(eps) < min_n:
        return MethodFinding("sizing_skill", "descriptive", 0.0, None, len(eps),
                             evidence={"reason": "under_min_n"}, controls={},
                             blockers=[], runnability="descriptive_only")
    equal = sum((ep.won or 0) - ep.entry_vwap for ep in eps) / len(eps)
    total_cost = sum(ep.buy_cost for ep in eps)
    dollar = (sum(((ep.won or 0) - ep.entry_vwap) * ep.buy_cost for ep in eps) / total_cost
              if total_cost else 0.0)
    effect = dollar - equal
    state = "supported_proxy" if equal > 0 and effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_sizing_edge", "to_resolution", 0.01, -0.01, min_n)
    return MethodFinding(
        method="sizing_skill",
        state=state,
        score=max(effect, 0.0) * 100.0,
        effect=effect,
        n=len(eps),
        n_events=len({ep.event_slug for ep in eps if ep.event_slug}),     # independent events, not positions
        evidence={"equal_weight_edge": equal, "dollar_weight_edge": dollar},
        controls={"guard": "requires_positive_base_edge_first"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
