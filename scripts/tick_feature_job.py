#!/usr/bin/env python3
"""Tick-tape feature job v0 — cron every 5 minutes. Research-grade only; no live consumer.

Per run: (1) resolve pool orientation (token0/token1, bounded eth_calls) for newly-seen pools;
(2) compute windowed microstructure features (OFI, arrival accel, wash-filtered) for active
pools into pool_features; (3) LIQUIDITY BRIDGE — write tick-derived V2 reserve USD into
memecoin_token_liquidity so the paper-slippage enrichment (memecoin_exits, 2026-07-22) stops
falling to the 0.5 blind cap on coins GT never priced. The bridge reuses the EXISTING
enrichment path; nothing on the money path changes.

Spec: docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md.
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(str(ROOT))

from sniper.analysis.tick_features import (  # noqa: E402
    compute_pool_windows, ensure_feature_tables, resolve_mint_pools, resolve_pool_meta,
    store_features, tick_liquidity_usd,
)
from sniper.execution.evm_wallet import CHAIN_QUOTE, _rpc, quote_usd_price  # noqa: E402
from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.persistence import db_ticks as t  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from sniper.util.clock import real_utc_now_iso  # noqa: E402

WINDOW_S = 300.0
ACTIVE_POOL_CAP = 200      # busiest pools per chain per run; log what is dropped (no silent caps)
META_BUDGET = 50
MINT_BUDGET = 10       # targeted entered-mint resolutions per run (factory-log lookups)
# Additional recognized quote tokens per chain, ORIENTATION ONLY (pool_meta.quote_side). The
# liquidity bridge prices reserves with the chain's native quote_usd_price — it must NOT be
# extended to these without per-quote pricing (bridge is gated off; guard stays).
# 4663: USDG (6-dec stable) — 3 cohort coins quoted against it, coverage audit 2026-07-26.
EXTRA_QUOTES = {"4663": {"0x5fc5360d0400a0fd4f2af552add042d716f1d168"}}


def _connect_main(path):
    """Autocommit prevents slow tick/RPC work from pinning a main-DB read snapshot."""
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _eth_call_fn(rpc_url):
    def call(pool, selector):
        try:
            resp = _rpc(rpc_url, "eth_call", [{"to": pool, "data": selector}, "latest"])
            result = resp.get("result") if isinstance(resp, dict) else None
            return result if isinstance(result, str) and len(result) >= 42 else None
        except Exception:
            return None
    return call


def main():
    now_iso = real_utc_now_iso()
    tc = t.connect_ticks()
    ensure_feature_tables(tc)
    mconn = _connect_main(ROOT / "sniper_tracker.db")

    for chain in ("4663", "8453"):
        rpc = cfg.evm_rpc_for_chain(chain)
        quote = CHAIN_QUOTE.get(chain) or {}
        if not rpc or not quote.get("token"):
            continue
        # The 2026-07-23 fix wrapped BOTH sides in datetime() because observed_at is ISO with
        # 'T'/+00:00 while datetime(now_iso, ...) yields SQLite's space-separated form, so a raw
        # string compare broke on 'T' > ' ' and admitted stale rows. Correct, but datetime() on
        # the COLUMN made the predicate non-sargable: idx_pool_ticks_chain_time went unused and
        # the query scanned every row of the chain — 30.2s on the live 15GB tape (2026-08-12).
        #
        # bound_iso keeps the same row set and seeks the index (0.006s). datetime(x) > datetime(t)
        # compares at SECOND granularity, so floor(x) > floor(t) <=> x >= floor(t) + 1s. Comparing
        # the bare column against that precomputed bound is row-for-row identical, verified on the
        # live tape over closed intervals (an open interval drifts only because the funded tape
        # appends while the slow form scans).
        cutoff = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                  - timedelta(seconds=int(WINDOW_S * 2)))
        bound_iso = (cutoff.replace(microsecond=0)
                     + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
        # INDEXED BY is a hard pin, on purpose. This SELECT reads only (pool, COUNT(*)), which
        # idx_pool_ticks_pool_kind_time covers, so once that index existed the planner preferred a
        # COVERING SCAN of every row of the chain over a seek on the time range — measured on the
        # live tape at 1,502ms against 29.9ms for the seek, and ANALYZE did not change the choice.
        # The pin fails loudly if the index is ever dropped, which is the behavior we want.
        active = [r["pool"] for r in tc.execute(
            "SELECT pool, COUNT(*) AS n FROM pool_ticks INDEXED BY idx_pool_ticks_chain_time "
            "WHERE chain_id=? AND kind IN ('v2_swap','v3_swap') "
            "AND observed_at >= ? "
            "GROUP BY pool ORDER BY n DESC", (chain, bound_iso)).fetchall()]
        dropped = max(0, len(active) - ACTIVE_POOL_CAP)
        active = active[:ACTIVE_POOL_CAP]
        if not active:
            print(f"chain={chain} skipped=no_recent_swaps", flush=True)
            continue
        quotes = {quote["token"].lower()} | EXTRA_QUOTES.get(chain, set())
        resolved = resolve_pool_meta(tc, chain, active, _eth_call_fn(rpc),
                                     quotes, now_iso, budget=META_BUDGET)
        # Targeted pass: mints we actually entered (last 7d) that the busy-pool resolver missed —
        # small/short-lived coins never crack the top-200 activity cut (coverage audit 2026-07-26).
        entered = [r["mint"].lower() for r in mconn.execute(
            "SELECT DISTINCT mint FROM memecoin_positions WHERE network_id=? AND mint IS NOT NULL "
            "AND datetime(opened_at) >= datetime(?, '-7 day')", (chain, now_iso))]
        def _raw_rpc(method, params, _url=rpc):
            return _rpc(_url, method, params)
        resolved_t = resolve_mint_pools(tc, chain, entered, _raw_rpc, _eth_call_fn(rpc),
                                        quotes, now_iso, budget=MINT_BUDGET)
        meta = {r["pool"]: r for r in tc.execute(
            "SELECT pool, token0, token1, quote_side FROM pool_meta WHERE chain_id=?", (chain,))}
        quote_usd = quote_usd_price(chain)
        feats = 0
        bridged = 0
        for pool in active:
            pm = meta.get(pool)
            if not pm or pm["quote_side"] is None:
                continue
            rows = compute_pool_windows(tc, chain, pool, window_s=WINDOW_S, now_iso=now_iso,
                                        quote_side=pm["quote_side"])
            feats += store_features(tc, rows)
            # liquidity bridge (V2 pools only). GATED OFF by default (2026-07-23 review P1):
            # set_token_liquidity feeds PAPER exit slippage — a decision-path write, not pure
            # observation — and it can bridge a non-canonical pool's reserve onto a coin. Keep the
            # tick plane observation-only until the bridge is validated (canonical pool, fresh data)
            # behind a pre-registered read.
            if cfg.MEMECOIN_TICK_LIQUIDITY_BRIDGE:
                usd = tick_liquidity_usd(tc, chain, pool, quote_side=pm["quote_side"],
                                         quote_decimals=int(quote.get("decimals") or 18),
                                         quote_usd=quote_usd)
                if usd:
                    coin = pm["token1"] if pm["quote_side"] == 0 else pm["token0"]
                    m.set_token_liquidity(mconn, chain, coin, usd, now_iso)
                    bridged += 1
        mconn.commit()
        print(f"[tick-features] chain={chain} active={len(active)} dropped={dropped} "
              f"meta+={resolved} mint_meta+={resolved_t} feature_rows+={feats} liquidity_bridged={bridged}")


if __name__ == "__main__":
    main()
