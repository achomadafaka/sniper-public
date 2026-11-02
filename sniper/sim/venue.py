"""Venue adapter protocol — interface for any prediction market venue."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sniper.sim.schema import RawEvent


@runtime_checkable
class VenueAdapter(Protocol):
    """Interface that venue-specific adapters must implement.

    Each venue adapter knows how to:
    1. Construct canonical market IDs from venue-native IDs.
    2. Classify market type from venue-native identifiers.
    3. Normalize prices to 0.0-1.0 probability space.
    4. Parse raw WS/API messages into canonical RawEvent(s).
    """

    venue_name: str

    def make_market_id(self, venue_native_id: str) -> str:
        """Build a canonical market_id from a venue-native ID."""
        ...

    def classify_market(self, identifier: str) -> str:
        """Return canonical market_type from venue-native identifier."""
        ...

    def normalize_price(self, raw_price: float) -> float:
        """Convert venue-native price to 0.0-1.0 probability space."""
        ...

    def parse_event(self, channel: str, raw_msg: dict[str, Any]) -> RawEvent | list[RawEvent]:
        """Parse a raw WS/API message into one or more canonical RawEvents."""
        ...
