"""CLOB WebSocket message normalization (Phase 1). This task makes the message->FeedEvent
conversion testable offline; the live WebSocket loop is a later task."""
from __future__ import annotations

import time
from typing import Any

from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash

SOURCE = "polymarket_clob_ws"


def _first_asset_id(msg: dict[str, Any]) -> str | None:
    if msg.get("asset_id"):
        return str(msg["asset_id"])
    changes = msg.get("price_changes") or []
    if changes and isinstance(changes[0], dict) and changes[0].get("asset_id"):
        return str(changes[0]["asset_id"])
    return None


def feed_event_from_clob_message(msg: dict[str, Any], *,
                                 observed_ts_ms: int | None = None) -> FeedEvent:
    if observed_ts_ms is None:
        observed_ts_ms = int(time.time() * 1000)
    try:
        event_ts_ms = int(msg["timestamp"]) if msg.get("timestamp") is not None else None
    except (TypeError, ValueError):
        event_ts_ms = None
    return FeedEvent(
        source=SOURCE,
        source_event_type=str(msg.get("event_type") or "unknown"),
        observed_ts_ms=observed_ts_ms,
        event_ts_ms=event_ts_ms,
        market_id=str(msg["market"]) if msg.get("market") else None,
        condition_id=None,
        asset_id=_first_asset_id(msg),
        event_slug=None,
        payload=msg,
        payload_hash=payload_hash(msg),
    )
