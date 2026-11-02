#!/usr/bin/env python3
"""Build hourly UTC-aligned research dataset from backfilled data.

Usage: python3 scripts/build_research_dataset.py [--db path] [--assets BTC,ETH,SOL]

Creates `research_dataset` table with signal states and forward return labels.
Works with 2 signals (funding + momentum) or 3 (+ OI when available).
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))

# Signal thresholds (from spec)
OI_CHANGE_THRESHOLD = 0.005     # 0.5%
FUNDING_8H_THRESHOLD = 0.00005  # 0.005% in 8h-equivalent
MOMENTUM_4H_THRESHOLD = 0.005   # 0.5%
OI_REQUIRED_UNITS = "usd_notional"


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_has_column(conn, table_name, column_name):
    if not table_exists(conn, table_name):
        return False
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    return column_name in cols


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS research_dataset")
    conn.execute("""
        CREATE TABLE research_dataset (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            anchor_ts   INTEGER NOT NULL,
            asset       TEXT NOT NULL,
            s1_state    TEXT,
            s2_state    TEXT NOT NULL,
            s3_state    TEXT NOT NULL,
            s4_state    TEXT,
            ret_1h      REAL,
            ret_4h      REAL,
            ret_1d      REAL,
            ret_3d      REAL,
            split       TEXT NOT NULL,
            close_price REAL,
            UNIQUE(asset, anchor_ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rd_split ON research_dataset(split, asset)")
    conn.commit()


def compute_s1_state(conn, asset, anchor_ms):
    """S1: OI regime. Returns ternary state or None if OI data insufficient.

    IMPORTANT: Coinalyze daily OI timestamps are period-START (midnight UTC).
    The 'close' value represents end-of-day OI. To avoid look-ahead bias,
    we lag by 24h: an OI reading timestamped at day D's midnight is only
    available starting at day D+1's midnight (anchor_ms - 86400000).
    """
    # Lag OI by 24h to avoid look-ahead bias from period-start timestamps
    safe_anchor = anchor_ms - 86400000  # subtract 24 hours

    row = conn.execute(
        """
        SELECT open_interest, timestamp_ms, source_detail
        FROM hl_oi_history
        WHERE asset = ? AND oi_units = ? AND timestamp_ms <= ?
        ORDER BY timestamp_ms DESC LIMIT 1
        """,
        (asset, OI_REQUIRED_UNITS, safe_anchor),
    ).fetchone()
    if not row:
        return None

    current_oi = row[0]
    current_ts = row[1]
    current_source_detail = row[2]

    # Get previous day's OI (at least 20h before current reading)
    prev = conn.execute(
        """
        SELECT open_interest
        FROM hl_oi_history
        WHERE asset = ?
          AND oi_units = ?
          AND source_detail = ?
          AND timestamp_ms <= ?
          AND timestamp_ms < ?
        ORDER BY timestamp_ms DESC LIMIT 1
        """,
        (
            asset,
            OI_REQUIRED_UNITS,
            current_source_detail,
            safe_anchor,
            current_ts - 72000000,
        ),  # 20h before current reading
    ).fetchone()
    if not prev or prev[0] <= 0:
        return None

    change = (current_oi - prev[0]) / prev[0]
    if change > OI_CHANGE_THRESHOLD:
        return "oi_rising"
    elif change < -OI_CHANGE_THRESHOLD:
        return "oi_falling"
    else:
        return "oi_flat"


def compute_s2_state(conn, asset, anchor_ms):
    """S2: Funding rate. Returns ternary state."""
    row = conn.execute(
        "SELECT funding_rate FROM hl_funding_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, anchor_ms),
    ).fetchone()
    if not row:
        return "funding_neutral"

    # Convert hourly rate to 8h-equivalent for threshold comparison
    fr_8h = row[0] * 8
    if fr_8h > FUNDING_8H_THRESHOLD:
        return "funding_positive"
    elif fr_8h < -FUNDING_8H_THRESHOLD:
        return "funding_negative"
    else:
        return "funding_neutral"


def compute_s3_state(conn, asset, anchor_ms):
    """S3: Price momentum (trailing 4h return). Returns ternary state."""
    # Price at anchor
    current = conn.execute(
        "SELECT close FROM price_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, anchor_ms),
    ).fetchone()

    # Price 4 hours ago
    four_h_ago = anchor_ms - 4 * 3600 * 1000
    prev = conn.execute(
        "SELECT close FROM price_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, four_h_ago),
    ).fetchone()

    if not current or not prev or prev[0] <= 0:
        return "mom_flat"

    ret = (current[0] - prev[0]) / prev[0]
    if ret > MOMENTUM_4H_THRESHOLD:
        return "mom_up"
    elif ret < -MOMENTUM_4H_THRESHOLD:
        return "mom_down"
    else:
        return "mom_flat"


def get_close_price(conn, asset, anchor_ms):
    """Get closing price at anchor for forward label computation."""
    row = conn.execute(
        "SELECT close FROM price_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, anchor_ms),
    ).fetchone()
    return row[0] if row else None


def get_forward_return(conn, asset, anchor_ms, hours):
    """Compute forward return from anchor to anchor + hours."""
    future_ms = anchor_ms + hours * 3600 * 1000
    current = conn.execute(
        "SELECT close FROM price_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, anchor_ms),
    ).fetchone()
    future = conn.execute(
        "SELECT close FROM price_history WHERE asset = ? AND timestamp_ms <= ? ORDER BY timestamp_ms DESC LIMIT 1",
        (asset, future_ms),
    ).fetchone()

    if not current or not future or current[0] <= 0:
        return None

    # Ensure future price actually comes from AFTER the anchor (no-leak)
    future_actual_ts = conn.execute(
        "SELECT timestamp_ms FROM price_history WHERE asset = ? AND timestamp_ms > ? ORDER BY timestamp_ms ASC LIMIT 1",
        (asset, anchor_ms),
    ).fetchone()
    if not future_actual_ts:
        return None

    return (future[0] - current[0]) / current[0]


def build_dataset(conn, assets, train_end_ts):
    """Build the full research dataset."""
    # Find data range from funding history (most reliable)
    range_row = conn.execute(
        "SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM hl_funding_history"
    ).fetchone()
    if not range_row or not range_row[0]:
        print("ERROR: No funding history data found")
        return 0

    start_ms = range_row[0]
    end_ms = range_row[1]

    # Align to hourly boundaries
    start_hour = (start_ms // 3600000 + 1) * 3600000  # next full hour
    # Leave room for 3d forward label at the end
    end_hour = end_ms - 72 * 3600000

    # Check if OI data is available
    oi_ready = (
        table_exists(conn, "hl_oi_history")
        and table_has_column(conn, "hl_oi_history", "oi_units")
        and table_has_column(conn, "hl_oi_history", "source_detail")
    )
    oi_count = 0
    if oi_ready:
        oi_count = conn.execute(
            "SELECT COUNT(*) FROM hl_oi_history WHERE oi_units = ? AND timestamp_ms < ?",
            (OI_REQUIRED_UNITS, end_ms),
        ).fetchone()[0]
    has_oi = oi_count >= 30  # need at least 30 days for meaningful OI signal
    if has_oi:
        print(f"OI data available: {oi_count} {OI_REQUIRED_UNITS} rows — S1 enabled")
    elif oi_ready:
        print(f"OI data insufficient: {oi_count} {OI_REQUIRED_UNITS} rows — S1 will be NULL")
    else:
        print("OI data missing units metadata or source provenance — S1 disabled")

    total_inserted = 0
    anchor_ms = start_hour

    while anchor_ms <= end_hour:
        for asset in assets:
            s1 = compute_s1_state(conn, asset, anchor_ms) if has_oi else None
            s2 = compute_s2_state(conn, asset, anchor_ms)
            s3 = compute_s3_state(conn, asset, anchor_ms)
            close_px = get_close_price(conn, asset, anchor_ms)

            # Forward labels
            ret_1h = get_forward_return(conn, asset, anchor_ms, 1)
            ret_4h = get_forward_return(conn, asset, anchor_ms, 4)
            ret_1d = get_forward_return(conn, asset, anchor_ms, 24)
            ret_3d = get_forward_return(conn, asset, anchor_ms, 72)

            # Train/test split
            split = "train" if anchor_ms < train_end_ts else "test"

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO research_dataset
                       (anchor_ts, asset, s1_state, s2_state, s3_state, s4_state,
                        ret_1h, ret_4h, ret_1d, ret_3d, split, close_price)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (anchor_ms, asset, s1, s2, s3, ret_1h, ret_4h, ret_1d, ret_3d, split, close_px),
                )
                total_inserted += 1
            except sqlite3.IntegrityError:
                pass

        # Commit every 1000 rows
        if total_inserted % 3000 == 0:
            conn.commit()
            dt = datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            print(f"  {total_inserted} rows built, at {dt}...")

        anchor_ms += 3600000  # +1 hour

    conn.commit()
    return total_inserted


def validate(conn, assets):
    """Validate the built dataset."""
    print("\n=== DATASET VALIDATION ===")

    for asset in assets:
        row = conn.execute("""
            SELECT COUNT(*) as n,
                SUM(CASE WHEN split = 'train' THEN 1 ELSE 0 END) as train_n,
                SUM(CASE WHEN split = 'test' THEN 1 ELSE 0 END) as test_n,
                SUM(CASE WHEN s1_state IS NOT NULL THEN 1 ELSE 0 END) as has_s1,
                SUM(CASE WHEN ret_1h IS NOT NULL THEN 1 ELSE 0 END) as has_ret,
                SUM(CASE WHEN ret_3d IS NOT NULL THEN 1 ELSE 0 END) as has_3d
            FROM research_dataset WHERE asset = ?
        """, (asset,)).fetchone()

        print(f"\n{asset}:")
        print(f"  Total: {row[0]}  Train: {row[1]}  Test: {row[2]}")
        print(f"  Has S1 (OI): {row[3]}  Has 1h label: {row[4]}  Has 3d label: {row[5]}")

    # Signal state distribution
    print("\n=== SIGNAL STATE DISTRIBUTION ===")
    for signal, col in [("S2 funding", "s2_state"), ("S3 momentum", "s3_state")]:
        rows = conn.execute(f"""
            SELECT {col}, COUNT(*) as n FROM research_dataset
            WHERE split = 'train' GROUP BY {col} ORDER BY n DESC
        """).fetchall()
        print(f"\n{signal} (train):")
        for r in rows:
            print(f"  {r[0]}: {r[1]}")

    # Check no-leak: verify no forward label uses data at or before anchor
    # (Structural guarantee from code, but let's check ret_1h is reasonable)
    sample = conn.execute("""
        SELECT anchor_ts, asset, close_price, ret_1h FROM research_dataset
        WHERE ret_1h IS NOT NULL LIMIT 5
    """).fetchall()
    print("\nSample rows (no-leak spot check):")
    for r in sample:
        dt = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        print(f"  {dt} {r[1]} close=${r[2]:.2f} ret_1h={r[3]*100:.4f}%")


def main():
    parser = argparse.ArgumentParser(description="Build research dataset")
    parser.add_argument("--db", type=str, default=DB_PATH)
    parser.add_argument("--assets", type=str, default="BTC,ETH,SOL")
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",")]
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    ensure_table(conn)

    # Train/test split: first 4 months train, last 2 months test
    # Data starts ~Sep 30 2025, so train ends ~Jan 30 2026
    train_end = datetime(2026, 1, 30, 0, 0, 0, tzinfo=timezone.utc)
    train_end_ms = int(train_end.timestamp() * 1000)

    print(f"Building research dataset for {assets}")
    print(f"Train/test split at: {train_end.strftime('%Y-%m-%d')}")
    print(f"DB: {args.db}")
    print()

    total = build_dataset(conn, assets, train_end_ms)
    print(f"\nTotal rows built: {total}")

    validate(conn, assets)

    conn.close()


if __name__ == "__main__":
    main()
