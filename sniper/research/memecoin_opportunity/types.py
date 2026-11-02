"""Immutable canonical event types for launch-opportunity capture."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .identity import normalize_identity, require_utc_iso


class NetworkId(str, Enum):
    SOLANA = "solana"
    ROBINHOOD_4663 = "4663"


class VenueId(str, Enum):
    PUMPFUN_SOLANA = "pumpfun_solana"
    BAGS_SOLANA = "bags_solana"
    BAGS_ROBINHOOD_4663 = "bags_robinhood_4663"
    PONS_V1_ROBINHOOD_4663 = "pons_v1_4663"


class ConfirmationState(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    ORPHANED = "orphaned"


class LifecycleState(str, Enum):
    PRE_MIGRATION = "pre_migration"
    MIGRATED = "migrated"
    UNRESOLVED = "unresolved"
    ACTIVE_POOL = "active_pool"


class EventKind(str, Enum):
    LAUNCH = "launch"
    BUY = "buy"
    SELL = "sell"
    TRANSFER = "transfer"
    FEE_SHARE_CHANGE = "fee_share_change"
    MIGRATION = "migration"
    CORRECTION = "correction"


_VENUE_NETWORK = {
    VenueId.PUMPFUN_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
    VenueId.PONS_V1_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
}


def _required(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _utc(value: str, field_name: str) -> str:
    try:
        return require_utc_iso(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be aware UTC ISO-8601") from exc


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _atomic(value: int | None, field_name: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"{field_name} must be an integer or None")
    return value


def _coerce_identity(
    network_id: NetworkId,
    value: str | None,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    try:
        return normalize_identity(network_id.value, value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a non-empty identity") from exc


@dataclass(frozen=True)
class ChainOrder:
    block_or_slot: int
    transaction_index: int | None
    event_index: int
    block_hash: str | None

    def __post_init__(self) -> None:
        if isinstance(self.block_or_slot, bool) or not isinstance(self.block_or_slot, int) or self.block_or_slot < 0:
            raise ValueError("block_or_slot must be a non-negative integer")
        if self.transaction_index is not None and (
            isinstance(self.transaction_index, bool)
            or not isinstance(self.transaction_index, int)
            or self.transaction_index < 0
        ):
            raise ValueError("transaction_index must be a non-negative integer or None")
        if isinstance(self.event_index, bool) or not isinstance(self.event_index, int) or self.event_index < 0:
            raise ValueError("event_index must be a non-negative integer")
        if self.block_hash is not None:
            object.__setattr__(self, "block_hash", _required(self.block_hash, "block_hash"))


@dataclass(frozen=True)
class RawEvent:
    raw_event_id: str
    network_id: NetworkId
    venue_id: VenueId
    source_key: str
    source_provider: str
    source_cursor: str
    token_id: str
    transaction_id: str
    chain_order: ChainOrder
    chain_timestamp: str
    received_at: str
    confirmation: ConfirmationState
    raw_payload_json: str
    raw_payload_hash: str

    def __post_init__(self) -> None:
        network = NetworkId(self.network_id)
        venue = VenueId(self.venue_id)
        confirmation = ConfirmationState(self.confirmation)
        if _VENUE_NETWORK[venue] != network:
            raise ValueError("venue_id is incompatible with network_id")
        object.__setattr__(self, "network_id", network)
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "confirmation", confirmation)
        for field_name in (
            "raw_event_id",
            "source_key",
            "source_provider",
            "source_cursor",
        ):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "token_id", _coerce_identity(network, self.token_id, "token_id"))
        object.__setattr__(
            self,
            "transaction_id",
            _coerce_identity(network, self.transaction_id, "transaction_id"),
        )
        if not isinstance(self.chain_order, ChainOrder):
            raise ValueError("chain_order must be ChainOrder")
        if self.chain_order.block_hash is not None:
            object.__setattr__(
                self,
                "chain_order",
                ChainOrder(
                    self.chain_order.block_or_slot,
                    self.chain_order.transaction_index,
                    self.chain_order.event_index,
                    _coerce_identity(network, self.chain_order.block_hash, "block_hash"),
                ),
            )
        object.__setattr__(self, "chain_timestamp", _utc(self.chain_timestamp, "chain_timestamp"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        object.__setattr__(self, "raw_payload_json", _required(self.raw_payload_json, "raw_payload_json"))
        object.__setattr__(self, "raw_payload_hash", _required(self.raw_payload_hash, "raw_payload_hash").lower())
        actual_hash = hashlib.sha256(self.raw_payload_json.encode("utf-8")).hexdigest()
        if actual_hash != self.raw_payload_hash:
            raise ValueError("raw_payload_hash does not match raw_payload_json")


@dataclass(frozen=True)
class CanonicalEvent:
    canonical_event_id: str
    raw_event_id: str
    launch_id: str
    network_id: NetworkId
    venue_id: VenueId
    token_id: str
    lifecycle: LifecycleState
    event_kind: EventKind
    transaction_id: str
    chain_order: ChainOrder
    chain_timestamp: str
    received_at: str
    decoded_at: str
    state_applied_at: str
    confirmation: ConfirmationState
    actor_id: str | None
    quote_delta_atomic: int | None
    token_delta_atomic: int | None
    progress_bps: int | None
    creator_linked: bool | None
    decoder_version: str
    correction_of: str | None = None

    def __post_init__(self) -> None:
        network = NetworkId(self.network_id)
        venue = VenueId(self.venue_id)
        lifecycle = LifecycleState(self.lifecycle)
        event_kind = EventKind(self.event_kind)
        confirmation = ConfirmationState(self.confirmation)
        if _VENUE_NETWORK[venue] != network:
            raise ValueError("venue_id is incompatible with network_id")
        object.__setattr__(self, "network_id", network)
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "confirmation", confirmation)
        for field_name in ("canonical_event_id", "raw_event_id", "launch_id", "decoder_version"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "token_id", _coerce_identity(network, self.token_id, "token_id"))
        object.__setattr__(
            self,
            "transaction_id",
            _coerce_identity(network, self.transaction_id, "transaction_id"),
        )
        object.__setattr__(self, "actor_id", _coerce_identity(network, self.actor_id, "actor_id"))
        if not isinstance(self.chain_order, ChainOrder):
            raise ValueError("chain_order must be ChainOrder")
        if self.chain_order.block_hash is not None:
            object.__setattr__(
                self,
                "chain_order",
                ChainOrder(
                    self.chain_order.block_or_slot,
                    self.chain_order.transaction_index,
                    self.chain_order.event_index,
                    _coerce_identity(network, self.chain_order.block_hash, "block_hash"),
                ),
            )
        for field_name in ("chain_timestamp", "received_at", "decoded_at", "state_applied_at"):
            object.__setattr__(self, field_name, _utc(getattr(self, field_name), field_name))
        if _as_datetime(self.decoded_at) < _as_datetime(self.received_at):
            raise ValueError("decoded_at must not precede received_at")
        if _as_datetime(self.state_applied_at) < _as_datetime(self.decoded_at):
            raise ValueError("state_applied_at must not precede decoded_at")
        object.__setattr__(
            self,
            "quote_delta_atomic",
            _atomic(self.quote_delta_atomic, "quote_delta_atomic"),
        )
        object.__setattr__(
            self,
            "token_delta_atomic",
            _atomic(self.token_delta_atomic, "token_delta_atomic"),
        )
        if self.progress_bps is not None and (
            isinstance(self.progress_bps, bool)
            or not isinstance(self.progress_bps, int)
            or not 0 <= self.progress_bps <= 10_000
        ):
            raise ValueError("progress_bps must be an integer from 0 through 10000 or None")
        if self.creator_linked is not None and not isinstance(self.creator_linked, bool):
            raise ValueError("creator_linked must be boolean or None")
        if event_kind == EventKind.CORRECTION and not self.correction_of:
            raise ValueError("correction_of is required for correction events")
        if self.correction_of is not None:
            object.__setattr__(self, "correction_of", _required(self.correction_of, "correction_of"))
