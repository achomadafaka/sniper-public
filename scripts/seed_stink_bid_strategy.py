#!/usr/bin/env python3
"""Seed the stink_bid_v1 strategy row.  Idempotent — skips if one already exists.

Usage:
    python3 scripts/seed_stink_bid_strategy.py [db_path]

Prints the strategy ID on success (needed for --strategy-id in the service file).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone


DEFAULT_PARAMS = {
    "dip_pct": 0.30,
    "min_favorite": 0.55,
    "min_bid": 0.05,
    "min_fill_size": 50.0,
    "max_resting": 3,
    "max_daily_fills": 5,
    "reprice_threshold": 0.02,
    "min_volume_24h": 10000.0,
    "position_size_usdc": 25.0,
    "espn_pause_after": 3,
    "espn_cancel_after_min": 10,
}


def seed(db_path: str) -> int:
    # timeout=30 matches the rest of the codebase — the VPS runs analyzer/paper/
    # stream/web concurrently and a default 5s timeout easily loses the race.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    existing = conn.execute(
        "SELECT id FROM strategies WHERE strategy_type = 'stink_bid_v1' LIMIT 1"
    ).fetchone()
    if existing:
        sid = int(existing["id"])
        print(f"stink_bid_v1 strategy already exists: id={sid}")
        conn.close()
        return sid

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO strategies
           (wallet, label, strategy_type, parameters, status, lifecycle_stage,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "0x0000000000000000000000000000000000000001",
            "Stink Bid Tennis v1",
            "stink_bid_v1",
            json.dumps(DEFAULT_PARAMS),
            "active",
            "paper",
            now,
            now,
        ),
    )
    conn.commit()
    sid = int(cur.lastrowid or 0)
    conn.close()
    print(f"Created stink_bid_v1 strategy: id={sid}")
    return sid


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    seed(db)
