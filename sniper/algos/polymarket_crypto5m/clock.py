"""Clock integrity. 'Now' must be an explicit, recorded decision — Binance server time is a fallback,
not hidden canonical truth. A machine with a large UTC offset (e.g. the +7h env skew) is an
operational problem to surface, not to paper over with a borrowed exchange clock.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import ClockSource

# Beyond this host-vs-reference offset, treat the local clock as broken and warn/fail loudly.
DEFAULT_SKEW_THRESHOLD_MS = 5 * 60 * 1000  # 5 minutes


@dataclass(frozen=True)
class ClockRecord:
    clock_source: ClockSource
    clock_offset_ms: int           # reference_now - host_now (ms)
    clock_last_verified_ts: int    # unix seconds (reference clock)


def excessive_skew(*, offset_ms: int, threshold_ms: int = DEFAULT_SKEW_THRESHOLD_MS) -> bool:
    """True if |host-vs-reference offset| exceeds the threshold (host clock is untrustworthy)."""
    return abs(int(offset_ms)) > int(threshold_ms)
