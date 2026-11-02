"""Thesis feed-integration data contracts (Phase 1). Raw feed events keep observed-time
and source-event-time separate; payload_hash gives append-store idempotency."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


def payload_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class FeedEvent:
    source: str
    source_event_type: str
    observed_ts_ms: int
    event_ts_ms: int | None
    market_id: str | None
    condition_id: str | None
    asset_id: str | None
    event_slug: str | None
    payload: dict[str, Any]
    payload_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderBookPoint:
    asset_id: str
    ts_ms: int
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    bid_depth_1: float
    ask_depth_1: float
    bid_depth_5: float
    ask_depth_5: float
    source: str


@dataclass(frozen=True)
class FillEvent:
    tx_hash: str
    log_id: str
    ts: int
    maker: str
    taker: str
    maker_asset_id: str
    taker_asset_id: str
    maker_amount: float
    taker_amount: float
    condition_id: str | None = None
    event_slug: str | None = None
    price: float | None = None
    source: str = "goldsky_orderbook_subgraph"
