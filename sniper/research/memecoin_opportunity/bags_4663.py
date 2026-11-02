"""Pure ABI log decoder for Bags pre-migration contracts on Robinhood 4663."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from eth_abi import decode
from eth_utils import keccak

from .decoders import DecodeBlocker, DecodedVenueEvent, blocked
from .pons_4663 import PONS_WETH
from .types import EventKind, LifecycleState


BAGS_FACTORY = "0xe8cc4431adf8b5a847c113ef0c6af9043219cb37"
BAGS_LENS = "0xc82db941daf90b754aecb5f7d14c683dc608d595"
BAGS_CURVE_BEACON = "0x8dcecaf516c828a493c2c449c1e25f92cf80207e"
BAGS_FEE_SHARE_BEACON = "0xdff07d39c5332c602e06fa64f0a97c92fd8537e0"
DECODER_VERSION = "bags-robinhood-abi-v2-2026-07-26"


def _topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


TOKEN_CREATED = _topic(
    "TokenCreated(address,address,address,address,address,bytes32,string,string,string)"
)
TOKENS_BOUGHT = _topic(
    "TokensBought(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
)
TOKENS_SOLD = _topic(
    "TokensSold(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
)
MIGRATED = _topic("Migrated(address,address,address,uint256,uint256,bytes32,uint160)")
CLAIMERS_UPDATED = _topic("ClaimersUpdated(address[],uint16[])")
UPGRADED = _topic("Upgraded(address)")


def enrich_bags_4663_state(
    decoded: DecodedVenueEvent,
    lens_state: Mapping[str, object],
) -> DecodedVenueEvent:
    """Attach an exact historical BagsLens state to one decoded chain event."""
    if not lens_state.get("exists"):
        raise ValueError("BagsLens token state does not exist")
    curve = str(lens_state.get("curve") or "").lower()
    if decoded.curve_id and curve != decoded.curve_id.lower():
        raise ValueError("BagsLens curve identity conflicts with event")
    migrated = bool(lens_state.get("migrated"))
    facts = {
        **dict(decoded.facts),
        "lens_threshold_quote": int(lens_state["threshold_quote"]),
        "lens_total_raised": int(lens_state["total_raised"]),
        "lens_price_quote_per_token": int(lens_state["price_quote_per_token"]),
        "lens_state_block_scoped": True,
        "state_enrichment_status": "available",
    }
    return replace(
        decoded,
        lifecycle=(
            LifecycleState.MIGRATED if migrated else LifecycleState.PRE_MIGRATION
        ),
        curve_id=curve,
        fee_share_id=str(lens_state.get("fee_share") or decoded.fee_share_id or "").lower() or None,
        pool_id=str(lens_state.get("pool_id") or decoded.pool_id or "").lower() or None,
        virtual_token_reserves=int(lens_state["virtual_token_reserves"]),
        virtual_quote_reserves=int(lens_state["virtual_quote_reserves"]),
        real_token_reserves=int(lens_state["real_token_reserves"]),
        real_quote_reserves=int(lens_state["real_quote_reserves"]),
        progress_bps=int(lens_state["progress_bps"]),
        price_atomic=int(lens_state["price_quote_per_token"]),
        facts=facts,
    )


def _address_topic(value: str) -> str:
    if not isinstance(value, str) or len(value) < 42:
        raise ValueError("invalid indexed address")
    return "0x" + value[-40:].lower()


def _data(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid log data")
    return bytes.fromhex(value[2:])


def is_untracked_bags_4663_curve_log(
    log: Mapping[str, object], token_by_contract: Mapping[str, str]
) -> bool:
    address = str(log.get("address") or "").lower()
    topics = [str(value).lower() for value in (log.get("topics") or [])]
    if not topics:
        return False
    if topics[0] == TOKEN_CREATED:
        return address != BAGS_FACTORY
    return (
        topics[0] in {TOKENS_BOUGHT, TOKENS_SOLD, MIGRATED, CLAIMERS_UPDATED}
        and address not in {str(key).lower() for key in token_by_contract}
    )


def decode_bags_4663_log(
    log: Mapping[str, object],
    *,
    token_by_contract: Mapping[str, str] | None = None,
) -> DecodedVenueEvent:
    token_by_contract = {str(k).lower(): str(v).lower() for k, v in (token_by_contract or {}).items()}
    address = str(log.get("address") or "").lower()
    topics = [str(value).lower() for value in (log.get("topics") or [])]
    if not topics:
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
    head = topics[0]
    if address in (BAGS_FACTORY, BAGS_CURVE_BEACON, BAGS_FEE_SHARE_BEACON) and head == UPGRADED:
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    try:
        if address == BAGS_FACTORY and head == TOKEN_CREATED and len(topics) >= 4:
            token, curve, creator = map(_address_topic, topics[1:4])
            fee_share, partner, pool_id, name, symbol, uri = decode(
                ["address", "address", "bytes32", "string", "string", "string"], _data(str(log.get("data")))
            )
            return DecodedVenueEvent(
                event_kind=EventKind.LAUNCH,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=token,
                curve_id=curve,
                creator_id=creator,
                fee_share_id=str(fee_share).lower(),
                pool_id="0x" + bytes(pool_id).hex(),
                decoder_version=DECODER_VERSION,
                facts={
                    "partner": str(partner).lower(),
                    "name": name,
                    "symbol": symbol,
                    "uri": uri,
                    "quote_mint": PONS_WETH,
                },
            )
        token = token_by_contract.get(address)
        if token is None:
            return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
        if head == TOKENS_BOUGHT and len(topics) >= 3:
            buyer, recipient = _address_topic(topics[1]), _address_topic(topics[2])
            values = decode(["uint256"] * 10, _data(str(log.get("data"))))
            gross, net, tokens, fee, vault_fee, creator_fee, refund, price, vtoken, vquote = values
            return DecodedVenueEvent(
                event_kind=EventKind.BUY,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=token,
                curve_id=address,
                actor_id=buyer,
                recipient_id=recipient,
                quote_delta_atomic=int(gross),
                token_delta_atomic=-int(tokens),
                fee_atomic=int(fee),
                virtual_token_reserves=int(vtoken),
                virtual_quote_reserves=int(vquote),
                price_atomic=int(price),
                decoder_version=DECODER_VERSION,
                facts={
                    "net_quote_in": int(net),
                    "vault_fee": int(vault_fee),
                    "creator_fee": int(creator_fee),
                    "refund": int(refund),
                    "quote_mint": PONS_WETH,
                },
            )
        if head == TOKENS_SOLD and len(topics) >= 3:
            seller, recipient = _address_topic(topics[1]), _address_topic(topics[2])
            values = decode(["uint256"] * 9, _data(str(log.get("data"))))
            tokens, gross, net, fee, vault_fee, creator_fee, price, vtoken, vquote = values
            return DecodedVenueEvent(
                event_kind=EventKind.SELL,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=token,
                curve_id=address,
                actor_id=seller,
                recipient_id=recipient,
                quote_delta_atomic=-int(gross),
                token_delta_atomic=int(tokens),
                fee_atomic=int(fee),
                virtual_token_reserves=int(vtoken),
                virtual_quote_reserves=int(vquote),
                price_atomic=int(price),
                decoder_version=DECODER_VERSION,
                facts={
                    "net_quote_out": int(net),
                    "vault_fee": int(vault_fee),
                    "creator_fee": int(creator_fee),
                    "quote_mint": PONS_WETH,
                },
            )
        if head == MIGRATED and len(topics) >= 4:
            creator, platform_admin, migrated_token = map(_address_topic, topics[1:4])
            lp_quote, lp_tokens, pool_id, sqrt_price = decode(
                ["uint256", "uint256", "bytes32", "uint160"], _data(str(log.get("data")))
            )
            if migrated_token != token:
                return blocked(DecodeBlocker.MISSING_IDENTITY, DECODER_VERSION)
            return DecodedVenueEvent(
                event_kind=EventKind.MIGRATION,
                lifecycle=LifecycleState.MIGRATED,
                token_id=token,
                curve_id=address,
                creator_id=creator,
                pool_id="0x" + bytes(pool_id).hex(),
                decoder_version=DECODER_VERSION,
                facts={"platform_admin": platform_admin, "lp_quote": int(lp_quote), "lp_tokens": int(lp_tokens), "sqrt_price_x96": int(sqrt_price)},
            )
        if head == CLAIMERS_UPDATED:
            claimers, bps = decode(["address[]", "uint16[]"], _data(str(log.get("data"))))
            return DecodedVenueEvent(
                event_kind=EventKind.FEE_SHARE_CHANGE,
                lifecycle=LifecycleState.PRE_MIGRATION,
                token_id=token,
                fee_share_id=address,
                decoder_version=DECODER_VERSION,
                facts={"claimers": [str(item).lower() for item in claimers], "bps": list(map(int, bps))},
            )
        return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
    except (ValueError, TypeError, OverflowError):
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
