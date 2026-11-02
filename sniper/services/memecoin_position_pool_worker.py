"""Resolve each funded position's traded pool from its entry receipt, once, and persist it.

PLACEMENT (ruled 2026-09-02): post-entry, deferred to the END of the runner's loop iteration, plus
a reconciler for whatever the hook misses. Measured funded 4663 holds are p10 35s, median 190s,
p90 870s, so:

  - A pure cron loses the fastest positions entirely. A 30s reconciler is blind for 85% of a p10
    position's life, and short-lived positions are exactly where a missed liquidity exit hurts.
  - A SYNCHRONOUS post-entry hook is worse in the other direction: it would put an RPC between a
    signal arriving and an order going out, on a loop that runs exit sweeps at 1s, against an
    endpoint that 403s under burst.

Deferring to the end of the iteration keeps the network call off that path without needing a thread.
The reconciler exists because a fire-and-forget that fails silently is a gap nobody sees.

ONE function serves both call sites and only `limit` differs — two implementations of the same
sweep would drift apart.

It never raises into its caller. The hook runs inside the funded runner loop, and a resolver
failure must not take down the thing that sells.
"""
from __future__ import annotations

import logging

from sniper.services.memecoin_fill_pool import select_traded_pool
from sniper.services.memecoin_position_pool import (
    ensure_position_pool_table, record_position_pool)

logger = logging.getLogger("memecoin")


def pending_positions(mconn, tc, chain_id, *, limit):
    """Open funded positions on this chain with no stored pool yet, newest first.

    Newest first because a young position has the most life left to protect, and the store is
    immutable so an older unresolved one loses nothing by waiting a pass.
    """
    rows = mconn.execute(
        "SELECT p.id, LOWER(p.mint) AS mint, f.tx_sig FROM memecoin_positions p "
        "LEFT JOIN memecoin_fills f ON f.position_id = p.id AND f.kind = 'entry' "
        "WHERE p.is_open = 1 AND p.execution_mode = 'real' AND p.network_id = ? "
        "AND p.mint IS NOT NULL ORDER BY p.opened_at DESC", (str(chain_id),)).fetchall()
    out = []
    for r in rows:
        if tc.execute("SELECT 1 FROM evm_position_pools WHERE position_id=? AND chain_id=?",
                      (r["id"], str(chain_id))).fetchone():
            out.append((r["id"], r["mint"], r["tx_sig"], True))
        else:
            out.append((r["id"], r["mint"], r["tx_sig"], False))
    return out


def resolve_pending_position_pools(mconn, tc, chain_id, *, receipt_fn, key_fn, now_iso,
                                   limit=8, pool_manager=None, ingest_topics=None):
    """Resolve up to `limit` unresolved positions. Returns a report; never raises."""
    rep = {"considered": 0, "already": 0, "no_tx": 0, "resolved": 0, "unidentified": 0, "errors": 0}
    try:
        ensure_position_pool_table(tc)
        if pool_manager is None:
            from sniper.execution.evm_v4_quote import V4
            pool_manager = (V4.get(str(chain_id)) or {}).get("pool_manager") or ""
        if ingest_topics is None:
            from sniper.services.memecoin_tick_stream import INGEST_TOPICS
            ingest_topics = INGEST_TOPICS
        pending = pending_positions(mconn, tc, chain_id, limit=limit)
    except Exception:
        logger.exception("position pool worker: setup failed")
        rep["errors"] += 1
        return rep

    for position_id, mint, tx_sig, done in pending:
        if rep["resolved"] + rep["unidentified"] + rep["errors"] >= limit:
            break
        rep["considered"] += 1
        if done:
            rep["already"] += 1            # immutable, so never re-fetch a resolved position
            continue
        if not tx_sig:
            rep["no_tx"] += 1
            continue
        try:
            receipt = receipt_fn(tx_sig) or {}
            picked = select_traded_pool(receipt.get("logs") or [], mint,
                                        pool_manager=pool_manager,
                                        ingest_topics=ingest_topics, key_fn=key_fn)
        except Exception:
            # One position's failure must not stop the sweep, and must not reach the caller.
            logger.exception("position pool worker: receipt read failed for position %s", position_id)
            rep["errors"] += 1
            continue
        if not picked:
            rep["unidentified"] += 1       # ambiguous or no swap of our mint -> store nothing
            continue
        pool, log_index = picked
        if record_position_pool(tc, position_id, chain_id, mint, pool,
                                tx=tx_sig, log_index=log_index, now_iso=now_iso):
            rep["resolved"] += 1
        else:
            rep["errors"] += 1
    return rep
