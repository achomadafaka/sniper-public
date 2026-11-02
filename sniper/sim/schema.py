"""Canonical event schema for the simulation platform."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def capture_ts_now() -> str:
    """Generate a capture_ts at the current moment. UTC ISO-8601 with ms."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventType(str, Enum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE = "trade"
    BEST_BBA = "best_bba"
    MARKET_METADATA = "market_metadata"
    MARKET_RESOLVED = "market_resolved"


@dataclass(slots=True)
class RawEvent:
    """A single raw event from any venue.

    Raw events are stored close to venue-native shape.
    The compactor normalizes them into market-wide views.
    """

    venue: str
    market_id: str
    venue_market_id: str
    market_type: str
    event_type: EventType
    capture_ts: str
    ingest_seq: int
    payload: dict[str, Any]
    asset_id: Optional[str] = None
    outcome_index: Optional[int] = None
    exchange_ts: Optional[str] = None
    venue_seq: Optional[int] = None
