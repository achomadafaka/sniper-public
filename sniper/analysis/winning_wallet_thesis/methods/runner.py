from __future__ import annotations

from collections.abc import Callable

from sniper.analysis.winning_wallet_thesis.data.fill_identity import (
    FillIdentityProvider, UnavailableFillIdentityProvider,
)
from sniper.analysis.winning_wallet_thesis.data.price_path import PricePathProvider
from sniper.analysis.winning_wallet_thesis.methods.attention import first_attention_discovery
from sniper.analysis.winning_wallet_thesis.methods.calibration import favorite_longshot_calibration
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.exit_hold import exit_hold_mechanism
from sniper.analysis.winning_wallet_thesis.methods.liquidity import dead_hour_timing, stale_liquidity
from sniper.analysis.winning_wallet_thesis.methods.microstructure import (
    adverse_selection_aware_making, market_making_spread_capture,
)
from sniper.analysis.winning_wallet_thesis.methods.reward import reward_attribution
from sniper.analysis.winning_wallet_thesis.methods.sizing import sizing_skill
from sniper.analysis.winning_wallet_thesis.methods.specialist import specialist_domain
from sniper.analysis.winning_wallet_thesis.methods.template import recurring_template
from sniper.analysis.winning_wallet_thesis.methods.velocity import capital_velocity
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook

DETECTORS: tuple[Callable[[ClusterContext], object], ...] = (
    specialist_domain,
    recurring_template,
    favorite_longshot_calibration,
    reward_attribution,
    sizing_skill,
    exit_hold_mechanism,
    capital_velocity,
)


def run_detectors(book: PositionBook, clusters: list[EdgeCluster], *,
                  price_path: PricePathProvider | None = None,
                  fill_identity: FillIdentityProvider | None = None) -> dict[str, list]:
    """Run every detector against each cluster's context (spec §4.6 — per cluster, not
    whole-wallet)."""
    identity = fill_identity or UnavailableFillIdentityProvider()
    out: dict[str, list] = {}
    for cluster in clusters:
        ctx = ClusterContext(cluster=cluster, book=book)
        findings = [detector(ctx) for detector in DETECTORS]
        findings.extend([
            stale_liquidity(ctx, price_path=price_path),
            dead_hour_timing(ctx),
            first_attention_discovery(ctx, fill_identity=identity),
            market_making_spread_capture(ctx, fill_identity=identity),
            adverse_selection_aware_making(ctx, fill_identity=identity),
        ])
        out[cluster.cluster_id] = findings
    return out
