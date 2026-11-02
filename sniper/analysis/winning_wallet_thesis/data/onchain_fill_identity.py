"""Feed-backed FillIdentityProvider — satisfies the Phase 4 `FillIdentityProvider`
contract (available + wallet/market fill lookup) using normalized on-chain fills. Flips
first-attention / follower-flow methods from blocked to testable when fills are present."""
from __future__ import annotations

from dataclasses import dataclass, field

from sniper.feeds.thesis_feed_types import FillEvent


@dataclass
class OnchainFillIdentityProvider:
    fills: list[FillEvent] = field(default_factory=list)
    available: bool = True

    def fills_for_wallet(self, wallet: str, *, start_ts: int, end_ts: int) -> list[FillEvent]:
        w = wallet.lower()
        return [f for f in self.fills
                if start_ts <= f.ts <= end_ts and (f.maker == w or f.taker == w)]

    def fills_for_market(self, condition_id: str, *, start_ts: int, end_ts: int) -> list[FillEvent]:
        return [f for f in self.fills
                if f.condition_id == condition_id and start_ts <= f.ts <= end_ts]
