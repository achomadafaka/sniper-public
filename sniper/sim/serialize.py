"""Event serialization — JSONL round-trip for raw events."""
from __future__ import annotations

import json
from typing import Any

from sniper.sim.schema import RawEvent, EventType


def event_to_jsonl(event: RawEvent) -> str:
    """Serialize a RawEvent to a single JSONL line (no trailing newline)."""
    d: dict[str, Any] = {
        "venue": event.venue,
        "market_id": event.market_id,
        "venue_market_id": event.venue_market_id,
        "market_type": event.market_type,
        "event_type": event.event_type.value,
        "capture_ts": event.capture_ts,
        "ingest_seq": event.ingest_seq,
        "payload": event.payload,
    }
    if event.asset_id is not None:
        d["asset_id"] = event.asset_id
    if event.outcome_index is not None:
        d["outcome_index"] = event.outcome_index
    if event.exchange_ts is not None:
        d["exchange_ts"] = event.exchange_ts
    if event.venue_seq is not None:
        d["venue_seq"] = event.venue_seq
    return json.dumps(d, separators=(",", ":"))


def jsonl_to_event(line: str) -> RawEvent:
    """Deserialize a JSONL line into a RawEvent."""
    d = json.loads(line)
    return RawEvent(
        venue=d["venue"],
        market_id=d["market_id"],
        venue_market_id=d["venue_market_id"],
        market_type=d["market_type"],
        event_type=EventType(d["event_type"]),
        capture_ts=d["capture_ts"],
        ingest_seq=d["ingest_seq"],
        payload=d["payload"],
        asset_id=d.get("asset_id"),
        outcome_index=d.get("outcome_index"),
        exchange_ts=d.get("exchange_ts"),
        venue_seq=d.get("venue_seq"),
    )
