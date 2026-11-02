"""Pure ABI decoder for Pons V1 launches and registered Uniswap V3 pools."""

from __future__ import annotations

from typing import Mapping, MutableMapping

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from .decoders import DecodeBlocker, DecodedVenueEvent, blocked
from .types import EventKind, LifecycleState


PONS_FACTORY = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
PONS_START_BLOCK = 8_991_118
PONS_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
DECODER_VERSION = "pons-v1-abi-v1-2026-07-29"
TRACKED_POOL_LIMIT = 2_048


def _topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


TOKEN_LAUNCHED = _topic(
    "TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)"
)
V3_SWAP = _topic("Swap(address,address,int256,int256,uint160,uint128,int24)")


def is_known_irrelevant_pons_swap(log: Mapping[str, object]) -> bool:
    """Return true only for a valid V3 Swap with no economic flow."""
    topics = [str(value).lower() for value in (log.get("topics") or [])]
    if len(topics) != 3 or topics[0] != V3_SWAP:
        return False
    try:
        tuple(_address_topic(value) for value in topics[1:])
        raw = _data(log.get("data"))
        if len(raw) != 32 * 5:
            return False
        amount0, amount1, *_rest = decode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            raw,
        )
        return int(amount0) == 0 or int(amount1) == 0
    except (DecodingError, ValueError, TypeError, OverflowError):
        return False


def remember_pool(
    pools: MutableMapping[str, tuple[str, str]],
    pool: str,
    token: str,
    pair_token: str,
) -> None:
    """Keep a bounded newest-first registry for scoped V3 log filters."""
    key = pool.lower()
    pools.pop(key, None)
    pools[key] = (token.lower(), pair_token.lower())
    while len(pools) > TRACKED_POOL_LIMIT:
        pools.pop(next(iter(pools)))


def _address_topic(value: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError("invalid indexed address")
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32 or any(raw[:12]):
        raise ValueError("invalid indexed address padding")
    return "0x" + raw[-20:].hex()


def _data(value: object) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid log data")
    return bytes.fromhex(value[2:])


def decode_pons_4663_log(
    log: Mapping[str, object],
    *,
    pool_tokens: Mapping[str, tuple[str, str]] | None = None,
) -> DecodedVenueEvent:
    pools = {
        str(pool).lower(): (str(token).lower(), str(pair).lower())
        for pool, (token, pair) in (pool_tokens or {}).items()
    }
    address = str(log.get("address") or "").lower()
    topics = [str(value).lower() for value in (log.get("topics") or [])]
    if not topics:
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
    try:
        if address == PONS_FACTORY and topics[0] == TOKEN_LAUNCHED and len(topics) == 4:
            token, deployer, dex_factory = map(_address_topic, topics[1:4])
            pair_token, pool, dex_id, config_id, position_id, restrictions_end, initial_buy = decode(
                ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256"],
                _data(log.get("data")),
            )
            pair_token = str(pair_token).lower()
            pool = str(pool).lower()
            return DecodedVenueEvent(
                event_kind=EventKind.LAUNCH,
                lifecycle=LifecycleState.ACTIVE_POOL,
                token_id=token,
                creator_id=deployer,
                pool_id=pool,
                decoder_version=DECODER_VERSION,
                facts={
                    "pair_token": pair_token,
                    "quote_mint": pair_token,
                    "dex_factory": dex_factory,
                    "dex_id": int(dex_id),
                    "launch_config_id": int(config_id),
                    "position_id": int(position_id),
                    "restrictions_end_block": int(restrictions_end),
                    "initial_buy_amount": int(initial_buy),
                    "token_is_token0": token < pair_token,
                },
            )
        identity = pools.get(address)
        if topics[0] != V3_SWAP or identity is None or len(topics) != 3:
            return blocked(DecodeBlocker.UNKNOWN_VERSION, DECODER_VERSION)
        token, pair_token = identity
        sender, recipient = map(_address_topic, topics[1:3])
        amount0, amount1, sqrt_price_x96, liquidity, tick = decode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            _data(log.get("data")),
        )
        token_is_token0 = token < pair_token
        token_delta = int(amount0 if token_is_token0 else amount1)
        quote_delta = int(amount1 if token_is_token0 else amount0)
        if quote_delta == 0 or token_delta == 0 or (quote_delta > 0) == (token_delta > 0):
            return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
        # The Pons router is the Swap sender. The recipient is the route beneficiary.
        return DecodedVenueEvent(
            event_kind=EventKind.BUY if quote_delta > 0 else EventKind.SELL,
            lifecycle=LifecycleState.ACTIVE_POOL,
            token_id=token,
            actor_id=recipient,
            recipient_id=recipient,
            pool_id=address,
            quote_delta_atomic=quote_delta,
            token_delta_atomic=token_delta,
            price_atomic=int(sqrt_price_x96),
            decoder_version=DECODER_VERSION,
            facts={
                "pair_token": pair_token,
                "quote_mint": pair_token,
                "swap_sender": sender,
                "token_is_token0": token_is_token0,
                "sqrt_price_x96": int(sqrt_price_x96),
                "liquidity": int(liquidity),
                "tick": int(tick),
            },
        )
    except (DecodingError, ValueError, TypeError, OverflowError):
        return blocked(DecodeBlocker.MALFORMED_PAYLOAD, DECODER_VERSION)
