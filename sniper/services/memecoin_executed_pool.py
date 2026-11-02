"""The pool our fill ACTUALLY touched, read from our own receipt.

WHY IT IS NOT ALREADY RECORDED. `quote_route_target` is the router or settler we routed through,
not a pool (memecoin_exits.py:1191 says so in as many words). `evm_route_json.pool_id` is the pool
the QUOTE chose and it is present on 42% of funded EVM fills since 2026-09-15 -- absent entirely on
relay and aggregator routes, which have no single pool to name in advance. And a quoted pool is not
an executed one: BUDDY (2026-09-06) hit decoy pool 0x5bb3337725 while the real hook pool also
quoted, and the first exact exit mark four seconds after the fill was -61%.

THE RECEIPT IS THE ONLY WITNESS THAT CANNOT BE WRONG. It is what the chain did, after routing,
after any aggregator's internal path choice, and after a hook took its cut.

WHICH SWAP, WHEN A ROUTE HAS SEVERAL. The LAST one in log order. A multi-hop path ends at the pool
that delivered the coin we now hold, and that is the pool whose price our marks and our exit care
about; the earlier hops priced a bridge we no longer own. `swaps_seen` is reported so a reader can
tell a single-hop fill from a four-hop aggregator path rather than guessing from the venue name.
A SPLIT route (several pools sent the coin; 9177657c: 92% via one pool, the last Swap 4%) names the
swapping V2/V3 pool that transferred the most of it when the caller passes `mint`. Fee-on-transfer
legs count toward their own pool; a pool that sent the coin without a Swap here (skim, LP burn) is
not counted; a V4/Infinity leg pays out of its manager, so a route with one keeps the last Swap.

NOT AN ENTRY INPUT. Nothing here gates an entry, sizes a clip or signs. The stamped pool is an exit
INPUT all the same: the tick stream marks on it and volume_death counts it among our own pools. It
is evidence, so it fails to None rather than to a guess: a receipt we cannot read names no pool at
all. A malformed coin Transfer only drops the split-route share (the last Swap stands); strict raises.
"""
from __future__ import annotations

import logging

from eth_utils import keccak

logger = logging.getLogger(__name__)

# Uniswap V2 pairs and V3/V4 pools all emit `Swap`, with different shapes. V4's emitter is the
# singleton PoolManager and the pool is topic1; V2 and V3 emit FROM the pair/pool itself, so there
# the emitter IS the identity.
V2_SWAP = "0x" + keccak(b"Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP = "0x" + keccak(b"Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
# PancakeSwap V3 (BSC) adds two protocol-fee fields, so its Swap is a different topic.
PANCAKE_V3_SWAP = "0x" + keccak(
    b"Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)").hex()
TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()

SOURCE_V4 = "receipt_v4_swap"
SOURCE_INFINITY = "receipt_infinity_swap"
SOURCE_V3 = "receipt_v3_swap"
SOURCE_V2 = "receipt_v2_swap"
SOURCE_SOL_TAPE = "sol_position_pools"


def _topic0(log):
    topics = log.get("topics") or ()
    return str(topics[0]).lower() if topics else ""


def _coin_sent_by_pools(logs, mint, pools):
    """{pool: atomic amount of `mint` it transferred out}, over the swapping V2/V3 pools only. A
    malformed amount raises (the caller fails to None or, strict, raises)."""
    if not mint or not pools:
        return {}
    coin, sent = str(mint).lower(), {}
    for log in logs:
        topics = log.get("topics") or ()
        if (str(log.get("address") or "").lower() != coin or len(topics) != 3
                or str(topics[0]).lower() != TRANSFER_TOPIC):
            continue
        sender = "0x" + str(topics[1]).lower()[-40:]
        if sender in pools:
            sent[sender] = sent.get(sender, 0) + int(str(log.get("data") or ""), 16)
    return sent


def executed_pool_from_receipt(chain_id, receipt, *, strict=False, mint=None):
    """``(pool, source, swaps_seen)`` for the LAST swap in the receipt, or ``(None, None, 0)``.

    `pool` is a V4 pool id (32 bytes) or a V2/V3 pair address (20 bytes). The two are
    distinguishable by length and by `source`, and are never mixed into one identifier space.
    With `mint` (our own fill), a route whose swaps are all V2/V3 names the pool that sent the most
    of that coin instead; a V4 or Infinity leg pays out of its manager, so it keeps the last swap.
    """
    try:
        from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
        from sniper.execution.evm_pancake_infinity import MANAGER as INFINITY_MANAGER, SWAP_TOPIC
        manager = (V4_POOL_MANAGERS.get(str(chain_id)) or "").lower()
        logs = (receipt or {}).get("logs") or ()
        found = []
        for log in logs:
            topic = _topic0(log)
            address = str(log.get("address") or "").lower()
            topics = log.get("topics") or ()
            if topic == str(V4_SWAP).lower() and manager and address == manager:
                pool = str(topics[1]).lower() if len(topics) > 1 else ""
                if pool.startswith("0x") and len(pool) == 66:
                    int(pool[2:], 16)
                    found.append((pool, SOURCE_V4))
                elif strict:
                    raise ValueError("malformed V4 pool id")
            elif str(chain_id) == "56" and address == INFINITY_MANAGER and topic == SWAP_TOPIC:
                pool = str(topics[1]).lower() if len(topics) > 1 else ""
                if pool.startswith("0x") and len(pool) == 66:
                    int(pool[2:], 16)
                    found.append((pool, SOURCE_INFINITY))
                elif strict:
                    raise ValueError("malformed Infinity pool id")
            elif topic in (V3_SWAP, PANCAKE_V3_SWAP) and address.startswith("0x") \
                    and len(address) == 42:
                found.append((address, SOURCE_V3))
            elif topic == V2_SWAP and address.startswith("0x") and len(address) == 42:
                found.append((address, SOURCE_V2))
        if not found:
            return None, None, 0
        pool, source = found[-1]           # the hop that delivered the coin we now hold
        try:
            sent = _coin_sent_by_pools(logs, mint, {p for p, s in found if s in (SOURCE_V3, SOURCE_V2)})
        except (TypeError, ValueError):
            if strict:
                raise
            sent = {}                      # a malformed amount drops the share, not the pool
        if sent and all(s in (SOURCE_V3, SOURCE_V2) for _p, s in found):
            # A SPLIT ROUTE (Alpha 2026-10-17, 9177657c): several pools sent our coin, so the
            # last one is not the venue. The largest delivery is; a tie goes to the later swap.
            best = max(range(len(found)), key=lambda i: (sent.get(found[i][0], 0), i))
            if sent.get(found[best][0], 0) > 0:
                pool, source = found[best]
        return pool, source, len(found)
    except Exception:
        if strict:
            raise
        logger.exception("executed pool decode failed chain=%s", chain_id)
        return None, None, 0


def executed_pool_for_solana(tape_conn, position_id):
    """The pool the Solana tape resolved for this position, or ``(None, None)``.

    SOLANA IS NOT READ FROM THE RECEIPT HERE, and the difference is worth stating. `sol_position_pools`
    already resolves a position's venue from its own transaction (`resolution_tx`), so the work is
    done and lives in the tape database. This only carries it onto the position so both chains can
    be read from one place, and it labels the source so nobody mistakes it for a receipt decode.
    """
    if tape_conn is None or position_id is None:
        return None, None
    try:
        row = tape_conn.execute(
            "SELECT pool FROM sol_position_pools WHERE position_id=?", (int(position_id),)
        ).fetchone()
    except Exception:
        logger.exception("solana executed pool lookup failed position=%s", position_id)
        return None, None
    pool = (row[0] if row else None) or None
    return (str(pool), SOURCE_SOL_TAPE) if pool else (None, None)
