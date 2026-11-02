#!/usr/bin/env python3
"""Backfill Binance hourly klines for research.

Usage: python3 scripts/backfill_binance_klines.py [--months 6] [--assets BTC,ETH,SOL]

Fetches hourly OHLCV from Binance REST API. Paginates with startTime.
Idempotent (UNIQUE constraint on asset+timestamp_ms).
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
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000
REQUEST_DELAY_S = 0.2

ASSET_TO_SYMBOL = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            asset        TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            open         REAL NOT NULL,
            high         REAL NOT NULL,
            low          REAL NOT NULL,
            close        REAL NOT NULL,
            volume       REAL NOT NULL DEFAULT 0,
            venue        TEXT NOT NULL DEFAULT 'binance',
            UNIQUE(asset, timestamp_ms)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ph_asset_ts
        ON price_history(asset, timestamp_ms)
    """)
    conn.commit()


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list:
    """Fetch one page of hourly klines from Binance."""
    params = {
        "symbol": symbol,
        "interval": "1h",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_LIMIT,
    }
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def backfill_asset(conn: sqlite3.Connection, asset: str, months: int) -> int:
    """Backfill hourly klines for one asset. Returns rows inserted."""
    symbol = ASSET_TO_SYMBOL.get(asset)
    if not symbol:
        print(f"  Unknown asset: {asset}")
        return 0

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=months * 30)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    total_inserted = 0
    cursor_start = start_ms
    page = 0

    while cursor_start < end_ms:
        klines = fetch_klines(symbol, cursor_start, end_ms)
        page += 1

        if not klines:
            break

        inserted = 0
        max_ts = cursor_start
        for k in klines:
            # Binance kline format: [open_time, open, high, low, close, volume, close_time, ...]
            ts_ms = int(k[0])
            o = float(k[1])
            h = float(k[2])
            l = float(k[3])
            c = float(k[4])
            v = float(k[5])

            try:
                conn.execute(
                    "INSERT OR IGNORE INTO price_history (asset, timestamp_ms, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (asset, ts_ms, o, h, l, c, v),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass

            max_ts = max(max_ts, ts_ms)

        conn.commit()
        total_inserted += inserted

        dt = datetime.fromtimestamp(max_ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        print(f"  {asset} page {page}: {len(klines)} bars, {inserted} new, latest={dt}")

        # Move cursor past the latest bar
        cursor_start = max_ts + 3600 * 1000  # +1 hour
        time.sleep(REQUEST_DELAY_S)

    return total_inserted


def validate(conn: sqlite3.Connection, asset: str, months: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM price_history WHERE asset = ?",
        (asset,),
    ).fetchone()
    count = row[0]
    min_ts = row[1]
    max_ts = row[2]
    expected = months * 30 * 24
    coverage = count / expected * 100 if expected > 0 else 0

    return {
        "asset": asset,
        "rows": count,
        "expected": expected,
        "coverage_pct": round(coverage, 1),
        "date_range": f"{datetime.fromtimestamp(min_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(max_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}" if min_ts else "empty",
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill Binance hourly klines")
    parser.add_argument("--months", type=int, default=6, help="Months to backfill")
    parser.add_argument("--assets", type=str, default="BTC,ETH,SOL", help="Comma-separated assets")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Database path")
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",")]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    print(f"Backfilling {args.months} months of Binance hourly klines for {assets}")
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

    print("=== SUMMARY ===")
    all_ok = True
    for asset, v in results.items():
        status = "OK" if v["rows"] >= 4000 else "WARN"
        if status == "WARN":
            all_ok = False
        print(f"  {asset}: {v['rows']} rows, {v['coverage_pct']}% coverage [{status}]")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
