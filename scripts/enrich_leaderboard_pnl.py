#!/usr/bin/env python3
"""
Enrich user_analysis with real PnL from Polymarket leaderboard API.

Our internal PnL calculation (open cashPnl + closed realizedPnl) is
UNRELIABLE for high-volume/churning traders. The Polymarket leaderboard
API has the authoritative PnL.

This script:
1. Adds lb_pnl + lb_vol + lb_updated_at columns if missing
2. Queries the LB API for wallets with significant activity
3. Stores the real PnL alongside our computed PnL
4. Reports discrepancies

Rate limit: ~1 request per 200ms to be safe.

Usage:
    python3 scripts/enrich_leaderboard_pnl.py              # enrich top wallets
    python3 scripts/enrich_leaderboard_pnl.py --all         # enrich ALL wallets (slow)
    python3 scripts/enrich_leaderboard_pnl.py --dry-run     # just report, don't update
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / os.getenv("DB_PATH", "sniper_tracker.db")
LB_API = "https://data-api.polymarket.com/v1/leaderboard"
RATE_LIMIT_MS = 250  # ms between requests


def _ensure_columns(conn):
    """Add leaderboard columns if they don't exist."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(user_analysis)").fetchall()}
    for col, ctype, default in [
        ("lb_pnl", "REAL", "NULL"),
        ("lb_vol", "REAL", "NULL"),
        ("lb_rank", "TEXT", "NULL"),
        ("lb_username", "TEXT", "NULL"),
        ("lb_updated_at", "TEXT", "NULL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE user_analysis ADD COLUMN {col} {ctype} DEFAULT {default}")
            print(f"  Added column: {col}")
    conn.commit()


def _fetch_lb(wallet: str) -> dict | None:
    """Fetch leaderboard entry for a wallet. Returns None if not on LB."""
    url = f"{LB_API}?user={wallet}&timePeriod=ALL"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None


def main():
    dry_run = "--dry-run" in sys.argv
    enrich_all = "--all" in sys.argv

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    _ensure_columns(conn)

    # Select wallets to enrich
    if enrich_all:
        wallets = conn.execute(
            "SELECT wallet, pnl, insider_score FROM user_analysis ORDER BY ABS(pnl) DESC"
        ).fetchall()
    else:
        # Top wallets by absolute PnL + high insider score + wallets with strategies
        wallets = conn.execute("""
            SELECT DISTINCT wallet, pnl, insider_score FROM (
                SELECT wallet, pnl, insider_score FROM user_analysis 
                WHERE ABS(pnl) > 10000 OR insider_score >= 50
                UNION
                SELECT ua.wallet, ua.pnl, ua.insider_score 
                FROM strategies s JOIN user_analysis ua ON LOWER(s.wallet) = LOWER(ua.wallet)
                UNION
                SELECT ua.wallet, ua.pnl, ua.insider_score
                FROM watchlist_wallets ww JOIN user_analysis ua ON LOWER(ww.wallet) = LOWER(ua.wallet)
            )
            ORDER BY ABS(pnl) DESC
        """).fetchall()

    print(f"Enriching {len(wallets)} wallets (dry_run={dry_run})")
    
    enriched = 0
    not_on_lb = 0
    big_discrepancies = []
    
    for i, w in enumerate(wallets):
        wallet = w["wallet"]
        our_pnl = w["pnl"] or 0
        
        lb = _fetch_lb(wallet)
        time.sleep(RATE_LIMIT_MS / 1000)
        
        if lb is None:
            not_on_lb += 1
            continue
        
        lb_pnl = float(lb.get("pnl", 0))
        lb_vol = float(lb.get("vol", 0))
        lb_rank = str(lb.get("rank", ""))
        lb_username = str(lb.get("userName", ""))
        
        # Check discrepancy
        if our_pnl != 0:
            pct_diff = abs(lb_pnl - our_pnl) / max(abs(our_pnl), 1) * 100
        else:
            pct_diff = 0 if lb_pnl == 0 else 100
        
        if pct_diff > 50 and abs(lb_pnl - our_pnl) > 10000:
            big_discrepancies.append({
                "wallet": wallet[:16],
                "our_pnl": our_pnl,
                "lb_pnl": lb_pnl,
                "diff_pct": pct_diff,
                "username": lb_username,
            })
        
        if not dry_run:
            conn.execute("""
                UPDATE user_analysis 
                SET lb_pnl = ?, lb_vol = ?, lb_rank = ?, lb_username = ?, 
                    lb_updated_at = datetime('now')
                WHERE LOWER(wallet) = LOWER(?)
            """, (lb_pnl, lb_vol, lb_rank, lb_username, wallet))
        
        enriched += 1
        
        if (i + 1) % 50 == 0:
            if not dry_run:
                conn.commit()
            print(f"  Progress: {i+1}/{len(wallets)} ({enriched} enriched, {not_on_lb} not on LB)")
    
    if not dry_run:
        conn.commit()
    
    print(f"\nDone: {enriched} enriched, {not_on_lb} not on leaderboard")
    
    if big_discrepancies:
        print(f"\n⚠️ {len(big_discrepancies)} wallets with >50% PnL discrepancy:")
        for d in sorted(big_discrepancies, key=lambda x: -abs(x["diff_pct"]))[:20]:
            print(f"  {d['wallet']}... ({d['username']}) Our=${d['our_pnl']:>12,.0f} LB=${d['lb_pnl']:>12,.0f} diff={d['diff_pct']:.0f}%")
    
    conn.close()


if __name__ == "__main__":
    main()
