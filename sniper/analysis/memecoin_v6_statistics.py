"""Shared deterministic statistics for the V6 policy, runner, and operator report."""
from __future__ import annotations

import math


def linear_percentile(values, percentile: float):
    """Return the linearly interpolated percentile, or ``None`` for an empty sample."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * float(percentile) / 100.0
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
