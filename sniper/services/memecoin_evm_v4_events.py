"""Read-only Uniswap V4 pool identity and event normalization.

V4 pools share one PoolManager address. Their durable market identity is the indexed PoolId,
not the emitting contract address. Rows are normalized to the existing concentrated-liquidity
shape so V6 grades V3 and V4 from the same amount, price, and active-liquidity facts.
"""
from __future__ import annotations

from collections.abc import Mapping

from eth_abi import encode
from eth_utils import keccak


# THE CANONICAL V4 PoolManager REGISTRY. `decode_v4_log` refuses any log whose emitter is not the
# manager listed here for that chain, so a chain absent from this map decodes NOTHING — which is
# why a second copy of these addresses must never exist: the writer would subscribe happily and
# every log would be dropped here, and an empty window reads as "no volume", not "no feed".
#
# 8453 (Base) verified on chain 2026-09-22 against base-rpc.publicnode.com: eth_chainId 0x2105,
# 24,009 bytes of code at the address, 1,687 V4 Swap logs across 309 distinct pool ids in 200
# blocks. Base memecoin liquidity is V4 rather than V3/V2 (V2 on Base totals $42, finding
# 2026-09-06), so this manager is the whole tape there.
V4_POOL_MANAGERS = {
    "1": "0x000000000004444c5dc75cb358380d2e3de08a90",
    "4663": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "5042": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "8453": "0x498581ff718922c3f8e6a244956af099b2652b2b",
}
V4_SWAP = "0x" + keccak(
    b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
).hex()
# Initialize carries the ONLY mint->pool mapping a V4 pool has. A V4 pool is not a contract, it
# is a key inside the PoolManager singleton, so `token0()`/`token1()` -- how pool_meta resolves
# V2/V3 pools -- have nothing to answer. The pool id and BOTH currencies are indexed topics here.
# Verified on chain 2026-08-30: 4 topics, 66-char pool id in topics[1], currencies in [2] and [3].
V4_INITIALIZE = "0x" + keccak(
    b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
).hex()
V4_MODIFY_LIQUIDITY = "0x" + keccak(
    b"ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
).hex()

_UINT256 = 1 << 256
_INT256_SIGN = 1 << 255


def _signed(word: str) -> int:
    value = int(word, 16)
    return value - _UINT256 if value >= _INT256_SIGN else value


def _address(value) -> str:
    text = str(value or "").lower()
    if not text.startswith("0x") or len(text) != 42:
        raise ValueError("V4 currency/hook must be a 20-byte address")
    int(text[2:], 16)
    return text


def v4_pool_id(pool_key: Mapping) -> str:
    """Canonical ``keccak256(abi.encode(PoolKey))`` identity."""
    if not isinstance(pool_key, Mapping):
        raise ValueError("V4 pool_key must be a mapping")
    try:
        currency0 = _address(pool_key["currency0"])
        currency1 = _address(pool_key["currency1"])
        fee = int(pool_key["fee"])
        tick_spacing = int(pool_key["tick_spacing"])
        hooks = _address(pool_key.get("hooks") or "0x" + "00" * 20)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid V4 PoolKey") from exc
    if not 0 <= fee < (1 << 24) or not -(1 << 23) <= tick_spacing < (1 << 23):
        raise ValueError("V4 PoolKey fee/tick_spacing out of range")
    if int(currency0, 16) >= int(currency1, 16):
        raise ValueError("V4 PoolKey currencies are not strictly ordered")
    return "0x" + keccak(encode(
        ["address", "address", "uint24", "int24", "address"],
        [currency0, currency1, fee, tick_spacing, hooks],
    )).hex()


def decode_v4_log(chain_id, log, observed_at):
    """Normalize PoolManager Swap/ModifyLiquidity logs, or return ``None``."""
    manager = V4_POOL_MANAGERS.get(str(chain_id))
    if manager is None or str(log.get("address") or "").lower() != manager:
        return None
    topics = log.get("topics") or ()
    if len(topics) < 3 or topics[0] not in {V4_SWAP, V4_MODIFY_LIQUIDITY}:
        return None
    pool = str(topics[1]).lower()
    if not pool.startswith("0x") or len(pool) != 66:
        return None
    data = str(log.get("data") or "0x")[2:]
    words = [data[index:index + 64] for index in range(0, len(data), 64)]
    base = {
        "chain_id": str(chain_id), "pool": pool,
        "block_no": int(log["blockNumber"], 16) if log.get("blockNumber") else None,
        "tx_sig": log.get("transactionHash"),
        "log_index": int(log["logIndex"], 16) if log.get("logIndex") is not None else None,
        "block_hash": log.get("blockHash"), "observed_at": observed_at,
        "maker": "0x" + str(topics[2])[-40:].lower(),
    }
    try:
        if topics[0] == V4_SWAP and len(words) >= 6:
            # ⚠️ NAMING TRAP: `v3_swap` IS THE KIND FOR BOTH V3 AND V4 ROWS. A V4 Swap carries the
            # same data shape as a V3 one, and the label was never split, so every reader that
            # filters `kind='v3_swap'` is reading V3 AND V4 together — which is what they all want,
            # and why the label has survived. THE DISCRIMINATOR IS THE POOL ID LENGTH: a V3 pool is
            # a 20-byte address (42 chars) and a V4 pool id is 32 bytes (66 chars).
            # `resolve_v3_pools` depends on exactly that, because without it every batch is spent
            # on V4 ids it cannot resolve.
            # Renaming this to `v4_swap` was costed on 2026-09-23 and PARKED: about 20 production
            # readers filter on the kind, several on funded exit paths, and every historical V4 row
            # already carries `v3_swap`, so a rename needs a shared constant accepting both labels
            # rather than a per-site swap. The label is wrong; the data is not.
            # INTO-POOL SIGN (2026-09-29). PoolManager emits the SWAPPER's deltas (negative = paid
            # in); V3's Swap is the POOL's side, and the tape contract (db_ticks) is positive = INTO
            # the pool. Stored as-is, every flow reader (exit_features.pool_flow,
            # tick_features.compute_window_features, market_breadth) read V4 buys as sells, 100%
            # of 4663 V4 swaps. Negated HERE, the one point every V4 row passes through (tick
            # stream, volume tape, its backfill). Prices are unaffected: they read |quote|/|base|.
            # Rows written before this change keep the swapper sign until they age out.
            return {
                **base, "kind": "v3_swap",
                "amount0": str(-_signed(words[0])), "amount1": str(-_signed(words[1])),
                "sqrt_price_x96": str(int(words[2], 16)),
                "liquidity": str(int(words[3], 16)), "tick": _signed(words[4]),
            }
        if topics[0] == V4_MODIFY_LIQUIDITY and len(words) >= 3:
            delta = _signed(words[2])
            if delta == 0:
                return None
            return {
                **base, "kind": "v3_mint" if delta > 0 else "v3_burn",
                "amount0": None, "amount1": None,
                "tick_lower": _signed(words[0]), "tick_upper": _signed(words[1]),
                "liquidity": str(abs(delta)),
            }
    except (TypeError, ValueError):
        return None
    return None


def decode_v4_initialize(chain_id, log, observed_at):
    """A PoolManager `Initialize` log -> a pool_meta row, or None.

    Fail-closed on every mismatch: a foreign PoolManager would map pools we cannot trade, and a
    42-char id is a V2/V3 pool ADDRESS rather than a V4 pool key.
    """
    manager = V4_POOL_MANAGERS.get(str(chain_id))
    if manager is None or str(log.get("address") or "").lower() != manager:
        return None
    topics = log.get("topics") or ()
    if len(topics) < 4 or topics[0] != V4_INITIALIZE:
        return None
    pool = str(topics[1]).lower()
    if not pool.startswith("0x") or len(pool) != 66:
        return None
    return {
        "chain_id": str(chain_id), "pool": pool,
        "token0": "0x" + str(topics[2])[-40:].lower(),
        "token1": "0x" + str(topics[3])[-40:].lower(),
        "resolved_at": observed_at,
    }
