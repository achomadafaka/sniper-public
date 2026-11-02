#!/usr/bin/env python3
"""Backfill open interest history for research.

Usage: python3 scripts/backfill_oi_history.py [--months 6] [--assets BTC,ETH,SOL]

Source priority:
  1. Coinalyze API (free, requires COINALYZE_API_KEY env var)
  2. Existing hyperliquid_snapshots (filtered to open_interest > 0, gap-audited)

For Coinalyze: register at https://coinalyze.net/account/api-key/

Important data-contract rules:
  - Always request Coinalyze OI with convert_to_usd=true.
  - Persist the OI unit and source provenance explicitly.
  - Never treat mixed-source OI rows as an undifferentiated series downstream.
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
COINALYZE_BASE = "https://api.coinalyze.net/v1"
REQUEST_DELAY_S = 1.5  # Coinalyze: 40 req/min

# Coinalyze symbol mapping for Hyperliquid exchange (exchange code = 6)
COINALYZE_SYMBOLS = {
    "BTC": "BTCUSDT.6",
    "ETH": "ETHUSDT.6",
    "SOL": "SOLUSDT.6",
}


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_oi_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            asset         TEXT NOT NULL,
            timestamp_ms  INTEGER NOT NULL,
            open_interest REAL NOT NULL,
            source        TEXT NOT NULL DEFAULT 'coinalyze',
            oi_units      TEXT NOT NULL DEFAULT 'unknown',
            source_detail TEXT NOT NULL DEFAULT '',
            UNIQUE(asset, timestamp_ms)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hloi_asset_ts
        ON hl_oi_history(asset, timestamp_ms)
    """)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(hl_oi_history)")
    }
    if "oi_units" not in columns:
        conn.execute(
            "ALTER TABLE hl_oi_history ADD COLUMN oi_units TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "source_detail" not in columns:
        conn.execute(
            "ALTER TABLE hl_oi_history ADD COLUMN source_detail TEXT NOT NULL DEFAULT ''"
        )

    # Existing Coinalyze rows predate convert_to_usd=true, so mark them honestly.
    conn.execute("""
        UPDATE hl_oi_history
           SET oi_units = CASE
                WHEN source = 'snapshots_fallback' THEN 'usd_notional'
                WHEN source = 'coinalyze' THEN 'asset_units'
                ELSE COALESCE(NULLIF(oi_units, ''), 'unknown')
           END
         WHERE oi_units IS NULL OR oi_units = '' OR oi_units = 'unknown'
    """)
    conn.execute("""
        UPDATE hl_oi_history
           SET source_detail = CASE
                WHEN source = 'coinalyze' THEN 'coinalyze_open_interest_history'
                WHEN source = 'snapshots_fallback' THEN 'hyperliquid_snapshots_last_daily_reading'
                ELSE source
           END
         WHERE source_detail IS NULL OR source_detail = ''
    """)
    conn.commit()


def try_coinalyze(conn: sqlite3.Connection, asset: str, months: int, api_key: str) -> int:
    """Try Coinalyze API for OI history. Returns rows inserted or -1 on failure."""
    symbol = COINALYZE_SYMBOLS.get(asset)
    if not symbol:
        return -1

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=months * 30)

    headers = {"api_key": api_key}
    params = {
        "symbols": symbol,
        "interval": "daily",
        "from": int(start.timestamp()),
        "to": int(now.timestamp()),
        "convert_to_usd": "true",
    }

    try:
        resp = requests.get(
            f"{COINALYZE_BASE}/open-interest-history",
            params=params,
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 401:
            print(f"  Coinalyze: 401 unauthorized — API key may be invalid")
            return -1
        if resp.status_code == 403:
            print(f"  Coinalyze: 403 forbidden — may need paid tier")
            return -1
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Coinalyze fetch failed: {e}")
        return -1

    if not data or not isinstance(data, list) or len(data) == 0:
        print(f"  Coinalyze: empty response")
        return -1

    # Coinalyze returns [{symbol, history: [{t, o, h, l, c}]}]
    series = data[0] if isinstance(data[0], dict) else {}
    history = series.get("history", [])
    if not history:
        print(f"  Coinalyze: no history in response")
        return -1

    inserted = 0
    for point in history:
        ts = int(point.get("t", 0)) * 1000  # Coinalyze uses seconds, we store ms
        oi = float(point.get("c", 0))  # close value for OI
        if ts > 0 and oi > 0:
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO hl_oi_history
                    (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
                VALUES (?, ?, ?, 'coinalyze', 'usd_notional', 'coinalyze_open_interest_history')
                ON CONFLICT(asset, timestamp_ms) DO UPDATE SET
                    open_interest = excluded.open_interest,
                    source = excluded.source,
                    oi_units = excluded.oi_units,
                    source_detail = excluded.source_detail
                WHERE hl_oi_history.source = 'coinalyze'
                   OR hl_oi_history.oi_units != 'usd_notional'
                   OR hl_oi_history.source_detail != 'coinalyze_open_interest_history'
                """,
                (asset, ts, oi),
            )
            if conn.total_changes > before:
                inserted += 1
    conn.commit()
    return inserted


def fallback_snapshots(conn: sqlite3.Connection, asset: str, months: int) -> int:
    """Fallback: extract OI from existing hyperliquid_snapshots."""
    if not table_exists(conn, "hyperliquid_snapshots"):
        print("  Snapshot fallback unavailable: hyperliquid_snapshots table does not exist")
        return -1

    latest_row = conn.execute(
        """
        SELECT MAX(captured_at)
        FROM hyperliquid_snapshots
        WHERE asset = ? AND open_interest > 0
        """,
        (asset,),
    ).fetchone()
    latest_captured_at = latest_row[0] if latest_row else None
    if not latest_captured_at:
        return 0

    latest_dt = datetime.fromisoformat(str(latest_captured_at).replace("Z", "+00:00"))
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    start = latest_dt - timedelta(days=months * 30)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute("""
        SELECT
            CAST(strftime('%s', captured_at) AS INTEGER) * 1000 as ts_ms,
            open_interest
        FROM hyperliquid_snapshots
        WHERE asset = ? AND open_interest > 0 AND captured_at >= ?
        ORDER BY captured_at
    """, (asset, start_str)).fetchall()

    if not rows:
        return 0

    # Aggregate to daily (take last reading per day)
    daily = {}
    for r in rows:
        day_ms = (r[0] // 86400000) * 86400000  # floor to day boundary
        daily[day_ms] = r[1]  # last value wins

    inserted = 0
    for ts_ms, oi in daily.items():
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, 'snapshots_fallback', 'usd_notional', 'hyperliquid_snapshots_last_daily_reading')
            """,
            (asset, ts_ms, oi),
        )
        if conn.total_changes > before:
            inserted += 1
    conn.commit()

    # Gap audit
    total_days = months * 30
    coverage = len(daily) / total_days * 100
    print(f"  Snapshot fallback: {len(daily)} daily readings from {len(rows)} raw snapshots ({coverage:.0f}% of {total_days}d)")
    if coverage < 50:
        print(f"  WARNING: <50% coverage — snapshot fallback is unreliable for this asset")

    return inserted


def validate(conn: sqlite3.Connection, asset: str, months: int) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms), source, oi_units
        FROM hl_oi_history
        WHERE asset = ?
        GROUP BY source, oi_units
        ORDER BY COUNT(*) DESC
        LIMIT 1
        """,
        (asset,),
    ).fetchone()
    if not row:
        return {"asset": asset, "rows": 0, "source": "none", "coverage_pct": 0}

    count = row[0]
    min_ts = row[1]
    max_ts = row[2]
    source = row[3]
    oi_units = row[4]
    expected = months * 30
    coverage = count / expected * 100
    mixed_units = conn.execute(
        "SELECT COUNT(DISTINCT oi_units) FROM hl_oi_history WHERE asset = ?",
        (asset,),
    ).fetchone()[0]

    return {
        "asset": asset,
        "rows": count,
        "expected": expected,
        "coverage_pct": round(coverage, 1),
        "source": source,
        "oi_units": oi_units,
        "mixed_units": mixed_units > 1,
        "date_range": f"{datetime.fromtimestamp(min_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(max_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}" if min_ts else "empty",
    }


def main():
    parser = argparse.ArgumentParser(description="Backfill OI history")
    parser.add_argument("--months", type=int, default=6, help="Months to backfill")
    parser.add_argument("--assets", type=str, default="BTC,ETH,SOL", help="Comma-separated assets")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Database path")
    parser.add_argument(
        "--require-coinalyze",
        action="store_true",
        help="Fail instead of falling back to snapshots when Coinalyze is unavailable",
    )
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",")]
    api_key = os.getenv("COINALYZE_API_KEY", "")

    if args.require_coinalyze and not api_key:
        print("ERROR: --require-coinalyze was set but COINALYZE_API_KEY is missing")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    print(f"Backfilling {args.months} months of OI history for {assets}")
    print(f"Coinalyze API key: {'set' if api_key else 'NOT SET — will use snapshot fallback'}")
    print(f"DB: {args.db}")
    print()

    results = {}
    for asset in assets:
        print(f"--- {asset} ---")
        inserted = -1

        # Try Coinalyze first
        if api_key:
            inserted = try_coinalyze(conn, asset, args.months, api_key)
            if inserted >= 0:
                print(f"  Coinalyze: {inserted} rows inserted")
            else:
                print(f"  Coinalyze failed, falling back to snapshots")
                if args.require_coinalyze:
                    conn.close()
                    print("ERROR: Coinalyze failed and snapshot fallback is disabled by --require-coinalyze")
                    sys.exit(1)
            time.sleep(REQUEST_DELAY_S)

        # Fallback to snapshots
        if inserted < 0 or not api_key:
            inserted = fallback_snapshots(conn, asset, args.months)
            if inserted < 0:
                conn.close()
                print("ERROR: OI backfill failed: Coinalyze unavailable and snapshot fallback source is missing")
                sys.exit(1)
            print(f"  Snapshot fallback: {inserted} daily rows inserted")

        results[asset] = validate(conn, asset, args.months)
        print(f"  Validation: {json.dumps(results[asset])}")
        print()

    conn.close()

    print("=== SUMMARY ===")
    for asset, v in results.items():
        status = "OK" if v["rows"] >= 180 else "WARN" if v["rows"] >= 7 else "FAIL"
        mixed = " MIXED_UNITS" if v.get("mixed_units") else ""
        print(
            f"  {asset}: {v['rows']} rows via {v['source']} [{v.get('oi_units', 'unknown')}], "
            f"{v['coverage_pct']}% coverage [{status}]{mixed}"
        )


if __name__ == "__main__":
    main()
