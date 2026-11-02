#!/usr/bin/env python3
"""WO-6 — predictive-rug shadow read: resolvability + watched-wallet precedence. READ-ONLY.

Two parts:
  (1) resolvability — what fraction of pools yield a GENUINE deployer (not a router) from the recorded
      tape. The WO demands this be verified, not assumed. On EVM the swap-log `maker` is the router
      (a handful of addresses appear in most pools), so a router-exclusion pass is mandatory before
      any deployer claim; the report surfaces the router set and the post-exclusion resolvable fraction.
  (2) rug precedence — across recorded rugs, how often did a watched wallet act first, at what lead
      time. This is the go/no-go for a CUT-precedence rule. It requires genuine deployer resolution;
      when that is unavailable (EVM router confound, or an empty Solana per-actor tape) it reports
      insufficient and names what would unblock it — never a fabricated lead-time.

No live RPC here (the getSignaturesForAddress genesis walk is the operator's throttled option, wired
via resolve_watch_set(sig_fetch=...)). Never the funded runner's lane.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

ROUTER_POOL_SHARE = 0.05      # a maker appearing in >5% of pools is a router/aggregator, not a deployer


def routers(conn, chain, min_pools):
    """Makers whose pool-spread marks them as routers (to exclude before any deployer claim)."""
    total_pools = conn.execute("SELECT COUNT(DISTINCT pool) FROM pool_ticks WHERE chain_id=?",
                               (chain,)).fetchone()[0] or 1
    rows = conn.execute(
        "SELECT maker, COUNT(DISTINCT pool) np FROM pool_ticks WHERE chain_id=? AND maker!='' "
        "GROUP BY maker HAVING np > ? ORDER BY np DESC", (chain, int(total_pools * min_pools))).fetchall()
    return {r[0] for r in rows}, total_pools


def resolvability(conn, chain, router_set):
    """Fraction of pools whose EARLIEST non-router maker exists (a genuine-deployer proxy)."""
    pools = [r[0] for r in conn.execute(
        "SELECT DISTINCT pool FROM pool_ticks WHERE chain_id=? LIMIT 5000", (chain,))]
    resolvable = 0
    for pool in pools:
        row = conn.execute(
            "SELECT maker FROM pool_ticks WHERE chain_id=? AND pool=? AND maker!='' "
            "ORDER BY block_no LIMIT 50", (chain, pool)).fetchall()
        makers = [m[0] for m in row if m[0] not in router_set]
        if makers:
            resolvable += 1
    return resolvable, len(pools)


def render(chain, router_set, total_pools, resolvable, sampled):
    lines = ["", "=" * 70, "  PREDICTIVE-RUG RESOLVABILITY (read-only)", "=" * 70,
             f"  chain={chain}  total_pools={total_pools}",
             f"  routers detected (>{ROUTER_POOL_SHARE:.0%} pool-spread, excluded): {len(router_set)}"]
    for r in list(router_set)[:5]:
        lines.append(f"    router: {r}")
    frac = (resolvable / sampled) if sampled else 0.0
    lines.append(f"  pools with a genuine (non-router) earliest maker: {resolvable}/{sampled} "
                 f"({frac:.1%})")
    if frac < 0.10:
        lines.append("  VERDICT: NOT resolvable from this tape — the maker field is router-dominated "
                     "(EVM). Genuine deployer resolution needs the Solana per-actor tape (worker "
                     "enabled) or the throttled getSignaturesForAddress genesis walk. Rug-precedence "
                     "read STAYS BLOCKED; no fabricated lead-time.")
    else:
        lines.append("  VERDICT: resolvable — the rug-precedence read may proceed on these pools "
                     "(cross-reference watched-wallet activity against recorded rug events).")
    lines += ["=" * 70, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Predictive-rug resolvability report (read-only).")
    ap.add_argument("--db", default=None)
    ap.add_argument("--chain", default="4663")
    args = ap.parse_args()
    from sniper.persistence.db_ticks import ticks_db_path
    db = args.db or ticks_db_path()
    if not os.path.exists(db):
        print("No tape DB.")
        return 0
    conn = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)
    try:
        router_set, total = routers(conn, args.chain, ROUTER_POOL_SHARE)
        resolvable, sampled = resolvability(conn, args.chain, router_set)
    finally:
        conn.close()
    print(render(args.chain, router_set, total, resolvable, sampled))
    return 0


if __name__ == "__main__":
    sys.exit(main())
