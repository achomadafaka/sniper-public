from __future__ import annotations

from datetime import datetime, timezone

from sniper.analysis.winning_wallet_thesis.data.price_path import PricePathProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def stale_liquidity(ctx: ClusterContext, *, price_path: PricePathProvider | None,
                    horizon_seconds: int = 3600) -> MethodFinding:
    """Post-entry drift proxy (#15). BLOCKED without a price path — never invents drift."""
    if price_path is None:
        return MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_price_path"],
                             runnability="data_blocked")
    drifts = []
    for ep in ctx.episodes:
        if ep.first_buy_ts is None:
            continue
        path = price_path.path(ep.condition_id, start_ts=ep.first_buy_ts,
                               end_ts=ep.first_buy_ts + horizon_seconds,
                               outcome_index=ep.outcome_index)
        if not path:
            continue
        drifts.append(path[-1][1] - ep.entry_vwap)
    if not drifts:
        return MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_price_path_window"],
                             runnability="data_blocked")
    effect = sum(drifts) / len(drifts)
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("post_entry_drift_gap", "1h", 0.01, -0.01, 20)
    return MethodFinding("stale_liquidity_sniping", state, max(effect, 0) * 100, effect, len(drifts),
                         evidence={"mean_post_entry_drift": effect},
                         controls={"guard": "proxy_not_proof"}, blockers=[],
                         runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
                         forward_test_design=design if state.startswith("supported") else None,
                         n_events=ctx.event_count)


def dead_hour_timing(ctx: ClusterContext) -> MethodFinding:
    """Descriptive: timing alone is not edge (and is confounded with category in practice)."""
    hours = [datetime.fromtimestamp(ep.first_buy_ts, tz=timezone.utc).hour
             for ep in ctx.episodes if ep.first_buy_ts is not None]
    dead = [h for h in hours if h in {2, 3, 4, 5, 6, 7, 8, 9}]
    share = len(dead) / len(hours) if hours else 0.0
    return MethodFinding("dead_hour_timing", "descriptive", share, share, len(hours),
                         evidence={"dead_hour_share": share, "dead_hours_utc": [2, 3, 4, 5, 6, 7, 8, 9]},
                         controls={"guard": "timing_alone_is_not_edge"},
                         blockers=[], runnability="descriptive_only")
