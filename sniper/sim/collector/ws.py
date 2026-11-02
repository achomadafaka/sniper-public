"""Polymarket WebSocket message parser.

Converts venue-native WS messages into canonical RawEvents.
Does NOT manage the WS connection — that's in ingest.py.
"""
from __future__ import annotations

from sniper.sim.schema import RawEvent, EventType

# Map Polymarket WS event_type to canonical EventType
_EVENT_MAP = {
    "last_trade_price": EventType.TRADE,
    "price_change": EventType.BOOK_DELTA,
    "book": EventType.BOOK_SNAPSHOT,
    "best_bid_ask": EventType.BEST_BBA,
}


def _epoch_to_iso(ts: str | int | None) -> str | None:
    """Convert a Unix epoch timestamp to ISO 8601 UTC string.

    Polymarket WS timestamps may be seconds or milliseconds.
    Auto-detects: values > 10 billion are milliseconds.
    """
    if ts in (None, ""):
        return None
    try:
        val = int(ts)
    except (TypeError, ValueError):
        return None
    if val > 10_000_000_000:
        val = val // 1000  # milliseconds → seconds
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return None


def parse_ws_message(
    raw: dict,
    *,
    market_id: str,
    venue_market_id: str,
    market_type: str,
    next_seq: callable,
    capture_ts: str,
) -> list[RawEvent]:
    """Parse a single Polymarket WS message into RawEvent(s).

    Each emitted RawEvent gets its own unique ingest_seq from next_seq().
    Returns empty list for unrecognized event types.
    """
    ws_type = raw.get("event_type", "")
    event_type = _EVENT_MAP.get(ws_type)
    if event_type is None:
        return []

    exchange_ts = _epoch_to_iso(raw.get("timestamp"))

    if ws_type == "price_change":
        events: list[RawEvent] = []
        for change in raw.get("price_changes", []):
            asset_id = change.get("asset_id")
            events.append(
                RawEvent(
                    venue="polymarket",
                    market_id=market_id,
                    venue_market_id=venue_market_id,
                    market_type=market_type,
                    event_type=event_type,
                    capture_ts=capture_ts,
                    ingest_seq=next_seq(),
                    payload=change,
                    asset_id=str(asset_id) if asset_id else None,
                    outcome_index=None,
                    exchange_ts=exchange_ts,
                    venue_seq=None,
                )
            )
        return events

    asset_id = raw.get("asset_id")
    return [
        RawEvent(
            venue="polymarket",
            market_id=market_id,
            venue_market_id=venue_market_id,
            market_type=market_type,
            event_type=event_type,
            capture_ts=capture_ts,
            ingest_seq=next_seq(),
            payload=raw,
            asset_id=str(asset_id) if asset_id else None,
            outcome_index=None,
            exchange_ts=exchange_ts,
            venue_seq=None,
        )
    ]
