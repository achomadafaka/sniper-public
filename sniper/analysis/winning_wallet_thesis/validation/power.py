from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower

SECONDS_PER_DAY = 86400


def estimate_forward_power(
    book: PositionBook,
    cluster: EdgeCluster,
    *,
    horizon_days: int = 30,
    min_events: int = 20,
) -> ForwardPower:
    """Extrapolate the cluster's historical event cadence to a fixed forward horizon.
    Underpowered theses are NOT 'failed' — they route to replication (spec §4.8)."""
    ctx = ClusterContext(cluster, book)
    ts_values = [ep.first_buy_ts for ep in ctx.episodes
                 if ep.first_buy_ts is not None and ep.event_slug]
    events = {ep.event_slug for ep in ctx.episodes if ep.event_slug}
    if len(ts_values) < 2 or not events:
        return ForwardPower(expected_events=0, horizon_days=horizon_days, powered=False)
    span_days = max((max(ts_values) - min(ts_values)) / SECONDS_PER_DAY, 1.0)
    events_per_day = len(events) / span_days
    expected = int(events_per_day * horizon_days)
    return ForwardPower(expected_events=expected, horizon_days=horizon_days,
                        powered=expected >= min_events)
