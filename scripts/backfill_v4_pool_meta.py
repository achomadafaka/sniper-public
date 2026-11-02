#!/usr/bin/env python3
"""Backfill V4 mint->pool mappings for chain 4663 pools that already initialized.

The forward resolver (memecoin_tick_stream) only fills from the moment it deployed. A V4 pool
never emits Initialize twice, so every pool born earlier stays blind for ever. Measured
2026-08-30 before this ran: the 4663 tape held 2,652 distinct V4 pools and pool_meta could name
53 of them (2.0%).

This is a TARGETED lookup, not a block sweep. The pool id is an indexed topic, so one
address-filtered eth_getLogs answers for one pool, and the cost is bounded by the size of our
own universe rather than by chain history.

RPC: publicnode (chain-owner official, keyless). NOT the custody endpoint — a backfill is
research and must not spend funded quota. Measured p50 189ms on this query shape.

The output is a COVERAGE REPORT. A pool the node could not answer for is UNKNOWN, never
"a pool with no trades".
"""
import os, sys, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files

# Anchored: from another CWD a relative path reads no .env and the run proceeds on defaults.
load_env_files(_ROOT)
import sniper.execution.evm_v4_quote as Q
from sniper.analysis.tick_features import SEL_DECIMALS
from sniper.execution.evm_wallet import CHAIN_QUOTE
from sniper.persistence import db_ticks as t
from sniper.services.memecoin_tick_stream import backfill_v4_pool_meta

CHAIN = "4663"
RPC = os.environ.get("MEMECOIN_V4_BACKFILL_RPC", "https://robinhood-rpc.publicnode.com")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0
# 0.7 pools/s with a 0.12s pace drove the unresolved rate to 30% (60 of 200). Probed calmly
# afterwards, 4 of 4 of those pools returned their Initialize log with no error. So the misses
# were OUR OWN rate limit on a free endpoint, not chain history we cannot reach — and publishing
# them as "unknown coverage" would have been a self-inflicted number presented as a fact.
PACE = float(os.environ.get("MEMECOIN_V4_BACKFILL_PACE_S", "0.6"))
# Orientation only, matching tick_feature_job.py: the chain quote plus USDG. The native sentinel
# is added by the backfill itself.
EXTRA_QUOTES = {"0x5fc5360d0400a0fd4f2af552add042d716f1d168"}


def key_fn(chain, pool_id, rpc):
    """Retry with escalating backoff. A miss on a shared free endpoint is far more often our own
    pacing than a pruning boundary, so a single-shot read UNDERSTATES coverage."""
    for attempt in range(4):
        Q._key_cache.pop((str(chain), pool_id.lower()), None)
        k = Q._pool_key(chain, pool_id, rpc)
        if k:
            return k
        time.sleep(0.8 * (attempt + 1))
    return None


def dec_fn(token):
    """ERC-20 decimals(), fail-closed. A defaulted scale is every price wrong by a power of ten."""
    try:
        r = Q._rpc(RPC, "eth_call", [{"to": token, "data": SEL_DECIMALS}, "latest"])
        v = int((r or {}).get("result") or "0x", 16)
    except Exception:
        return None
    return v if 0 <= v <= 255 else None   # decimals is a uint8; anything else is not a decimals word


def _counts(tc):
    row = tc.execute(
        "SELECT COUNT(*) tot, SUM(quote_side IS NOT NULL AND token0_decimals IS NOT NULL "
        "AND token1_decimals IS NOT NULL) usable FROM pool_meta "
        "WHERE chain_id = ? AND LENGTH(pool) = 66", (CHAIN,)).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def main():
    tc = t.connect_ticks()
    quotes = {(CHAIN_QUOTE.get(CHAIN) or {}).get("token", "").lower()} | EXTRA_QUOTES
    quotes.discard("")
    usable_pools = {r[0] for r in tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id = ? AND LENGTH(pool) = 66 "
        "AND quote_side IS NOT NULL AND token0_decimals IS NOT NULL "
        "AND token1_decimals IS NOT NULL", (CHAIN,))}
    # Most recently active first: if this is interrupted, the pools a witness is actually asked
    # about are the ones already done.
    pools = [r[0] for r in tc.execute(
        "SELECT pool FROM pool_ticks WHERE chain_id = ? AND LENGTH(pool) = 66 "
        "GROUP BY pool ORDER BY MAX(observed_at) DESC", (CHAIN,))]
    # A pool_meta row that exists but is UNENRICHED needs no tape membership to fix: enrichment
    # reads its stored currencies and adds quote_side plus decimals. Measured 2026-09-01 over the
    # funded positions opened since the lane connected, 85 of 329 unenriched V4 rows were absent
    # from pool_ticks, so a tape-only candidate set silently skipped a quarter of the work.
    seen_pools = set(pools)
    unenriched = [r[0] for r in tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id = ? AND LENGTH(pool) = 66 "
        "AND (quote_side IS NULL OR token0_decimals IS NULL OR token1_decimals IS NULL)",
        (CHAIN,))]
    # Enrichment FIRST: it needs no Initialize lookup, its decimals reads are cached across pools
    # that share a quote token, and it is the case the funded lane actually fails on — 30 of the
    # 61 positions opened since the lane connected have a pool_meta row that is merely unenriched.
    pools = unenriched + [p for p in pools if p not in set(unenriched)]
    todo = [p for p in pools if p not in usable_pools]
    if LIMIT:
        todo = todo[:LIMIT]
    tot0, use0 = _counts(tc)
    print(f"tape V4 pools={len(pools)}  pool_meta V4 rows={tot0} USABLE={use0}  todo={len(todo)}\n"
          f"quote tokens={sorted(quotes)}  rpc={RPC.split('/')[2]}", flush=True)
    t0 = time.time()
    agg = {"resolved": 0, "enriched": 0, "unresolved": 0, "unresolved_pools": []}
    for i, p in enumerate(todo, 1):
        r = backfill_v4_pool_meta(tc, CHAIN, [p], RPC, key_fn=key_fn, dec_fn=dec_fn,
                                  quote_tokens=quotes)
        for k in ("resolved", "enriched", "unresolved"):
            agg[k] += r[k]
        agg["unresolved_pools"] += r["unresolved_pools"]
        if i % 100 == 0 or i == len(todo):
            el = time.time() - t0
            _, use = _counts(tc)
            print(f"  {i}/{len(todo)} usable={use} resolved={agg['resolved']} "
                  f"enriched={agg['enriched']} unresolved={agg['unresolved']} "
                  f"{el:.0f}s ({i/max(el,1e-9):.1f}/s)", flush=True)
        # Pace the RPC, not the loop. An enrichment-only row makes no Initialize call and its
        # decimals reads hit the cache, so sleeping on it turned an 18-hour walk into the
        # bottleneck rather than the endpoint.
        if r["resolved"] or r["unresolved"]:
            time.sleep(PACE)
    tot, use = _counts(tc)
    print(f"\n=== COVERAGE (USABLE rows, not row count — row count is what hid this) ===\n"
          f"  V4 pools in our tape:  {len(pools)}\n"
          f"  pool_meta V4 rows:     {tot}\n"
          f"  USABLE by the resolver:{use}  ({100.0*use/max(len(pools),1):.1f}% of the tape)\n"
          f"  gained this run:       {use - use0}\n"
          f"  UNKNOWN (node could not answer after 4 tries): {agg['unresolved']}", flush=True)
    if agg["unresolved_pools"]:
        print("  unknown pools (treat as UNKNOWN, not as absent):")
        for p in agg["unresolved_pools"][:20]:
            print("   ", p)


if __name__ == "__main__":
    main()
