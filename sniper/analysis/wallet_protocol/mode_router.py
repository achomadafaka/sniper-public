"""§5 — Mode router.

Classifies a wallet's primary trading mode based on flip_rate. Determines which
Stage 2 branch(es) run.

Thresholds locked at 0.30 / 0.70 per spec §5. These are judgment calls (§13
limitation 13) until labeled wallet set ≥50 lands for empirical fit. They
are NOT empirically validated — change requires protocol v3.
"""

from __future__ import annotations

from dataclasses import dataclass

from sniper.analysis.wallet_protocol.flip_reconstruction import (
    FlipReconstructionResult,
)
from sniper.analysis.wallet_protocol.types import TradingMode


# Locked thresholds (§5). Judgment calls; documented in §13 limitation 13.
FLIP_RATE_HELD_ONLY_MAX = 0.30
FLIP_RATE_FLIPPER_ONLY_MIN = 0.70


@dataclass
class RoutingDecision:
    mode: TradingMode
    flip_rate: float
    confidence: str                                        # "ok" | "low" (cap-hit warning)
    rationale: str
    run_held_branch: bool
    run_flipper_branch: bool
    flipper_truncated: bool                                # cap+>0.50 → flipper branch suppressed entirely


def classify_mode(
    flip_recon: FlipReconstructionResult,
) -> RoutingDecision:
    """Determine which Stage 2 branch(es) to run.

    Activity-cap interaction (per §4.3 + §13 limitation 14):
    A high-volume flipper's recent activity may show fewer flips than lifetime
    behavior, undercounting flip_rate. confidence='low' surfaces this.
    """
    flip_rate = flip_recon.flip_rate

    # Activity-cap warnings affect confidence, not the threshold logic
    confidence = "low" if flip_recon.activity_cap_hit else "ok"

    if flip_recon.flipper_analysis_truncated:
        # flip_rate > 0.50 AND cap hit → cannot reliably reconstruct flips.
        # Per spec §4.7: don't run flipper branch. Held-analyst branch can still
        # try, but with the activity-cap warning.
        return RoutingDecision(
            mode="held_analyst_only",
            flip_rate=flip_rate,
            confidence="low",
            rationale=(
                f"FLIPPER_ANALYSIS_TRUNCATED: flip_rate={flip_rate:.2f} > 0.50 "
                f"AND activity_cap_hit. Flipper branch suppressed; held-analyst "
                f"branch may still produce a verdict but is degraded."
            ),
            run_held_branch=True,
            run_flipper_branch=False,
            flipper_truncated=True,
        )

    if flip_rate < FLIP_RATE_HELD_ONLY_MAX:
        return RoutingDecision(
            mode="held_analyst_only",
            flip_rate=flip_rate,
            confidence=confidence,
            rationale=(
                f"flip_rate={flip_rate:.2f} < {FLIP_RATE_HELD_ONLY_MAX}: "
                f"wallet predominantly holds to resolution."
            ),
            run_held_branch=True,
            run_flipper_branch=False,
            flipper_truncated=False,
        )

    if flip_rate > FLIP_RATE_FLIPPER_ONLY_MIN:
        return RoutingDecision(
            mode="flipper_only",
            flip_rate=flip_rate,
            confidence=confidence,
            rationale=(
                f"flip_rate={flip_rate:.2f} > {FLIP_RATE_FLIPPER_ONLY_MIN}: "
                f"wallet predominantly flips before resolution."
            ),
            run_held_branch=False,
            run_flipper_branch=True,
            flipper_truncated=False,
        )

    return RoutingDecision(
        mode="mixed_mode",
        flip_rate=flip_rate,
        confidence=confidence,
        rationale=(
            f"flip_rate={flip_rate:.2f} in [{FLIP_RATE_HELD_ONLY_MAX}, "
            f"{FLIP_RATE_FLIPPER_ONLY_MIN}]: mixed mode. Both branches run "
            f"independently; verdicts reported separately."
        ),
        run_held_branch=True,
        run_flipper_branch=True,
        flipper_truncated=False,
    )


__all__ = [
    "RoutingDecision",
    "classify_mode",
    "FLIP_RATE_HELD_ONLY_MAX",
    "FLIP_RATE_FLIPPER_ONLY_MIN",
]
