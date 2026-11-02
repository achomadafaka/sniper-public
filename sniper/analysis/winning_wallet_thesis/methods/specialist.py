from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def specialist_domain(ctx: ClusterContext, *, min_events: int = 5) -> MethodFinding:
    eps = ctx.resolved_episodes
    tags = Counter(tag for ep in eps for tag in ep.tags)
    top_tag, tag_n = tags.most_common(1)[0] if tags else ("", 0)
    effect = ctx.cluster.edge_pp
    if ctx.event_count < min_events:
        return MethodFinding("specialist_domain_edge", "descriptive", 0.0, effect, len(eps),
                             evidence={"reason": "under_min_events", "events": ctx.event_count},
                             controls={}, blockers=[], runnability="descriptive_only")
    state = "supported_proxy" if effect > 0 and tag_n >= min_events else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="specialist_domain_edge",
        state=state,
        score=max(effect, 0.0) * min(ctx.event_count, 50) / 50.0,
        effect=effect,
        n=ctx.event_count,
        n_events=ctx.event_count,
        evidence={"tag": top_tag, "tag_events": tag_n, "axis": ctx.cluster.axis},
        controls={"guard": "cluster_guarded_by_phase1_min_events_min_positions"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
