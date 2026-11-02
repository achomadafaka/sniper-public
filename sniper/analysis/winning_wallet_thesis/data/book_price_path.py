"""Feed-backed PricePathProvider — satisfies the Phase 4 `PricePathProvider` contract
using captured OrderBookPoints. Flips `stale_liquidity` from blocked to testable."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.feeds.thesis_feed_types import OrderBookPoint


@dataclass
class BookPricePathProvider:
    points: list[OrderBookPoint]
    condition_to_asset: dict[object, str]

    def path(self, condition_id: str, *, start_ts: int, end_ts: int,
             outcome_index: int | None = None
             ) -> list[tuple[int, float]] | None:
        asset = self.condition_to_asset.get((condition_id, outcome_index))
        if asset is None:
            asset = self.condition_to_asset.get(condition_id)
        if asset is None:
            return None
        out = []
        for point in self.points:
            ts = int(point.ts_ms / 1000)
            if point.asset_id == asset and start_ts <= ts <= end_ts and point.mid is not None:
                out.append((ts, point.mid))
        return sorted(out)
