"""Market universe filter — applies collection scope policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass(slots=True)
class MarketCandidate:
    """A market being evaluated for collection."""
    condition_id: str
    question: str
    outcomes: list[str]
    volume: float
    end_date: str
    active: bool
    closed: bool


@dataclass
class UniverseFilter:
    """Applies the market universe policy from the spec."""

    min_volume: float = 10_000.0
    max_days_to_end: int = 90
    max_active: int = 200
    required_outcome_count: int = 2

    def qualifies(self, candidate: MarketCandidate, now: datetime | None = None) -> bool:
        """Check if a single market passes all filters."""
        if candidate.closed:
            return False
        if not candidate.active:
            return False
        if len(candidate.outcomes) != self.required_outcome_count:
            return False
        if candidate.volume < self.min_volume:
            return False
        try:
            end = datetime.fromisoformat(candidate.end_date.replace("Z", "+00:00"))
            ref = now or datetime.now(timezone.utc)
            delta = end - ref
            if delta.total_seconds() < 0:
                return False  # already ended
            if delta.total_seconds() > self.max_days_to_end * 86400:
                return False  # too far out
        except (ValueError, TypeError):
            return False
        return True

    def apply_cap(self, candidates: list[MarketCandidate]) -> list[MarketCandidate]:
        """Enforce max_active cap. Keeps highest volume markets."""
        sorted_by_vol = sorted(candidates, key=lambda c: c.volume, reverse=True)
        return sorted_by_vol[: self.max_active]
