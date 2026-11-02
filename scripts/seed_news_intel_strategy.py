#!/usr/bin/env python3
"""Seed the single paper news_intel_v1 strategy row. Idempotent.

Without this row execute_trade_idea() rejects every Execute click with
"No active news_intel_v1 strategy found" — the `strategies` table has 0 rows,
so the manual-entry path has been structurally dead (audit F-09).

Plan: docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md (Task 6)

Usage:
    python3 scripts/seed_news_intel_strategy.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

PARAMS = {
    "execution_mode": "paper",
    "bet_size_usdc": 10.0,
    "max_concurrent_positions": 10,
    # NOT "momentum": news_intel_v1 with direction_mode=momentum consistently loses
    # on geopolitical signals (brain: patterns/strategy-news-intel-geo-momentum-misalignment).
    # The idea's own direction carries the causal reasoning; momentum just chases price.
    "direction_mode": "idea",
}


def seed(conn: sqlite3.Connection):
    """Insert the strategy row. Returns the new id, or None if it already exists."""
    existing = conn.execute(
        "SELECT id FROM strategies WHERE strategy_type='news_intel_v1' AND status='active' LIMIT 1"
    ).fetchone()
    if existing is not None:
        return None
    # created_at/updated_at are supplied explicitly: the LIVE strategies table
    # declares them NOT NULL with no DEFAULT, unlike the CREATE TABLE the brain
    # index registry is generated from. Passing them works under either DDL.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO strategies (wallet, label, strategy_type, parameters, status,
                                   lifecycle_stage, created_at, updated_at)
           VALUES ('news_intel_manual', 'News Intel v1 (paper)', 'news_intel_v1', ?,
                   'active', 'paper', ?, ?)""",
        (json.dumps(PARAMS), now, now),
    )
    conn.commit()
    return cur.lastrowid


def main() -> int:
    # Anchored at the repo root: a relative path does not error from another CWD, it
    # CREATES an empty ledger and the run looks clean.
    _root = __import__("pathlib").Path(__file__).resolve().parents[1]
    conn = sqlite3.connect(str(_root / "sniper_tracker.db"), timeout=30)
    new_id = seed(conn)
    print(f"created strategy id={new_id}" if new_id else "already seeded — no change")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
