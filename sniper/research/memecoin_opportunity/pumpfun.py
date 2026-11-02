"""Versioned Pump.fun Anchor-event decoder."""

from __future__ import annotations

import hashlib

from .decoders import (
    BorshReader,
    DecodeBlocker,
    DecodedVenueEvent,
    anchor_event_bytes,
    blocked,
)
from .types import EventKind, LifecycleState


PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DECODER_VERSION = "pump-public-idl-0.1.0-2026-07-26"

_CREATE = bytes([27, 114, 169, 77, 222, 235, 99, 118])
_TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
_COMPLETE = bytes([95, 114, 97, 156, 212, 46, 152, 8])
_MIGRATED = bytes([189, 233, 93, 185, 92, 148, 234, 148])

_KNOWN_IRRELEVANT_EVENTS = frozenset(
    hashlib.sha256(f"event:{name}".encode()).digest()[:8]
    for name in (
        "AdminSetCreatorEvent",
        "AdminSetIdlAuthorityEvent",
        "AdminUpdateTokenIncentivesEvent",
        "ClaimCashbackEvent",
        "ClaimTokenIncentivesEvent",
        "CloseUserVolumeAccumulatorEvent",
        "CollectCreatorFeeEvent",
        "DistributeCreatorFeesEvent",
        "ExtendAccountEvent",
        "InitUserVolumeAccumulatorEvent",
        "MigrateBondingCurveCreatorEvent",
        "MinimumDistributableFeeEvent",
        "ReservedFeeRecipientsEvent",
        "SetCreatorEvent",
        "SetMetaplexCreatorEvent",
        "SetParamsEvent",
        "SyncUserVolumeAccumulatorEvent",
        "UpdateGlobalAuthorityEvent",
        "UpdateMayhemVirtualParamsEvent",
    )
)


def is_known_irrelevant_pumpfun_event(log_line: str) -> bool:
    """Return true only for named official Pump events outside the launch/trade lifecycle."""
    try:
        return anchor_event_bytes(log_line)[:8] in _KNOWN_IRRELEVANT_EVENTS
    except ValueError:
        return False


def decode_pumpfun_event(program_id: str, log_line: str) -> DecodedVenueEvent:
    if program_id != PUMPFUN_PROGRAM:
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    try:
        raw = anchor_event_bytes(log_line)
        discriminator, reader = raw[:8], BorshReader(raw[8:])
        if discriminator == _CREATE:
            name, symbol, uri = reader.string(), reader.string(), reader.string()
            mint, curve, user, creator = (
                reader.pubkey(), reader.pubkey(), reader.pubkey(), reader.pubkey()
            )
            timestamp = reader.i64()
            virtual_token = reader.u64()
            virtual_quote_legacy = reader.u64()
            real_token = reader.u64()
            total_supply = reader.u64()
            token_program = reader.pubkey()
            mayhem, cashback = reader.bool(), reader.bool()
            quote_mint = reader.pubkey()
            virtual_quote = reader.u64()
            return DecodedVenueEvent(
                event_kind=EventKind.LAUNCH,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=mint,
                curve_id=curve,
                actor_id=user,
                creator_id=creator,
                virtual_token_reserves=virtual_token,
                virtual_quote_reserves=virtual_quote,
                real_token_reserves=real_token,
                timestamp_s=timestamp,
                decoder_version=DECODER_VERSION,
                facts={
                    "name": name,
                    "symbol": symbol,
                    "uri": uri,
                    "token_total_supply": total_supply,
                    "token_program": token_program,
                    "quote_mint": quote_mint,
                    "legacy_virtual_sol_reserves": virtual_quote_legacy,
                    "mayhem_mode": mayhem,
                    "cashback_enabled": cashback,
                },
            )
        if discriminator == _TRADE:
            mint = reader.pubkey()
            quote_amount, token_amount, is_buy = reader.u64(), reader.u64(), reader.bool()
            user, timestamp = reader.pubkey(), reader.i64()
            virtual_quote, virtual_token = reader.u64(), reader.u64()
            real_quote, real_token = reader.u64(), reader.u64()
            fee_recipient, fee_bps, fee = reader.pubkey(), reader.u64(), reader.u64()
            creator, creator_fee_bps, creator_fee = reader.pubkey(), reader.u64(), reader.u64()
            return DecodedVenueEvent(
                event_kind=EventKind.BUY if is_buy else EventKind.SELL,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=mint,
                creator_id=creator,
                actor_id=user,
                quote_delta_atomic=quote_amount if is_buy else -quote_amount,
                token_delta_atomic=-token_amount if is_buy else token_amount,
                fee_atomic=fee + creator_fee,
                virtual_token_reserves=virtual_token,
                virtual_quote_reserves=virtual_quote,
                real_token_reserves=real_token,
                real_quote_reserves=real_quote,
                timestamp_s=timestamp,
                decoder_version=DECODER_VERSION,
                facts={
                    "fee_recipient": fee_recipient,
                    "fee_basis_points": fee_bps,
                    "creator_fee_basis_points": creator_fee_bps,
                },
            )
        if discriminator in (_COMPLETE, _MIGRATED):
            user, mint = reader.pubkey(), reader.pubkey()
            if discriminator == _COMPLETE:
                curve, timestamp, quote_mint = reader.pubkey(), reader.i64(), reader.pubkey()
                facts = {"quote_mint": quote_mint, "completion_kind": "curve_complete"}
            else:
                mint_amount, quote_amount, migration_fee = reader.u64(), reader.u64(), reader.u64()
                curve, timestamp, pool, quote_mint = (
                    reader.pubkey(), reader.i64(), reader.pubkey(), reader.pubkey()
                )
                facts = {
                    "quote_mint": quote_mint,
                    "pool": pool,
                    "mint_amount": mint_amount,
                    "quote_amount": quote_amount,
                    "migration_fee": migration_fee,
                    "completion_kind": "pump_amm_migrated",
                }
            return DecodedVenueEvent(
                event_kind=EventKind.MIGRATION,
                lifecycle=LifecycleState.MIGRATED,
                token_id=mint,
                curve_id=curve,
                actor_id=user,
                timestamp_s=timestamp,
                decoder_version=DECODER_VERSION,
                facts=facts,
            )
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    except (ValueError, UnicodeDecodeError):
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
