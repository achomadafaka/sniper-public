#!/usr/bin/env python3
"""Phase 1A shadow simulator — OFFLINE replay over the Phase 0 capture DB.

Reads ONLY captured local data (no live APIs, no orders, no keys, no production DB). Materializes
decision-time feature snapshots, runs a (diagnostic) probability model, the FOK-backed shadow decision
engine, and an evaluation report. In Phase 1A every decision is shadow_prediction / blocked_decision —
no paper entries (the boundary rule is unlocked and the baselines are uncalibrated).

  python3 scripts/run_polymarket_crypto5m_shadow_sim.py --db-path data/crypto5m_capture.sqlite \
      --mode replay --model market_midpoint_baseline
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import persistence as P          # noqa: E402
from sniper.algos.polymarket_crypto5m import probability_model as PM    # noqa: E402
from sniper.algos.polymarket_crypto5m import replay as R               # noqa: E402

DEFAULT_DB = "data/crypto5m_capture.sqlite"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto-5m Phase 1A shadow simulator (offline replay).")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--mode", default="replay", choices=["replay"])
    ap.add_argument("--model", default="market_midpoint_baseline", choices=sorted(PM.BASELINES))
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--stake-usdc", type=float, default=10.0)
    ap.add_argument("--min-edge", type=float, default=0.0)
    ap.add_argument("--market-limit", type=int, default=None)
    ap.add_argument("--window-start", type=int, default=None)
    ap.add_argument("--window-end", type=int, default=None)
    ap.add_argument("--checkpoint-policy", default="all", choices=["all"])  # only "all" implemented
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db_path):
        print(f"capture DB not found: {args.db_path} (run the collector first)")
        return 0
    model = PM.BASELINES[args.model]
    if args.dry_run:
        # truly write nothing — open read-only and only count windows (no schema DDL)
        import sqlite3
        ro = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
        n = ro.execute("SELECT COUNT(*) FROM crypto5m_markets").fetchone()[0]
        ro.close()
        print(f"[dry-run] {n} markets in {args.db_path}; model={args.model}; no writes")
        return 0
    conn = P.connect(args.db_path)
    summary = R.replay(conn, model=model, latency_ms=args.latency_ms, stake_usdc=args.stake_usdc,
                       min_edge=args.min_edge, market_limit=args.market_limit,
                       window_start=args.window_start, window_end=args.window_end, now_ts=0)
    print(R.format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
