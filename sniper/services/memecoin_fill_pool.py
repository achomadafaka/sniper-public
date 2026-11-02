"""Fill-anchored pool resolution: the entry receipt names the pool a position actually traded.

Mint-keyed resolution (`resolve_pool_for_position`) refuses whenever a mint maps to more than one
usable pool, and on chain 4663 that is most of them — measured 2026-09-02, it answered for 1 of the
8 most recent funded entries. A mint is one-to-many; a fill is not. The receipt of the entry swap
names the pool by construction, is immutable, and therefore caches for ever.

WHY THE OBVIOUS SHORTCUTS DO NOT WORK, each rejected on measurement rather than taste:

  - "take the pool from the swap log" — a routed entry emits SEVERAL Swap logs (observed 3 swaps
    across 3 distinct pools). First or last would pick a hop we do not hold.
  - "narrow the candidates with pool_meta" — pool_meta knew nothing about 6 of those 8 mints, so it
    narrowed 1 of 8. It is the same coverage gap this exists to route around.
  - "match the pool against a Transfer of our mint" — correct for V2/V3, where the pool contract is
    the counterparty. USELESS for V4: the log is emitted by the PoolManager singleton, so no pool id
    ever appears in a Transfer.

So the two families are identified differently, and both tests are needed:

  V2/V3  the pool contract appears as a counterparty in a Transfer of our mint.
  V4     the pool id is topics[1], and its currencies come from the PoolManager's Initialize event
         (the same reader the V4 pool_meta backfill uses).

Two candidates that both hold the mint stay AMBIGUOUS and fail closed. That happened once in eight.
Inventing a tie-break there would be the guess this module exists to avoid.

This module is PURE — no network, no database. The caller injects `key_fn`. That is deliberate:
`resolve_pool_for_position` is on the funded exit path, and an RPC inside it would turn a transient
network failure into `None`, which the caller reads as "no usable pool" and converts into an exit
refusal indistinguishable from a genuine ambiguity. Resolve once, persist, then read.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memecoin")

# ERC-20 Transfer(address,address,uint256)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_V4_POOL_ID_LEN = 66          # 32 bytes; a V2/V3 pool is a 20-byte address (42 chars)


def _mint_transfer_parties(logs, mint):
    """Every counterparty of a Transfer of OUR token in this receipt."""
    out = set()
    for lg in logs or ():
        topics = (lg or {}).get("topics") or []
        if str(lg.get("address", "")).lower() != mint or not topics:
            continue
        if str(topics[0]).lower() != TRANSFER_TOPIC or len(topics) < 3:
            continue
        for raw in (topics[1], topics[2]):
            out.add("0x" + str(raw)[-40:].lower())
    return out


def select_traded_pool(logs, mint, *, pool_manager, ingest_topics, key_fn):
    """`(pool, log_index)` for the pool this entry actually traded, or None.

    `key_fn(pool_id)` returns the V4 PoolKey (a mapping with currency0/currency1) or None. A lookup
    that fails must NOT admit the pool — an RPC miss is not evidence that a pool holds our mint.
    """
    mint = str(mint or "").lower()
    if not mint or not logs:
        return None
    manager = str(pool_manager or "").lower()
    wanted = {str(t).lower() for t in (ingest_topics or ())}
    parties = _mint_transfer_parties(logs, mint)

    hits = []
    for index, lg in enumerate(logs):
        topics = (lg or {}).get("topics") or []
        if not topics or str(topics[0]).lower() not in wanted:
            continue
        address = str(lg.get("address", "")).lower()
        if address == manager and len(topics) > 1 and len(str(topics[1])) == _V4_POOL_ID_LEN:
            pool = str(topics[1]).lower()
            try:
                key = key_fn(pool)
            except Exception:
                logger.exception("fill pool: V4 key lookup raised for %s", pool)
                key = None
            if not key:
                continue                      # unknown currencies -> cannot claim it holds the mint
            currencies = {str(key.get("currency0", "")).lower(),
                          str(key.get("currency1", "")).lower()}
            if mint in currencies:
                hits.append((pool, index))
        elif address and address in parties:
            hits.append((address, index))     # V2/V3: this pool moved our token

    unique = {pool for pool, _ in hits}
    if len(unique) != 1:
        return None                           # 0 = not identifiable; >1 = ambiguous -> fail closed
    return hits[0]          # first occurrence: a pool can be swapped through twice
