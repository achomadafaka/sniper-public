"""Wallet position-size helpers used by analyzer consumers.

These helpers aggregate fills into position-level stakes, then compute a shared nearest-rank Q75.
They are research/intake utilities only: no strategy state, no paper book, no trade advice.
"""
from __future__ import annotations

import math


def q75_index(n: int) -> int:
    """Nearest-rank 75th-percentile index into a sorted list of length n>=1."""
    return max(0, min(int(n) - 1, int(math.ceil(0.75 * int(n))) - 1)) if n else 0


def position_stakes(activity: list[dict]) -> list[float]:
    """Position-level BUY stakes across all markets, sorted.

    Fills are aggregated by (condition_id, outcome_index), so multi-fill orders do not inflate the
    threshold used by live-signal profiling.
    """
    agg: dict[tuple, float] = {}
    for a in activity:
        if a.get("type") != "TRADE" or a.get("side") != "BUY":
            continue
        usd = float(a.get("usdcSize") or 0.0)
        px = float(a.get("price") or 0.0)
        if usd <= 0 or not (0 < px < 1):
            continue
        try:
            oidx = int(a.get("outcomeIndex"))
        except (TypeError, ValueError):
            oidx = -1
        key = (str(a.get("conditionId") or ""), oidx)
        agg[key] = agg.get(key, 0.0) + usd
    return sorted(agg.values())
