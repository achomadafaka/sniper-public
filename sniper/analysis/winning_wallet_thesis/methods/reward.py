from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def reward_attribution(ctx: ClusterContext) -> MethodFinding:
    """Method #12 — is the wallet's edge prediction or liquidity rewards? Descriptive."""
    rewards = sum(ctx.book.reward_income.values())
    gross = abs(ctx.pnl) + rewards
    share = rewards / gross * 100.0 if gross else 0.0
    return MethodFinding(
        method="reward_attribution",
        state="descriptive",
        score=0.0,
        effect=share,
        n=len(ctx.episodes),
        evidence={"reward_usdc": rewards, "reward_share_pct": share},
        controls={"interpretation": "reward-heavy_clusters_are_not_selection_theses"},
        blockers=[],
        runnability="descriptive_only",
    )
