from __future__ import annotations

from collections import defaultdict

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def _band(price: float) -> str:
    if price < 0.10:
        return "longshot"
    if price < 0.35:
        return "underdog"
    if price < 0.65:
        return "mid"
    if price < 0.90:
        return "favorite"
    return "near_certainty"


def favorite_longshot_calibration(ctx: ClusterContext, *, min_events: int = 5) -> MethodFinding:
    """Calibration edge weighted at the MARKET/EVENT level (not per-position), which is the
    correction that kills the favorite-longshot scale artifact (finding-prediction-market-
    price-band-carry: fake +2.8% position-weighted -> real -4.1% market-weighted)."""
    by_event = defaultdict(list)
    for ep in ctx.resolved_episodes:
        if ep.entry_vwap >= 0.98:
            continue
        by_event[ep.event_slug].append(ep)
    event_rows = []
    for event_slug, eps in by_event.items():
        mean_entry = sum(ep.entry_vwap for ep in eps) / len(eps)
        win_rate = sum(ep.won or 0 for ep in eps) / len(eps)
        event_rows.append((event_slug, _band(mean_entry), win_rate, mean_entry))
    if len(event_rows) < min_events:
        return MethodFinding("favorite_longshot_calibration", "descriptive", 0.0, None, len(event_rows),
                             evidence={"market_weighted": True, "reason": "under_min_events"},
                             controls={}, blockers=[], runnability="descriptive_only")
    win_rate = sum(row[2] for row in event_rows) / len(event_rows)
    entry = sum(row[3] for row in event_rows) / len(event_rows)
    effect = win_rate - entry
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="favorite_longshot_calibration",
        state=state,
        score=max(effect, 0.0) * 100.0,
        effect=effect,
        n=len(event_rows),
        n_events=len(event_rows),                       # already event-grouped
        evidence={"market_weighted": True, "edge": effect, "bands": sorted({r[1] for r in event_rows})},
        controls={"guard": "event_level_weighting_avoids_position_scale_artifact"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
