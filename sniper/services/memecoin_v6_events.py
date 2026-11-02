"""Immutable normalized event, cursor, and health contracts for the V6 fast paper runtime."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sniper.services.memecoin_v6_engine import SUPPORTED_NETWORKS, canonical_json, fingerprint


def _aware(value: str | None, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be an aware timestamp")


@dataclass(frozen=True)
class V6MarketEvent:
    network_id: str
    pool: str
    block_height: int
    block_hash: str | None
    tx_id: str
    event_index: int
    source_event_at: str | None
    received_at: str
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self):
        if self.network_id not in SUPPORTED_NETWORKS:
            raise ValueError(f"unsupported network_id: {self.network_id}")
        for name in ("pool", "tx_id", "kind"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("block_height", "event_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.network_id == "4663" and not self.block_hash:
            raise ValueError("4663 events require a block_hash reorg anchor")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        canonical_json(dict(self.payload))
        _aware(self.source_event_at, "source_event_at", optional=True)
        _aware(self.received_at, "received_at")


@dataclass(frozen=True)
class V6FeedCursor:
    network_id: str
    pool: str
    height: int
    event_identity: str

    def __post_init__(self):
        if self.network_id not in SUPPORTED_NETWORKS or not self.pool or not self.event_identity:
            raise ValueError("cursor identities are invalid")
        if isinstance(self.height, bool) or not isinstance(self.height, int) or self.height < 0:
            raise ValueError("cursor height must be a non-negative integer")


@dataclass(frozen=True)
class V6FeedHealth:
    mode: str
    current: bool
    gap: bool
    last_received_at: str | None
    reconnect_count: int

    def __post_init__(self):
        if not self.mode or not isinstance(self.current, bool) or not isinstance(self.gap, bool):
            raise ValueError("feed health mode/current/gap are invalid")
        if isinstance(self.reconnect_count, bool) or not isinstance(self.reconnect_count, int) \
                or self.reconnect_count < 0:
            raise ValueError("reconnect_count must be a non-negative integer")
        _aware(self.last_received_at, "last_received_at", optional=True)


def event_identity(event: V6MarketEvent) -> str:
    anchor = ({"block_hash": event.block_hash, "tx_id": event.tx_id,
               "log_index": event.event_index}
              if event.network_id == "4663" else
              {"slot": event.block_height, "signature": event.tx_id,
               "instruction_or_log_index": event.event_index})
    return "event-" + fingerprint({
        "network_id": event.network_id, "pool": event.pool, "anchor": anchor,
        "kind": event.kind, "payload": dict(event.payload),
    })
