from __future__ import annotations

from typing import Protocol


class FillIdentityProvider(Protocol):
    available: bool

    def fills_for_market(self, condition_id: str, *, start_ts: int, end_ts: int) -> list:
        ...

    def fills_for_wallet(self, wallet: str, *, start_ts: int, end_ts: int) -> list:
        ...


class UnavailableFillIdentityProvider:
    """Default: the Data API does not expose per-fill wallet identity / maker-taker, so
    follower-flow and first-attention detectors stay BLOCKED (verified 2026-05-31)."""
    available = False
