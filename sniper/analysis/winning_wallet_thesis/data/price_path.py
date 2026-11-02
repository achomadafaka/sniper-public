from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PricePathProvider(Protocol):
    def path(self, condition_id: str, *, start_ts: int, end_ts: int,
             outcome_index: int | None = None
             ) -> list[tuple[int, float]] | None:
        ...


@dataclass
class InMemoryPricePathProvider:
    """Returns None for an absent condition (no path = honest gap), and a window-filtered
    path otherwise. A real provider would query captured order-book/price-index snapshots."""
    rows: dict[str, list[tuple[int, float]]]

    def path(self, condition_id: str, *, start_ts: int, end_ts: int,
             outcome_index: int | None = None
             ) -> list[tuple[int, float]] | None:
        if condition_id not in self.rows:
            return None
        return sorted((ts, price) for ts, price in self.rows[condition_id] if start_ts <= ts <= end_ts)
