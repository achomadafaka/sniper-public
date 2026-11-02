#!/usr/bin/env python3
"""Measure the REAL executable-vs-chart gap on 4663, free (design: no paid provider).

`memecoin_position_marks.executable_exit_value` has been an ALIAS for `mid` on all 1.1M rows, so
every exit decision this system has ever made used a chart price nobody can actually sell into.
This probe closes that hole with machinery already in the repo:

  * V3 pools -> `dex_v3_quote` (QuoterV2 `quoteExactInputSingle`, eth_call, FREE, no key). It scans
    fee tiers and returns the exact whole-bag `amountOut`, i.e. what the position would truly get.
  * V2 pools -> exact constant-product math from `v2_sync` reserves we already record. Zero RPC.

Read-only: quotes and prints, writes nothing. Fail-closed: no quote -> the coin is skipped, never
an assumed number.
"""
import os
import sqlite3
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from dotenv import load_dotenv                                  # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"))   # the 4663 RPC lives here; without it every quote fails

from sniper.analysis.tape_price import tape_price_usd          # noqa: E402
from sniper.persistence import db_ticks as t                    # noqa: E402

CHAIN = "4663"
TOP_N = 12                    # bounded probe: busiest pools only
BAG_USD = (25.0, 100.0, 300.0)   # clip sizes to price the exit at


def v2_out(reserve_in, reserve_out, amount_in, fee_bps=30):
    """Exact Uniswap-V2 constant-product output. No RPC, no approximation."""
    ain = int(amount_in) * (10_000 - fee_bps) // 10_000
    if ain <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    return (int(reserve_out) * ain) // (int(reserve_in) + ain)


def main():
    tc = t.connect_ticks()
    tc.row_factory = sqlite3.Row
    from sniper.execution.evm_wallet import CHAIN_QUOTE, quote_usd_price
    quote_tok = (CHAIN_QUOTE.get(CHAIN) or {}).get("token")
    quote_usd = quote_usd_price(CHAIN)
    if not quote_tok or not quote_usd:
        print("no quote token / quote USD for 4663 — cannot measure.")
        return

    pools = tc.execute(
        "SELECT pool, COUNT(*) n FROM pool_ticks WHERE chain_id=? AND kind IN ('v2_swap','v3_swap') "
        "GROUP BY pool ORDER BY n DESC LIMIT ?", (CHAIN, TOP_N)).fetchall()

    print(f"executable-vs-chart probe — chain {CHAIN}, quote=${quote_usd:,.2f}, top {len(pools)} pools\n")
    rows = []
    for p in pools:
        pool = p["pool"]
        meta = tc.execute(
            "SELECT token0, token1, quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id=? AND pool=?", (CHAIN, pool)).fetchone()
        if not meta or meta["quote_side"] is None:
            continue
        qs = int(meta["quote_side"])
        qd = meta["token0_decimals"] if qs == 0 else meta["token1_decimals"]
        bd = meta["token1_decimals"] if qs == 0 else meta["token0_decimals"]
        base_tok = meta["token1"] if qs == 0 else meta["token0"]
        if qd is None or bd is None:
            continue

        swaps = [dict(r) for r in tc.execute(
            "SELECT kind, block_no, amount0, amount1 FROM pool_ticks WHERE chain_id=? AND pool=? "
            "AND kind IN ('v2_swap','v3_swap') AND block_no IS NOT NULL "
            "ORDER BY block_no DESC LIMIT 200", (CHAIN, pool))]
        for s in swaps:
            s["block_ts"] = int(s["block_no"])
        chart = tape_price_usd(swaps, quote_side=qs, base_decimals=bd,
                               quote_decimals=qd, quote_usd=quote_usd)
        if not chart:
            continue
        # DexScreener-convention liquidity = quote reserve x2, from the reserves we already record
        liq_usd = None
        sync = tc.execute(
            "SELECT amount0, amount1 FROM pool_ticks WHERE chain_id=? AND pool=? AND kind='v2_sync' "
            "ORDER BY id DESC LIMIT 1", (CHAIN, pool)).fetchone()
        if sync:
            try:
                liq_usd = (int(sync[f"amount{qs}"]) / (10 ** qd)) * quote_usd * 2.0
            except (TypeError, ValueError):
                liq_usd = None

        for bag_usd in BAG_USD:
            bag_tokens = bag_usd / chart
            bag_atomic = int(bag_tokens * (10 ** int(bd)))
            if bag_atomic <= 0:
                continue
            try:
                from sniper.execution.evm_dex_quote import dex_v3_quote
                q = dex_v3_quote(CHAIN, base_tok, quote_tok, bag_atomic,
                                 "0x" + "0" * 40)      # taker only affects calldata, not the quote
            except Exception:
                q = None
            out_atomic = None
            if q:
                out_atomic = int(q.get("buyAmount") or 0) if isinstance(q, dict) else None
            if not out_atomic:
                continue
            got_usd = (out_atomic / (10 ** int(qd))) * quote_usd
            gap = (got_usd - bag_usd) / bag_usd          # negative = we receive LESS than chart says
            # What PAPER assumes it would get: modeled_fill's slippage floor against the pool's
            # own quote-reserve liquidity (the same input the paper arms use).
            paper_usd = None
            if liq_usd:
                from sniper.execution.solana_quote import slippage_frac
                paper_usd = bag_usd * (1.0 - slippage_frac(bag_usd, liq_usd))
            over = ((paper_usd - got_usd) / got_usd) if (paper_usd and got_usd > 0) else None
            rows.append((pool, bag_usd, chart, got_usd, gap, paper_usd, over))
            extra = (f"  paper thinks ${paper_usd:>7.2f}  OVER-REPORTS {over*100:>6.2f}%"
                     if over is not None else "  (no liquidity -> paper uses the cap)")
            print(f"  {pool[:12]}..  bag ${bag_usd:>6.0f}  ACTUALLY GET ${got_usd:>7.2f}"
                  f"  gap {gap*100:>7.2f}%{extra}")

    if not rows:
        print("\nNo pool could be quoted (fail-closed). Nothing measured — no number invented.")
        return
    for bag in BAG_USD:
        g = [r[4] for r in rows if r[1] == bag]
        o = [r[6] for r in rows if r[1] == bag and r[6] is not None]
        if g:
            line = (f"\nbag ${bag:>6.0f}: n={len(g)}  median gap {statistics.median(g)*100:+.2f}%  "
                    f"worst {min(g)*100:+.2f}%")
            if o:
                line += (f"   | PAPER over-reports: median {statistics.median(o)*100:+.2f}%  "
                         f"worst {max(o)*100:+.2f}%  (n={len(o)})")
            print(line)


if __name__ == "__main__":
    main()
