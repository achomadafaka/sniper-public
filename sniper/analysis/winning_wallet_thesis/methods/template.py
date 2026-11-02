from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def recurring_template(ctx: ClusterContext, *, min_events: int = 3) -> MethodFinding:
    eps = ctx.resolved_episodes
    templates = Counter(ep.template for ep in eps if ep.template)
    template, n = templates.most_common(1)[0] if templates else ("", 0)
    effect = ctx.cluster.edge_pp
    if not template or n < min_events:
        return MethodFinding("recurring_template_edge", "descriptive", 0.0, effect, len(eps),
                             evidence={"reason": "no_repeated_platform_template"},
                             controls={}, blockers=[], runnability="descriptive_only")
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="recurring_template_edge",
        state=state,
        score=max(effect, 0.0) * n / max(ctx.event_count, 1),
        effect=effect,
        n=n,
        n_events=len({ep.event_slug for ep in eps if ep.template == template and ep.event_slug}),
        evidence={"template": template, "template_events": n},
        controls={"source": "series_or_slug_family_not_title_regex"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
