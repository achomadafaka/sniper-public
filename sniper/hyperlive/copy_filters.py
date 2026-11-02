"""Entry-quality filters for the copy runner — pure decision functions.

Derived from the fidelity study:
  * lag filter   — the detection-lag tax is median ~3bps but has a +739bps p90 tail (we chase
                   fast alt pops past the target's fill). Skip the chases.
  * conviction   — copy the target's meaningful bets, not dust (target_notional floor).
  * consensus    — 2-3 wallets on the same coin+side is the sweet spot (+123bps/65% win);
                   4+ is a crowded/late trade (-19bps/34%). Cap the crowd.

The default policy filters NOTHING (backward compatible). The operator opts into thresholds via
the runner flags. Filters fail OPEN on missing data (never reject for want of a captured field)."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.hyperlive.altshort_universe import ALT_SHORT_EXCLUDED, is_alt_short

# The copy edge is concentrated in one cell: shorting non-major alts. Over the discovery window
# the alt/short cell was the ONLY net-positive one (honest follow ledger +71bps/66%; blended book
# ~breakeven; majors net-negative both sides; alt/long net-negative). MAJORS are excluded because
# BTC/ETH/SOL show no copy edge either direction. See finding/feature-study/altshort-standalone-
# viability (regime-dependent: strongest in a bearish alt tape).
MAJORS = ALT_SHORT_EXCLUDED  # compatibility alias; new code uses ALT_SHORT_EXCLUDED


@dataclass(frozen=True)
class EntryPolicy:
    max_lag_bps: float | None = None       # skip if |lag_bps| exceeds this (chasing)
    min_conviction_usd: float = 0.0        # skip if target_notional below this
    min_consensus: int = 0                 # skip if fewer than this many wallets agree
    max_consensus: int | None = None       # skip if more than this many already crowd in
    alt_short_only: bool = False           # restrict to non-major shorts (the copy-edge cell)

    @property
    def active(self) -> bool:
        return (self.max_lag_bps is not None or self.min_conviction_usd > 0
                or self.min_consensus > 0 or self.max_consensus is not None
                or self.alt_short_only)


def should_open(*, coin: str | None = None, side: str | None = None, lag_bps: float | None = None,
                target_notional: float | None = None,
                consensus_n: int = 1, policy: EntryPolicy) -> tuple[bool, str]:
    """Return (open?, reason). Quality filters reject only on data that is present (fail open).
    alt_short_only is a deliberate RESTRICTION and fails closed: unknown coin/side is not
    confirmably an alt-short, so it is skipped."""
    if policy.alt_short_only and not is_alt_short(coin, side):
        return False, f"not alt-short ({coin}/{side})"
    if policy.max_lag_bps is not None and lag_bps is not None and abs(lag_bps) > policy.max_lag_bps:
        return False, f"lag {lag_bps:+.0f}bps > {policy.max_lag_bps:.0f} (chase)"
    if policy.min_conviction_usd > 0 and target_notional is not None \
            and target_notional < policy.min_conviction_usd:
        return False, f"conviction ${target_notional:,.0f} < ${policy.min_conviction_usd:,.0f}"
    if policy.min_consensus > 0 and consensus_n < policy.min_consensus:
        return False, f"consensus {consensus_n} < {policy.min_consensus}"
    if policy.max_consensus is not None and consensus_n > policy.max_consensus:
        return False, f"consensus {consensus_n} > {policy.max_consensus} (crowded)"
    return True, "ok"


# Recommended starting thresholds if the operator enables filtering (from the live study).
RECOMMENDED = EntryPolicy(max_lag_bps=150.0, min_conviction_usd=20000.0,
                          min_consensus=0, max_consensus=3)
