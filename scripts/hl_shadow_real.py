#!/usr/bin/env python3
"""Shadow-real deployable ledger — what the copy book is worth at REAL size, not idealized paper.

Two views:
  * capacity  — per open-book coin, the max notional the live book absorbs within a slippage
                budget. Exposes that the alt edge lives in books too thin to deploy.
  * deployable — for closed trades with CAPTURED depth, paper PnL minus real size-slippage minus
                funding at a chosen per-position bankroll.
Read-only.  python3 scripts/hl_shadow_real.py --size 10000 --budget-bps 25"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_scorecard as SC  # noqa: E402
from sniper.hyperlive import fills as F  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.markets import hyperliquid_api as HL  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Shadow-real deployable ledger for the HL copy book")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--size", type=float, default=10000.0, help="per-position bankroll for deployable")
    ap.add_argument("--budget-bps", type=float, default=25.0, help="round-trip slippage budget for capacity")
    ap.add_argument("--sizes", default="1000,5000,10000,25000,50000")
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)

    open_coins = sorted({str(r["coin"]).upper()
                         for r in conn.execute("SELECT coin FROM hl_copy_book WHERE status='open'")})
    print("═══ SHADOW-REAL — CAPACITY (live book, round-trip within "
          f"{args.budget_bps:.0f}bps) ═══")
    print(f"  {'coin':<8}{'spread_bps':>11}{'confirmed$':>17}{'status':>15}")
    caps = []
    for coin in open_coins:
        try:
            book = HL.fetch_l2_book(coin)
        except Exception:
            continue
        cap = SC.capacity_result(book, budget_bps=args.budget_bps)
        caps.append((coin, F.spread_bps(book), cap))
    for coin, sp, cap in sorted(
            caps, key=lambda x: -float(x[2].get("capacity_lower_bound_usd") or 0)):
        lower = float(cap.get("capacity_lower_bound_usd") or 0)
        print(f"  {coin:<8}{(sp if sp is not None else float('nan')):>11.1f}"
              f"{lower:>17,.0f}{cap['status']:>15}")
    if caps:
        confirmed_thin = [
            item for item in caps
            if item[2]["status"] == "measured"
            and float(item[2].get("capacity_upper_bound_usd") or 0) < args.size
        ]
        censored = [item for item in caps if item[2]["status"] == "lower_bound"]
        print(f"\n  confirmed below ${args.size:,.0f}: {len(confirmed_thin)}/{len(caps)}; "
              f"provider-depth lower bounds: {len(censored)}/{len(caps)}.")

    # deployable on captured closes
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM hl_copy_book WHERE status!='open' AND pnl_bps IS NOT NULL")]
    print("\n═══ DEPLOYABLE-AT-SIZE (closed trades with captured depth) ═══")
    any_depth = any(SC.load_depth(r, "entry") for r in closed)
    if not any_depth:
        print("  (no captured depth on closed trades yet — accrues after the runner restart)")
    else:
        for size in [float(s) for s in args.sizes.split(",")]:
            sr = SC.shadow_real(closed, size=size)
            d = sr["deployable"]
            print(f"  ${size:>8,.0f}  feasible={sr['feasible']}/{sr['considered']} "
                  f"({sr['feasible_pct']}%)  deployable_mean="
                  f"{d.get('mean') if d.get('n') else 'n/a'}bps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
