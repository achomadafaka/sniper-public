#!/usr/bin/env python3
"""Backfill Hyperliquid funding rate history for research.

Usage: python3 scripts/backfill_hl_funding.py [--months 6] [--assets BTC,ETH,SOL]

Fetches hourly funding rates from HL's fundingHistory API.
Paginates backward in 500-hour chunks. Idempotent (skips existing timestamps).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
MAX_HOURS_PER_REQUEST = 500
REQUEST_DELAY_S = 0.3  # polite rate limiting


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_funding_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            asset         TEXT NOT NULL,
            timestamp_ms  INTEGER NOT NULL,
            funding_rate  REAL NOT NULL,
            premium       REAL NOT NULL DEFAULT 0,
            backfilled_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(asset, timestamp_ms)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hlfh_asset_ts
        ON hl_funding_history(asset, timestamp_ms)
    """)
    conn.commit()


def fetch_funding_chunk(coin: str, start_ms: int, end_ms: int) -> list:
    """Fetch one chunk of funding history from HL API."""
    payload = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_ms,
        "endTime": end_ms,
    }
    resp = requests.post(HL_INFO_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return data


def backfill_asset(conn: sqlite3.Connection, asset: str, months: int) -> int:
    """Backfill funding history for one asset. Returns rows inserted."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=months * 30)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # Find latest existing timestamp to avoid re-fetching
    row = conn.execute(
        "SELECT MAX(timestamp_ms) FROM hl_funding_history WHERE asset = ?",
        (asset,),
    ).fetchone()
    existing_max_ms = row[0] if row and row[0] else 0

    total_inserted = 0
    cursor_end = end_ms
    chunk_num = 0

    while cursor_end > start_ms:
        chunk_start = max(start_ms, cursor_end - MAX_HOURS_PER_REQUEST * 3600 * 1000)

        records = fetch_funding_chunk(asset, chunk_start, cursor_end)
        chunk_num += 1

        if not records:
            print(f"  {asset} chunk {chunk_num}: no data for {chunk_start}-{cursor_end}, moving back")
            cursor_end = chunk_start
            time.sleep(REQUEST_DELAY_S)
            continue

        inserted = 0
        for rec in records:
            ts_ms = rec.get("time", 0)
            fr = float(rec.get("fundingRate", 0))
            premium = float(rec.get("premium", 0))

            if ts_ms <= existing_max_ms and existing_max_ms > 0:
                continue

            try:
                conn.execute(
                    "INSERT OR IGNORE INTO hl_funding_history (asset, timestamp_ms, funding_rate, premium) VALUES (?, ?, ?, ?)",
                    (asset, ts_ms, fr, premium),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        total_inserted += inserted

        # Find the earliest timestamp in this chunk to continue backward
        earliest = min(r.get("time", cursor_end) for r in records)
        print(f"  {asset} chunk {chunk_num}: {len(records)} records, {inserted} new, earliest={datetime.fromtimestamp(earliest/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

        cursor_end = earliest - 1  # move window back
        time.sleep(REQUEST_DELAY_S)

    return total_inserted


def validate(conn: sqlite3.Connection, asset: str, months: int) -> dict:
    """Validate backfilled data for one asset."""
    row = conn.execute(
        "SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM hl_funding_history WHERE asset = ?",
        (asset,),
    ).fetchone()
    count = row[0]
    min_ts = row[1]
    max_ts = row[2]

    expected_hours = months * 30 * 24
    coverage = count / expected_hours * 100 if expected_hours > 0 else 0

    # Check monotonicity
    non_mono = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT timestamp_ms, LAG(timestamp_ms) OVER (ORDER BY timestamp_ms) as prev_ts
            FROM hl_funding_history WHERE asset = ?
        ) WHERE prev_ts IS NOT NULL AND timestamp_ms <= prev_ts
    """, (asset,)).fetchone()[0]

    result = {
        "asset": asset,
        "rows": count,
        "expected": expected_hours,
        "coverage_pct": round(coverage, 1),
        "date_range": f"{datetime.fromtimestamp(min_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(max_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}" if min_ts else "empty",
        "monotonic": non_mono == 0,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="Backfill HL funding history")
    parser.add_argument("--months", type=int, default=6, help="Months to backfill")
    parser.add_argument("--assets", type=str, default="BTC,ETH,SOL", help="Comma-separated assets")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Database path")
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",")]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    print(f"Backfilling {args.months} months of funding history for {assets}")
    print(f"DB: {args.db}")
    print()

    results = {}
    for asset in assets:
        print(f"--- {asset} ---")
        inserted = backfill_asset(conn, asset, args.months)
        print(f"  Total inserted: {inserted}")
        results[asset] = validate(conn, asset, args.months)
        print(f"  Validation: {json.dumps(results[asset])}")
        print()

    conn.close()

    # Summary
    print("=== SUMMARY ===")
    all_ok = True
    for asset, v in results.items():
        status = "OK" if v["rows"] >= 4000 and v["monotonic"] else "WARN"
        if status == "WARN":
            all_ok = False
        print(f"  {asset}: {v['rows']} rows, {v['coverage_pct']}% coverage, monotonic={v['monotonic']} [{status}]")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
