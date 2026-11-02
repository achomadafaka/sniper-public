"""WO-9 — EVM hot exit lane (Robinhood 4663 + Base 8453). Held funded pools only.

No EVM chain can be armed on the 30 s pull cadence — an on-chain rug resolves in seconds. This is the
block-cadence feeder for the EXISTING hot engine/supervisor (`exit_engine_hot` / `HotSupervisor`) —
it reimplements neither. A raw log -> the canonical `decode_evm_log` (reused, not reforked) -> one
supervisor row per (chain_id, pool), stamped with EXPLICIT sale-authority provenance.

Why EVM differs from Solana: every EVM row is natively transaction-grounded (tx hash + log index),
so WO-2's Solana vault-delta corroboration does not apply — a decoded EVM row may authorize a sale
directly. But ONLY via explicit provenance (`transaction_decoded` + tx hash + block hash + a true
block time), never inferred from "it's an EVM chain" — that inference is exactly the hole that would
route an EVM sale into the Solana corroboration path, or let a header-less/mempool row authorize on
receive time. Reorged (`removed`) logs invalidate. The pull materializer stays verifier + fail-closed
fallback inside the same 60 s decision-gap bound; receive time is telemetry only.
"""
from __future__ import annotations

import logging

from sniper.services.memecoin_tick_stream import (
    V2_BURN, V2_SWAP, V2_SYNC, V3_BURN, V3_COLLECT, V3_MINT, V3_SWAP, decode_evm_log)

logger = logging.getLogger("memecoin")

HOT_CHAINS = ("4663", "8453", "56")
_MAX_SPAN = 400          # match the proven bounded getLogs window used by the EVM tape ingest
# Sale-relevant topics for the held-pool scan (a superset the engine already understands).
HOT_TOPICS = [V3_SWAP, V2_SWAP, V2_SYNC, V2_BURN, V3_BURN, V3_MINT, V3_COLLECT]


def evm_hot_row(chain_id, log, observed_at, *, block_ts=None, block_time_true=False):
    """Decode one confirmed-block EVM log into a hot-engine row with explicit sale-authority
    provenance, or None for a reorged/removed log or a foreign topic.

    `block_ts` is the TRUE block time from the header; `block_time_true` says it is a real header time
    (not a receive-time estimate). A row becomes `transaction_decoded=True` — the only state from
    which a sale may authorize directly — ONLY with a real header time, a tx hash, and a block hash.
    Anything short of that stays ungrounded: it wakes + prewarms but can never authorize a sale.
    """
    if log.get("removed"):
        return None                        # reorg-invalidated -> never wakes, never authorizes
    row = decode_evm_log(chain_id, log, observed_at)
    if row is None:
        return None
    grounded = bool(block_time_true and block_ts is not None
                    and row.get("tx_sig") and row.get("block_hash")
                    and row.get("block_no") is not None and row.get("log_index") is not None)
    row.update(network_id=str(chain_id),
               block_ts=(block_ts if block_ts is not None else None),
               block_time_true=bool(block_time_true and block_ts is not None),
               transaction_decoded=grounded)
    return row


def feed_evm_block(supervisor, chain_id, logs, *, block_ts, block_time_true, observed_at):
    """One observed block: decode held-pool logs, load each pool atomically, then decide once/pool.

    Reorged logs are dropped. The caller scopes `logs` to held funded pools — this lane never widens
    into the observation tape.
    """
    decisions = []
    by_pool = {}
    for lg in logs:
        row = evm_hot_row(chain_id, lg, observed_at, block_ts=block_ts,
                          block_time_true=block_time_true)
        if row is None:
            continue
        by_pool.setdefault(row.get("pool"), []).append(row)
    for pool_rows in by_pool.values():
        pool_rows.sort(key=lambda row: (
            int(row.get("log_index") or 0), str(row.get("tx_sig") or "")))
        try:
            decisions.append(supervisor.on_evm_block(pool_rows))
        except Exception:
            logger.exception("evm hot lane: row feed failed chain=%s pool=%s",
                             chain_id, pool_rows[0].get("pool"))
    return decisions


def fetch_evm_hot_logs(chain_id, held_pools, rpc_call, *, cursor):
    """RPC-ONLY phase of one per-block poll: on a new block, ONE `eth_getLogs` scoped to the held
    funded pools (<=7 by the concurrency bound) for the sale-relevant topics, grouped by block with
    each block's true header time. Returns `(new_cursor, blocks)` where `blocks` is a list of
    `(block_ts, block_time_true, [logs])` in ascending block order; `(cursor, [])` unchanged on any
    RPC failure — the pull lane covers the gap. Never raises, NEVER touches the supervisor, so the
    shared hot lane can run it in a worker thread without blocking the enforced pull fallback.

    `rpc_call(method, params)` returns the JSON-RPC result or raises. Block time comes from the log's
    own `blockTimestamp` when the node provides it (free, like the tick-stream head path); a log
    without it stays ungrounded (transaction_decoded=False) rather than authorizing on receive time.
    """
    if str(chain_id) not in HOT_CHAINS:
        return cursor, []
    pools = sorted({str(p).lower() for p in (held_pools or ()) if p})
    if not pools:
        return cursor, []
    try:
        head = int(rpc_call("eth_blockNumber", []), 16)
    except Exception:
        logger.exception("evm hot lane %s: head read failed", chain_id)
        return cursor, []
    frm = (head if cursor is None else cursor + 1)
    if head < frm:
        return cursor, []
    to_block = min(head, frm + _MAX_SPAN - 1)
    try:
        logs = rpc_call("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to_block),
                                         "address": pools, "topics": [HOT_TOPICS]}])
        if not isinstance(logs, list):
            raise ValueError("unexpected getLogs result")
    except Exception:
        logger.exception("evm hot lane %s: getLogs failed", chain_id)
        return cursor, []
    # group by block so each block feeds with its own true header time (one decision cadence/block)
    by_block = {}
    for lg in logs:
        try:
            bn = int(lg["blockNumber"], 16)
        except (KeyError, TypeError, ValueError):
            continue
        by_block.setdefault(bn, []).append(lg)
    blocks = []
    for bn in sorted(by_block):
        block_logs = by_block[bn]
        ts_hex = next((lg.get("blockTimestamp") for lg in block_logs
                       if lg.get("blockTimestamp") is not None), None)
        try:
            block_ts = int(ts_hex, 16) if ts_hex is not None else None
        except (TypeError, ValueError):
            block_ts = None
        blocks.append((block_ts, block_ts is not None, block_logs))
    return to_block, blocks


def feed_evm_blocks(supervisor, chain_id, blocks, *, observed_at):
    """Feed the grouped blocks from `fetch_evm_hot_logs` to the supervisor. Runs on the OWNER thread
    (execution callbacks own their SQLite connection). Returns the flat list of decisions."""
    decisions = []
    for block_ts, block_time_true, block_logs in blocks:
        decisions.extend(feed_evm_block(supervisor, chain_id, block_logs, block_ts=block_ts,
                                        block_time_true=block_time_true, observed_at=observed_at))
    return decisions


def poll_evm_hot_once(supervisor, chain_id, held_pools, rpc_call, now_fn, *, cursor):
    """Sync convenience: fetch then feed in one call (fail-closed, never raises). The shared hot lane
    splits these two phases (RPC off the event loop, feed on the owner thread); this wrapper keeps the
    single-thread callers and tests on one entry point. Returns the new cursor."""
    new_cursor, blocks = fetch_evm_hot_logs(chain_id, held_pools, rpc_call, cursor=cursor)
    feed_evm_blocks(supervisor, chain_id, blocks, observed_at=now_fn())
    return new_cursor
