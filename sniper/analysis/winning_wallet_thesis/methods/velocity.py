from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def capital_velocity(ctx: ClusterContext) -> MethodFinding:
    """Amplifier/meta-method only — velocity WITHOUT a per-bet edge is fee-paying churn,
    never a standalone edge claim."""
    turnover = ctx.turnover
    pnl = ctx.pnl
    roi = pnl / turnover if turnover else 0.0
    return MethodFinding(
        method="capital_velocity",
        state="descriptive",
        score=abs(roi),
        effect=roi,
        n=len(ctx.episodes),
        evidence={"turnover": turnover, "pnl": pnl, "roi_per_dollar": roi},
        controls={"interpretation": "amplifier_only_velocity_without_edge_is_churn"},
        blockers=[],
        runnability="descriptive_only",
    )
