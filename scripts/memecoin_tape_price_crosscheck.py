#!/usr/bin/env python3
"""Cross-check tape-derived USD against DexScreener USD on 4663 (design §5).

Read-only. Prints n, median and p90 relative error, and the PRE-REGISTERED verdict (§5.1, committed
2026-07-24 before this file existed). The tape price may not drive any exit until this prints PASS.
"""
import os
import sqlite3
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.analysis.tape_price import tape_price_usd          # noqa: E402
from sniper.persistence import db_ticks as t                    # noqa: E402

# pre-registered §5.1 — do NOT edit after seeing a result
ALIGN_S = 120.0
TOL_MEDIAN = 0.05
TOL_P90 = 0.15
MIN_N = 30


def _ep(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _quote_usd():
    from sniper.execution.evm_wallet import quote_usd_price
    return quote_usd_price("4663")


def main():
    tc = t.connect_ticks()
    conn = sqlite3.connect("file:sniper_tracker.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # memecoin_position_marks is keyed (chain_id, mint, sweep_ts) — there is NO position_id column.
    # Join on (chain_id, mint). quality='live' excludes quarantined spike marks. NEVER read
    # executable_exit_value here: it is an ALIAS for mid (binding retraction, exit-brain spec §1),
    # not an executable price.
    # Marks are keyed by (chain_id, mint) — the pool is resolved from the MINT below, NOT from
    # quote_route_target (that is the router/settler and has zero ticks; spec §4.3b).
    marks = conn.execute(
        "SELECT LOWER(mk.mint) AS mint, mk.mid AS dex_usd, mk.sweep_ts, mk.source "
        "FROM memecoin_position_marks mk "
        "WHERE mk.chain_id='4663' AND mk.quality='live' AND mk.mid > 0").fetchall()

    cols = {r[1] for r in tc.execute("PRAGMA table_info(pool_meta)")}
    if not {"token0_decimals", "token1_decimals"} <= cols:
        print("tape cross-check: pool_meta has no decimals columns yet — run tick_feature_job "
              "(which calls ensure_feature_tables) against the ticks DB first. No verdict.")
        return
    if not tc.execute("SELECT COUNT(*) FROM pool_meta WHERE token0_decimals IS NOT NULL").fetchone()[0]:
        print("tape cross-check: no pool has resolved decimals yet — resolve_pool_meta must run "
              "(needs live eth_call: operator STOP condition). No verdict.")
        return

    quote_usd = _quote_usd()
    if not quote_usd:
        print("tape cross-check: quote USD unavailable for 4663 — cannot compare. No verdict.")
        return

    errs, by_source, pools_seen, skips = [], {}, set(), {}

    def _skip(why):
        skips[why] = skips.get(why, 0) + 1

    meta_cache = {}
    tick_cache = {}
    for mk in marks:
        mint = mk["mint"]
        if mint not in meta_cache:
            meta_cache[mint] = tc.execute(
                "SELECT pool, quote_side, token0_decimals, token1_decimals FROM pool_meta "
                "WHERE chain_id='4663' AND quote_side IS NOT NULL "
                "AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL "
                "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
                (mint, mint)).fetchall()
        metas = meta_cache[mint]
        if len(metas) != 1:
            _skip("mint not resolvable to exactly one usable pool")
            continue
        meta = metas[0]
        pool = str(meta["pool"]).lower()
        pools_seen.add(pool)
        qs = int(meta["quote_side"])
        qd = meta["token0_decimals"] if qs == 0 else meta["token1_decimals"]
        bd = meta["token1_decimals"] if qs == 0 else meta["token0_decimals"]
        if qd is None or bd is None:
            _skip("decimals unresolved")
            continue
        if pool not in tick_cache:
            rows = tc.execute(
                "SELECT kind, block_no, amount0, amount1, observed_at FROM pool_ticks "
                "WHERE chain_id='4663' AND pool=? AND kind IN ('v2_swap','v3_swap') "
                "AND block_no IS NOT NULL ORDER BY block_no DESC LIMIT 400", (pool,)).fetchall()
            # This harness validates the CONVERSION (atomic -> USD), not event-time windowing, so it
            # needs no block_times (which only the open-position worker backfills, and there are no
            # open positions). Group per block by block_no — the true block identity, exactly what
            # §3.3's per-block VWAP wants — and align to the mark on observed_at, which is wall-clock
            # within seconds for a live stream. Production freshness still uses real block_ts.
            timed = []
            for r in rows:
                obs = _ep(r["observed_at"])
                if obs is None:
                    continue
                timed.append({**dict(r), "block_ts": int(r["block_no"]), "obs": obs})
            tick_cache[pool] = timed
        timed = tick_cache[pool]
        if not timed:
            _skip("no timed ticks for pool")
            continue
        target = _ep(mk["sweep_ts"])
        if target is None:
            _skip("unparseable sweep_ts")
            continue
        aligned = [s for s in timed if abs(s["obs"] - target) <= ALIGN_S]
        if not aligned:
            _skip("no tick within alignment window")
            continue
        px = tape_price_usd(aligned, quote_side=qs, base_decimals=bd,
                            quote_decimals=qd, quote_usd=quote_usd)
        if not px:
            _skip("tape price None")
            continue
        rel = abs(px - float(mk["dex_usd"])) / float(mk["dex_usd"])
        errs.append(rel)
        by_source.setdefault(mk["source"], []).append(rel)

    print(f"tape cross-check — pools seen: {len(pools_seen)}, marks scanned: {len(marks)}")
    for why, n in sorted(skips.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {n:>7}  {why}")

    n = len(errs)
    if n < MIN_N:
        print(f"\nRESULT: {n}/{MIN_N} aligned pairs — no verdict (pre-registered §5.1).")
        return
    med = statistics.median(errs)
    p90 = sorted(errs)[int(0.9 * (n - 1))]
    ok = med <= TOL_MEDIAN and p90 <= TOL_P90
    print(f"\nn={n}  median_rel_err={med:.4f}  p90={p90:.4f}"
          f"  (tolerance: median<={TOL_MEDIAN} and p90<={TOL_P90})")
    for src, rs in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        print(f"  by source: {src:<16} n={len(rs):<6} median={statistics.median(rs):.4f}")
    print(f"VERDICT: {'PASS — tape may price momentum_a on 4663' if ok else 'FAIL — do NOT ship Task 5'}")


if __name__ == "__main__":
    main()
