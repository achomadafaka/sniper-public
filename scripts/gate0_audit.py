#!/usr/bin/env python3
"""
Gate 0 Audit — batch-check followed/high-score wallets for Polymarket lifetime P&L.

Survivorship bias fix: wallets that score well on local behavioral signals but are
lifetime losers on Polymarket should never be copied. This script audits the current
followed wallet set and user_analysis high-scorers against Polymarket lifetime P&L.

Usage:
  python3 scripts/gate0_audit.py                     # audit + report only
  python3 scripts/gate0_audit.py --mark-failed        # also set status=gate0_failed
  python3 scripts/gate0_audit.py --min-score 60       # check wallets with insider_score >= 60
  python3 scripts/gate0_audit.py --wallet 0xABC...    # check a single wallet
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))

sys.path.insert(0, str(ROOT / "scripts"))
from wallet_profiler import fetch_wallet_lifetime_stats, GATE0_LIFETIME_PNL_FLOOR


def _get_wallets_to_audit(
    conn: sqlite3.Connection,
    *,
    min_insider_score: float = 50.0,
    include_all_followed: bool = True,
) -> List[Dict[str, Any]]:
    """Collect wallets to audit from followed_wallets and high-score user_analysis."""
    wallets: Dict[str, Dict[str, Any]] = {}

    if include_all_followed:
        rows = conn.execute(
            """SELECT wallet, label, status FROM followed_wallets
               WHERE status IN ('active', 'paused')"""
        ).fetchall()
        for r in rows:
            w = str(r[0] or "").strip().lower()
            if w:
                wallets[w] = {"wallet": w, "label": r[1], "source": "followed_wallets", "status": r[2]}

    # High-score wallets from user_analysis
    rows = conn.execute(
        """SELECT wallet, insider_score, followability_cached, lb_username
           FROM user_analysis
           WHERE insider_score >= ? OR followability_cached >= 0.5
           ORDER BY insider_score DESC""",
        (min_insider_score,),
    ).fetchall()
    for r in rows:
        w = str(r[0] or "").strip().lower()
        if w and w not in wallets:
            wallets[w] = {
                "wallet": w,
                "label": r[3] or w[:12],
                "source": "user_analysis",
                "insider_score": r[1],
                "followability": r[2],
            }

    return list(wallets.values())


def audit_wallets(
    wallets: List[Dict[str, Any]],
    *,
    delay_seconds: float = 1.0,
) -> List[Dict[str, Any]]:
    """Run Gate 0 check on a list of wallet dicts. Returns results list."""
    results = []
    total = len(wallets)
    for i, entry in enumerate(wallets, 1):
        wallet = entry["wallet"]
        label = entry.get("label", wallet[:12])
        print(f"  [{i}/{total}] {wallet[:16]}... ({label})", end="", flush=True)

        stats = fetch_wallet_lifetime_stats(wallet)
        entry_result = dict(entry)
        entry_result.update(stats)

        if stats["disqualified"]:
            pnl_str = f"${stats['lifetime_pnl']:+,.0f}" if stats["lifetime_pnl"] is not None else "unknown"
            print(f"  FAIL  {stats['disqualify_reason']}")
        else:
            pnl_str = f"${stats['lifetime_pnl']:+,.0f}" if stats["lifetime_pnl"] is not None else "unknown"
            trade_str = f"{stats['trade_count']} trades" if stats["trade_count"] is not None else "? trades"
            print(f"  pass  lifetime={pnl_str} {trade_str} [{stats['source']}]")

        results.append(entry_result)
        if i < total:
            time.sleep(delay_seconds)

    return results


def apply_failures(conn: sqlite3.Connection, results: List[Dict[str, Any]]) -> int:
    """Set status='gate0_failed' for disqualified wallets in followed_wallets."""
    count = 0
    for r in results:
        if r.get("disqualified") and r.get("source") in ("followed_wallets",):
            conn.execute(
                """UPDATE followed_wallets
                   SET status = 'gate0_failed',
                       notes = COALESCE(notes || ' | ', '') || ?
                   WHERE wallet = ? AND status NOT IN ('removed', 'gate0_failed')""",
                (f"Gate0: {r.get('disqualify_reason', '?')}", r["wallet"]),
            )
            count += conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    return count


def print_summary(results: List[Dict[str, Any]]) -> None:
    passed = [r for r in results if not r.get("disqualified")]
    failed = [r for r in results if r.get("disqualified")]

    print(f"\n{'='*70}")
    print(f"GATE 0 AUDIT SUMMARY  (floor = ${GATE0_LIFETIME_PNL_FLOOR:,.0f})")
    print(f"{'='*70}")
    print(f"Total checked : {len(results)}")
    print(f"Passed        : {len(passed)}")
    print(f"FAILED        : {len(failed)}")

    if failed:
        print(f"\n--- DISQUALIFIED WALLETS ---")
        # Sort by lifetime PnL ascending (worst first)
        failed_sorted = sorted(
            failed,
            key=lambda x: (x.get("lifetime_pnl") is None, x.get("lifetime_pnl") or 0),
        )
        for r in failed_sorted:
            pnl_str = f"${r['lifetime_pnl']:+,.0f}" if r.get("lifetime_pnl") is not None else "unknown"
            label = r.get("label") or r.get("lb_username") or r["wallet"][:16]
            print(f"  {label:<30} {r['wallet'][:18]}  lifetime={pnl_str}")
            print(f"    reason: {r.get('disqualify_reason', '?')}")
            print(f"    source: {r.get('source')} | insider_score: {r.get('insider_score', '?')}")

    if passed:
        print(f"\n--- PASSING WALLETS ---")
        for r in sorted(passed, key=lambda x: x.get("lifetime_pnl") or 0, reverse=True):
            pnl_str = f"${r['lifetime_pnl']:+,.0f}" if r.get("lifetime_pnl") is not None else "unknown"
            trades_str = f"{r['trade_count']}t" if r.get("trade_count") is not None else "?t"
            label = r.get("label") or r.get("lb_username") or r["wallet"][:16]
            print(f"  {label:<30} {r['wallet'][:18]}  lifetime={pnl_str:<14} {trades_str}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit followed wallets for Polymarket lifetime P&L (Gate 0)")
    parser.add_argument("--wallet", help="Check a single wallet instead of full audit")
    parser.add_argument("--mark-failed", action="store_true",
                        help="Set status=gate0_failed for disqualified followed wallets")
    parser.add_argument("--min-score", type=float, default=50.0,
                        help="Minimum insider_score to include from user_analysis (default: 50)")
    parser.add_argument("--followed-only", action="store_true",
                        help="Only check followed_wallets, skip user_analysis high-scorers")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between API calls (default: 1.0)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Output full results as JSON")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if args.wallet:
        print(f"Checking single wallet: {args.wallet}")
        stats = fetch_wallet_lifetime_stats(args.wallet)
        if args.json_out:
            print(json.dumps(stats, indent=2))
        else:
            verdict = "DISQUALIFIED" if stats["disqualified"] else "PASS"
            pnl_str = f"${stats['lifetime_pnl']:+,.0f}" if stats["lifetime_pnl"] is not None else "unknown"
            print(f"\n[Gate 0] {verdict}")
            print(f"  Lifetime P&L : {pnl_str}")
            print(f"  Trade count  : {stats.get('trade_count', 'unknown')}")
            print(f"  Volume       : ${stats.get('volume') or 0:,.0f}" if stats.get("volume") else "  Volume       : unknown")
            print(f"  Exists       : {stats.get('exists')}")
            print(f"  Source       : {stats.get('source')}")
            if stats["disqualified"]:
                print(f"  Reason       : {stats.get('disqualify_reason')}")
        sys.exit(0 if not stats["disqualified"] else 1)

    # Full audit
    print(f"Gate 0 Audit — floor: ${GATE0_LIFETIME_PNL_FLOOR:,.0f}")
    print(f"DB: {DB_PATH}")
    print()

    wallets = _get_wallets_to_audit(
        conn,
        min_insider_score=args.min_score,
        include_all_followed=not args.followed_only or True,
    )

    print(f"Wallets to check: {len(wallets)}")
    print()

    results = audit_wallets(wallets, delay_seconds=args.delay)

    print_summary(results)

    if args.mark_failed:
        marked = apply_failures(conn, results)
        print(f"\nMarked {marked} wallets as gate0_failed in followed_wallets")

    if args.json_out:
        print("\n--- JSON OUTPUT ---")
        print(json.dumps(results, indent=2, default=str))

    conn.close()

    failed_count = sum(1 for r in results if r.get("disqualified"))
    sys.exit(0 if failed_count == 0 else 1)
