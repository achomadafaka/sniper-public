from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def exit_hold_mechanism(ctx: ClusterContext) -> MethodFinding:
    """Held-vs-sold mechanism. DESCRIPTIVE only — there is no clean exit-time null yet
    (the held/sold split is adverse-selected), so it is never promotable."""
    counts = Counter(ep.exit_mechanism for ep in ctx.episodes)
    return MethodFinding(
        method="exit_hold_mechanism",
        state="descriptive",
        score=0.0,
        effect=None,
        n=len(ctx.episodes),
        evidence={"held": counts.get("held", 0), "sold": counts.get("sold", 0),
                  "open": counts.get("open", 0)},
        controls={"guard": "no_clean_exit_time_null_yet"},
        blockers=["missing_exit_time_price_path"],
        runnability="descriptive_only",
    )
