"""Canonical market ID construction and parsing."""
from __future__ import annotations

_VENUE_PREFIXES = {
    "polymarket": "pm",
    "kalshi": "kalshi",
}

_PREFIX_TO_VENUE = {v: k for k, v in _VENUE_PREFIXES.items()}


def make_market_id(venue: str, venue_native_id: str) -> str:
    """Build a canonical market_id from venue + native ID."""
    prefix = _VENUE_PREFIXES.get(venue)
    if prefix is None:
        raise ValueError(f"Unknown venue: {venue}")
    return f"{prefix}:{venue_native_id}"


def parse_market_id(market_id: str) -> tuple[str, str]:
    """Parse a canonical market_id into (venue, native_id)."""
    if ":" not in market_id:
        raise ValueError(f"Invalid market_id format (no colon): {market_id}")
    prefix, native_id = market_id.split(":", 1)
    venue = _PREFIX_TO_VENUE.get(prefix)
    if venue is None:
        raise ValueError(f"Invalid market_id prefix: {prefix}")
    return venue, native_id
