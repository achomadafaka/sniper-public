#!/usr/bin/env python3
"""Item 11b — advisory holder-concentration report (rug-risk signal, NOT a gate).

Scans a Solana mint (or every open real Solana position) via native RPC and prints top-holder
concentration + an advisory flag. ADVISORY ONLY — it never blocks anything; it's the data-gathering
step before deciding whether concentration is worth gating on.

CANARY: every run first checks the stable USDC supply response. A failed check means the scanner's
RPC/parse path is broken — exactly the failure the honeypot lesson warns about — so the report says
CANARY FAILED instead of pretending every token is fine.

    python3 scripts/research/memecoin_holder_scan.py --mint <MINT>
    python3 scripts/research/memecoin_holder_scan.py --open-real          # all open real Solana bags
    python3 scripts/research/memecoin_holder_scan.py --open-real --db sniper_tracker.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sniper.execution.solana_holder_scan import (  # noqa: E402
    concentration_flag,
    holder_concentration,
    mechanism_ok,
)


def _rpc_url():
    from sniper.settings import memecoin as cfg
    return getattr(cfg, "MEMECOIN_SOLANA_RPC_URL", None) or os.getenv("MEMECOIN_SOLANA_RPC_URL")


def _scan_line(label, mint, rpc_url):
    snap = holder_concentration(mint, rpc_url)
    flag = concentration_flag(snap)
    if snap is None:
        return flag, f"  {label:24} {mint[:12]}  UNKNOWN (unreadable)"
    return flag, (f"  {label:24} {mint[:12]}  top1={snap['top1_pct']:5.1f}%  "
                  f"top10={snap['top10_pct']:5.1f}%  n={snap['largest_n']:2}  [{flag.upper()}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint")
    ap.add_argument("--open-real", action="store_true")
    ap.add_argument("--db", default="sniper_tracker.db")
    args = ap.parse_args()
    rpc_url = _rpc_url()
    if not rpc_url:
        print("no Solana RPC configured (MEMECOIN_SOLANA_RPC_URL) — cannot scan", file=sys.stderr)
        sys.exit(2)

    # canary first — a broken scanner must not masquerade as 'everything is safe' (honeypot lesson).
    # Mechanism check on USDC supply (never overloads), NOT a mega-token account scan.
    if mechanism_ok(rpc_url):
        print("  CANARY (USDC supply)      OK — scanner RPC + parse path healthy\n")
    else:
        print("  CANARY FAILED — scanner RPC/parse unhealthy; treat ALL results below as UNKNOWN.\n")

    targets = []
    if args.mint:
        targets = [("mint", args.mint)]
    elif args.open_real:
        conn = sqlite3.connect(args.db, timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, mint FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' "
            "AND COALESCE(network_id,'solana')='solana' ORDER BY opened_at DESC").fetchall()
        targets = [((r["ticker"] or "?"), r["mint"]) for r in rows]
        if not targets:
            print("no open real Solana positions.")
            return
    else:
        ap.error("pass --mint <MINT> or --open-real")

    counts = {"ok": 0, "warn": 0, "high": 0, "unknown": 0}
    for label, mint in targets:
        flag, line = _scan_line(label, mint, rpc_url)
        counts[flag] += 1
        print(line)
    print(f"\n  summary: {counts}  (ADVISORY — nothing was blocked)")


if __name__ == "__main__":
    main()
