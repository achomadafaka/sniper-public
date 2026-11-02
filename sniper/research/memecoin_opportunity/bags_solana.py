"""Versioned Meteora Dynamic Bonding Curve event decoder used by Bags."""

from __future__ import annotations

import hashlib
from typing import Mapping

from .decoders import (
    BorshReader,
    DecodeBlocker,
    DecodedVenueEvent,
    anchor_event_bytes,
    blocked,
)
from .types import EventKind, LifecycleState


BAGS_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
DECODER_VERSION = "meteora-dbc-idl-0.1.5-2026-07-26"

_INITIALIZE = bytes([228, 50, 246, 85, 203, 66, 134, 37])
_SWAP = bytes([27, 60, 21, 213, 138, 170, 187, 147])
_COMPLETE = bytes([229, 231, 86, 84, 156, 134, 75, 24])
_KNOWN_IRRELEVANT_EVENTS = frozenset(
    {
        hashlib.sha256(b"event:EvtClaimProtocolFee2").digest()[:8],
        # Ordinary DBC swaps emit backward-compatible EvtSwap and EvtSwap2
        # together. EvtSwap is the canonical receipt for this decoder.
        hashlib.sha256(b"event:EvtSwap2").digest()[:8],
    }
)


def is_known_irrelevant_bags_solana_event(log_line: str) -> bool:
    """Recognize named official DBC events outside the launch/trade lifecycle."""
    try:
        return anchor_event_bytes(log_line)[:8] in _KNOWN_IRRELEVANT_EVENTS
    except ValueError:
        return False


def decode_bags_solana_event(
    program_id: str,
    log_line: str,
    *,
    token_by_pool: Mapping[str, str] | None = None,
    actor_id: str | None = None,
) -> DecodedVenueEvent:
    if program_id != BAGS_DBC_PROGRAM:
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    token_by_pool = token_by_pool or {}
    try:
        raw = anchor_event_bytes(log_line)
        discriminator, reader = raw[:8], BorshReader(raw[8:])
        if discriminator == _INITIALIZE:
            pool, config, creator, mint = (
                reader.pubkey(), reader.pubkey(), reader.pubkey(), reader.pubkey()
            )
            pool_type, activation_point = reader.u8(), reader.u64()
            return DecodedVenueEvent(
                event_kind=EventKind.LAUNCH,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=mint,
                curve_id=pool,
                creator_id=creator,
                timestamp_s=activation_point,
                decoder_version=DECODER_VERSION,
                facts={"config": config, "pool_type": pool_type},
            )
        if discriminator == _SWAP:
            pool, config = reader.pubkey(), reader.pubkey()
            direction, has_referral = reader.u8(), reader.bool()
            requested_in, minimum_out = reader.u64(), reader.u64()
            actual_in, output = reader.u64(), reader.u64()
            next_sqrt_price = reader.u128()
            trading_fee, protocol_fee, referral_fee = reader.u64(), reader.u64(), reader.u64()
            amount_in, timestamp = reader.u64(), reader.u64()
            token = token_by_pool.get(pool)
            if token is None:
                return blocked(DecodeBlocker.MISSING_IDENTITY, DECODER_VERSION)
            if direction not in (0, 1):
                return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
            is_buy = direction == 1
            return DecodedVenueEvent(
                event_kind=EventKind.BUY if is_buy else EventKind.SELL,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=token,
                curve_id=pool,
                actor_id=actor_id,
                quote_delta_atomic=actual_in if is_buy else -output,
                token_delta_atomic=-output if is_buy else actual_in,
                fee_atomic=trading_fee + protocol_fee + referral_fee,
                price_atomic=next_sqrt_price,
                timestamp_s=timestamp,
                decoder_version=DECODER_VERSION,
                facts={
                    "config": config,
                    "has_referral": has_referral,
                    "requested_amount_in": requested_in,
                    "minimum_amount_out": minimum_out,
                    "event_amount_in": amount_in,
                },
            )
        if discriminator == _COMPLETE:
            pool, config, base_reserve, quote_reserve = (
                reader.pubkey(), reader.pubkey(), reader.u64(), reader.u64()
            )
            token = token_by_pool.get(pool)
            if token is None:
                return blocked(DecodeBlocker.MISSING_IDENTITY, DECODER_VERSION)
            return DecodedVenueEvent(
                event_kind=EventKind.MIGRATION,
                lifecycle=LifecycleState.MIGRATED,
                token_id=token,
                curve_id=pool,
                real_token_reserves=base_reserve,
                real_quote_reserves=quote_reserve,
                decoder_version=DECODER_VERSION,
                facts={"config": config},
            )
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    except (ValueError, UnicodeDecodeError):
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
