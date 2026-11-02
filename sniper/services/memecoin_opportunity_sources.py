"""Transport-neutral opportunity source ingestion.

Live WebSocket/RPC loops may build :class:`OpportunitySourceEnvelope` values,
but this module's core path is synchronous and injected so replay uses exactly
the same decoder and persistence logic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from sniper.research.memecoin_opportunity.bags_4663 import decode_bags_4663_log
from sniper.research.memecoin_opportunity.bags_solana import decode_bags_solana_event
from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.identity import canonical_json, content_fingerprint
from sniper.research.memecoin_opportunity.pumpfun import decode_pumpfun_event
from sniper.research.memecoin_opportunity.pons_4663 import (
    decode_pons_4663_log,
    remember_pool,
)
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    NetworkId,
    RawEvent,
    VenueId,
)


_NETWORK_BY_VENUE = {
    VenueId.PUMPFUN_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
    VenueId.PONS_V1_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
}


@dataclass
class VenueIdentityMap:
    token_by_solana_pool: dict[str, str] = field(default_factory=dict)
    token_by_4663_contract: dict[str, str] = field(default_factory=dict)
    pons_pool_tokens: dict[str, tuple[str, str]] = field(default_factory=dict)
    creator_by_token: dict[tuple[str, str], str] = field(default_factory=dict)

    def learn(self, venue: VenueId, decoded: DecodedVenueEvent) -> None:
        if not decoded.token_id:
            return
        if venue is VenueId.BAGS_SOLANA and decoded.curve_id:
            self.token_by_solana_pool[decoded.curve_id] = decoded.token_id
        if venue is VenueId.BAGS_ROBINHOOD_4663:
            for contract in (decoded.curve_id, decoded.fee_share_id):
                if contract:
                    self.token_by_4663_contract[contract.lower()] = decoded.token_id.lower()
        if venue is VenueId.PONS_V1_ROBINHOOD_4663 and decoded.pool_id:
            pair_token = decoded.facts.get("pair_token")
            if pair_token:
                remember_pool(
                    self.pons_pool_tokens,
                    decoded.pool_id,
                    decoded.token_id,
                    str(pair_token),
                )
        if decoded.creator_id:
            self.creator_by_token[(venue.value, decoded.token_id)] = decoded.creator_id


@dataclass(frozen=True)
class OpportunitySourceEnvelope:
    source_key: str
    source_provider: str
    source_cursor: str
    venue_id: VenueId
    program_or_contract_id: str
    transaction_id: str
    block_or_slot: int
    transaction_index: int | None
    event_index: int
    block_hash: str | None
    chain_timestamp: str
    received_at: str
    confirmation: ConfirmationState
    payload: Mapping[str, object]
    token_hint: str
    existing_raw_event_id: str | None = None

    def __post_init__(self) -> None:
        venue = VenueId(self.venue_id)
        object.__setattr__(self, "venue_id", venue)
        object.__setattr__(self, "confirmation", ConfirmationState(self.confirmation))
        payload_json = canonical_json(dict(self.payload))
        object.__setattr__(self, "raw_payload_json", payload_json)
        identity = {
            "source_key": self.source_key,
            "source_provider": self.source_provider,
            "source_cursor": self.source_cursor,
            "venue_id": venue.value,
            "program_or_contract_id": self.program_or_contract_id,
            "token_hint": self.token_hint,
            "transaction_id": self.transaction_id,
            "block_or_slot": self.block_or_slot,
            "transaction_index": self.transaction_index,
            "event_index": self.event_index,
            "block_hash": self.block_hash,
            "chain_timestamp": self.chain_timestamp,
            "received_at": self.received_at,
            "confirmation": self.confirmation.value,
            "payload": dict(self.payload),
        }
        raw_event_id = self.existing_raw_event_id or content_fingerprint(identity)
        if not str(raw_event_id).strip():
            raise ValueError("existing_raw_event_id must be nonempty when supplied")
        object.__setattr__(self, "raw_event_id", str(raw_event_id))


@dataclass(frozen=True)
class ProcessEnvelopeResult:
    raw_event_id: str
    canonical_event_id: str | None
    raw_inserted: bool
    canonical_inserted: bool
    blocker: str | None
    decoded: DecodedVenueEvent


def _decode(envelope: OpportunitySourceEnvelope, identities: VenueIdentityMap) -> DecodedVenueEvent:
    payload = envelope.payload
    if envelope.venue_id is VenueId.PUMPFUN_SOLANA:
        return decode_pumpfun_event(
            envelope.program_or_contract_id,
            str(payload.get("log_line") or ""),
        )
    if envelope.venue_id is VenueId.BAGS_SOLANA:
        token_by_pool = dict(identities.token_by_solana_pool)
        pool_hint = str(payload.get("pool_id") or "")
        if pool_hint and envelope.token_hint:
            token_by_pool[pool_hint] = envelope.token_hint
        return decode_bags_solana_event(
            envelope.program_or_contract_id,
            str(payload.get("log_line") or ""),
            token_by_pool=token_by_pool,
            actor_id=str(payload["actor_id"]) if payload.get("actor_id") else None,
        )
    if envelope.venue_id is VenueId.PONS_V1_ROBINHOOD_4663:
        pool_tokens = dict(identities.pons_pool_tokens)
        hinted_token = payload.get("pons_token_id")
        hinted_pair = payload.get("pons_pair_token")
        if hinted_token and hinted_pair:
            pool_tokens[envelope.program_or_contract_id.lower()] = (
                str(hinted_token).lower(), str(hinted_pair).lower()
            )
        return decode_pons_4663_log(
            payload.get("log") if isinstance(payload.get("log"), Mapping) else payload,
            pool_tokens=pool_tokens,
        )
    return decode_bags_4663_log(
        payload.get("log") if isinstance(payload.get("log"), Mapping) else payload,
        token_by_contract=identities.token_by_4663_contract,
    )


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("opportunity source timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _outside_capture_window(
    conn,
    *,
    network,
    venue,
    decoded,
    chain_timestamp,
    window_s,
    candidate_window_s=None,
) -> bool:
    if window_s is None or decoded.event_kind is EventKind.LAUNCH or not decoded.token_id:
        return False
    launch = conn.execute(
        "SELECT launch_id,launched_at FROM op_launches "
        "WHERE network_id=? AND venue_id=? AND token_id=?",
        (network.value, venue.value, decoded.token_id),
    ).fetchone()
    if launch is None:
        return False
    effective_window_s = int(window_s)
    if candidate_window_s is not None and conn.execute(
        "SELECT 1 FROM op_forward_alerts WHERE launch_id=? "
        "AND rule_version IN ('pump-ignition-v1','pump-confirmation-v1') LIMIT 1",
        (launch["launch_id"],),
    ).fetchone() is not None:
        effective_window_s = int(candidate_window_s)
    age_s = (_utc(chain_timestamp) - _utc(launch["launched_at"])).total_seconds()
    return age_s > effective_window_s


def process_envelope(
    conn,
    envelope: OpportunitySourceEnvelope,
    *,
    identities: VenueIdentityMap,
    decoded_enricher: Callable[[OpportunitySourceEnvelope, DecodedVenueEvent], DecodedVenueEvent]
    | None = None,
    capture_window_s: int | None = None,
    candidate_capture_window_s: int | None = None,
) -> ProcessEnvelopeResult:
    network = _NETWORK_BY_VENUE[envelope.venue_id]
    order = ChainOrder(
        envelope.block_or_slot,
        envelope.transaction_index,
        envelope.event_index,
        envelope.block_hash,
    )
    raw = RawEvent(
        raw_event_id=envelope.raw_event_id,
        network_id=network,
        venue_id=envelope.venue_id,
        source_key=envelope.source_key,
        source_provider=envelope.source_provider,
        source_cursor=envelope.source_cursor,
        token_id=envelope.token_hint,
        transaction_id=envelope.transaction_id,
        chain_order=order,
        chain_timestamp=envelope.chain_timestamp,
        received_at=envelope.received_at,
        confirmation=envelope.confirmation,
        raw_payload_json=envelope.raw_payload_json,
        raw_payload_hash=hashlib.sha256(envelope.raw_payload_json.encode()).hexdigest(),
    )
    decoded = _decode(envelope, identities)
    if (
        decoded.blocker is None
        and decoded.event_kind is not None
        and _outside_capture_window(
            conn,
            network=network,
            venue=envelope.venue_id,
            decoded=decoded,
            chain_timestamp=envelope.chain_timestamp,
            window_s=capture_window_s,
            candidate_window_s=candidate_capture_window_s,
        )
    ):
        return ProcessEnvelopeResult(
            raw.raw_event_id,
            None,
            append_raw_event(conn, raw),
            False,
            None,
            decoded,
        )
    raw_inserted = append_raw_event(conn, raw)
    if decoded_enricher is not None and decoded.blocker is None:
        decoded = decoded_enricher(envelope, decoded)
    if decoded.blocker is not None or decoded.event_kind is None or decoded.token_id is None:
        return ProcessEnvelopeResult(
            raw.raw_event_id,
            None,
            raw_inserted,
            False,
            decoded.blocker.value if decoded.blocker else "DECODER_FAILURE",
            decoded,
        )
    identities.learn(envelope.venue_id, decoded)
    launch_id = content_fingerprint(
        {"network_id": network.value, "venue_id": envelope.venue_id.value, "token_id": decoded.token_id}
    )
    canonical_id = content_fingerprint(
        {
            "network_id": network.value,
            "venue_id": envelope.venue_id.value,
            "transaction_id": envelope.transaction_id,
            "block_or_slot": envelope.block_or_slot,
            "transaction_index": envelope.transaction_index,
            "event_index": envelope.event_index,
            "block_hash": envelope.block_hash,
            "decoder_version": decoded.decoder_version,
            "event_kind": decoded.event_kind.value,
            "token_id": decoded.token_id,
        }
    )
    creator = decoded.creator_id or identities.creator_by_token.get(
        (envelope.venue_id.value, decoded.token_id)
    )
    creator_linked = None
    if creator is not None and decoded.actor_id is not None:
        creator_linked = creator == decoded.actor_id
    canonical = CanonicalEvent(
        canonical_event_id=canonical_id,
        raw_event_id=raw.raw_event_id,
        launch_id=launch_id,
        network_id=network,
        venue_id=envelope.venue_id,
        token_id=decoded.token_id,
        lifecycle=decoded.lifecycle,
        event_kind=decoded.event_kind,
        transaction_id=envelope.transaction_id,
        chain_order=order,
        chain_timestamp=envelope.chain_timestamp,
        received_at=envelope.received_at,
        decoded_at=envelope.received_at,
        state_applied_at=envelope.received_at,
        confirmation=envelope.confirmation,
        actor_id=decoded.actor_id,
        quote_delta_atomic=decoded.quote_delta_atomic,
        token_delta_atomic=decoded.token_delta_atomic,
        progress_bps=decoded.progress_bps,
        creator_linked=creator_linked,
        decoder_version=decoded.decoder_version,
    )
    canonical_inserted = False
    if conn.execute(
        "SELECT 1 FROM op_canonical_events WHERE canonical_event_id=?", (canonical_id,)
    ).fetchone() is None:
        canonical_inserted = append_canonical_event(conn, canonical)
    from sniper.research.memecoin_opportunity.projection import persist_venue_event_facts

    persist_venue_event_facts(conn, canonical_id, decoded)
    if decoded.event_kind.value == "launch":
        upsert_launch_identity(
            conn,
            LaunchIdentity(
                launch_id=launch_id,
                network_id=network,
                venue_id=envelope.venue_id,
                token_id=decoded.token_id,
                creator_id=creator,
                launch_event_id=canonical_id,
                launched_at=envelope.chain_timestamp,
            ),
        )
    return ProcessEnvelopeResult(
        raw.raw_event_id,
        canonical_id,
        raw_inserted,
        canonical_inserted,
        None,
        decoded,
    )
