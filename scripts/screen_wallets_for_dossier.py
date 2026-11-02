#!/usr/bin/env python3
"""Cheap pre-screen of the discovered-wallet backlog before full dossiers.

Reads user_analysis (local snapshot, no API calls), applies the Tier-1 screen
(sniper.analysis.wallet_protocol.dossier_prescreen), and prints the analyze /
skip partition. Feed the analyze list to scripts/run_wallet_protocol_batch.py
so the expensive full dossier only runs on wallets worth it.

Usage:
    python3 scripts/screen_wallets_for_dossier.py [--db PATH] [--lb-pnl-floor N]
        [--limit N] [--json OUT.json]

Read-only. Does not write the DB or mutate any verdict.

SCOPE / STALENESS — `user_analysis` is a frozen legacy table (AGENTS.md
§"Legacy / Historical Areas"); this read is an explicitly-scoped exception,
justified in dossier_prescreen.py's module docstring. The snapshot does not
refresh, so the partition is a compute-ordering HINT, not ground truth. The
full dossier re-fetches live data downstream.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sniper.analysis.wallet_protocol.dossier_prescreen import (  # noqa: E402
    DEFAULT_LB_PNL_FLOOR,
    partition_wallets,
)

_SCREEN_COLUMNS = (
    "wallet", "classification", "lb_pnl", "pnl",
    "wallet_quality_eligible", "wallet_quality_score", "wallet_quality_fail_reason",
    "win_rate", "updated_at",
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pre-screen wallets for full-dossier worthiness")
    p.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    p.add_argument("--lb-pnl-floor", type=float, default=DEFAULT_LB_PNL_FLOOR)
    p.add_argument("--limit", type=int, default=None, help="cap rows scanned (debug)")
    p.add_argument("--json", type=Path, default=None, help="write full partition to JSON")
    p.add_argument(
        "--analyze-list", type=Path, default=None,
        help="write the analyze set as one wallet per line (rank-sorted), "
             "directly consumable by run_wallet_protocol_batch.py --wallet-file",
    )
    args = p.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    q = f"SELECT {', '.join(_SCREEN_COLUMNS)} FROM user_analysis"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()

    part = partition_wallets(rows, lb_pnl_floor=args.lb_pnl_floor)
    n_analyze, n_skip = len(part["analyze"]), len(part["skip"])
    total = n_analyze + n_skip

    print("NOTE: source is the FROZEN legacy user_analysis snapshot — this is a "
          "compute-ordering hint, not ground truth. The full dossier re-fetches "
          "live data. See dossier_prescreen.py SCOPE/STALENESS note.")
    print(f"Scanned {total} wallets (lb_pnl_floor=${args.lb_pnl_floor:,.0f})")
    if total == 0:
        print("  No wallets to screen (empty user_analysis or --limit 0).")
        return 0
    print(f"  ANALYZE: {n_analyze} ({100*n_analyze/total:.1f}%)  "
          f"SKIP: {n_skip} ({100*n_skip/total:.1f}%)")
    print()

    # Skip-reason breakdown
    from collections import Counter
    reasons = Counter(r["_prescreen_reason"].split(":")[0] for r in part["skip"])
    print("Skip reasons:")
    for reason, n in reasons.most_common():
        print(f"  {reason:35s} {n}")
    print()

    print("Top 15 to analyze (rank-sorted):")
    print(f"  {'wallet':14s} {'rank':>14s}  reason")
    for r in part["analyze"][:15]:
        print(f"  {r['wallet'][:14]:14s} {r['_prescreen_rank']:>14,.1f}  {r['_prescreen_reason']}")

    if args.json:
        args.json.write_text(json.dumps(
            {"analyze": [r["wallet"] for r in part["analyze"]],
             "skip": [{"wallet": r["wallet"], "reason": r["_prescreen_reason"]} for r in part["skip"]],
             "lb_pnl_floor": args.lb_pnl_floor},
            indent=2))
        print(f"\nWrote partition → {args.json}")
    if args.analyze_list:
        args.analyze_list.write_text(
            "".join(f"{r['wallet']}\n" for r in part["analyze"])
        )
        print(f"Wrote {n_analyze} wallets (rank-sorted) → {args.analyze_list}  "
              f"(pipe: run_wallet_protocol_batch.py --wallet-file {args.analyze_list})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
