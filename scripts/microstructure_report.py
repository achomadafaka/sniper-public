#!/usr/bin/env python3
"""Microstructure report — spread, size, frequency analysis on normalized Parquet.

Two modes:
  --full-report      All markets, grouped by market_type and time-to-end bucket
  --near-dated-report  Only markets in the current snapshot (ending within 14 days),
                        per-market profile with taker-fill viability verdict

Uses DuckDB for fast analytical queries over Parquet files.

Usage:
  python3 scripts/microstructure_report.py --full-report --normalized-dir ~/Desktop/polymarket-research/normalized
  python3 scripts/microstructure_report.py --near-dated-report --normalized-dir ~/Desktop/polymarket-research/normalized --snapshot state/markets_snapshot.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

import duckdb


def _connect(normalized_dir: str) -> duckdb.DuckDBPyConnection:
    """Create DuckDB connection with Parquet views."""
    con = duckdb.connect(":memory:")

    con.execute(f"""
        CREATE VIEW quotes AS
        SELECT * FROM read_parquet('{normalized_dir}/quotes_top/*.parquet')
    """)
    con.execute(f"""
        CREATE VIEW trades AS
        SELECT * FROM read_parquet('{normalized_dir}/trades/*.parquet')
    """)
    con.execute(f"""
        CREATE VIEW lifecycle AS
        SELECT * FROM read_parquet('{normalized_dir}/market_lifecycle/*.parquet')
    """)
    return con


def _build_market_meta_table(con: duckdb.DuckDBPyConnection) -> None:
    """Extract end_date and market_type from lifecycle metadata payloads."""
    con.execute("""
        CREATE TABLE market_meta AS
        SELECT DISTINCT ON (market_id)
            market_id,
            market_type,
            json_extract_string(payload, '$.end_date') AS end_date
        FROM lifecycle
        WHERE event_type = 'market_metadata'
        ORDER BY market_id, capture_ts DESC, ingest_seq DESC
    """)


def _print_section(title: str, body: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    print(body)


def full_report(con: duckdb.DuckDBPyConnection) -> None:
    """Run the full microstructure report."""
    _build_market_meta_table(con)

    # Add time-to-end bucket
    con.execute("""
        CREATE VIEW quotes_enriched AS
        SELECT q.*,
            m.market_type,
            m.end_date,
            CASE
                WHEN m.end_date IS NULL THEN 'unknown'
                WHEN DATEDIFF('day', q.capture_ts::TIMESTAMP, m.end_date::TIMESTAMP) <= 7 THEN '0-7d'
                WHEN DATEDIFF('day', q.capture_ts::TIMESTAMP, m.end_date::TIMESTAMP) <= 14 THEN '7-14d'
                WHEN DATEDIFF('day', q.capture_ts::TIMESTAMP, m.end_date::TIMESTAMP) <= 30 THEN '14-30d'
                ELSE '30-90d'
            END AS tte_bucket
        FROM quotes q
        LEFT JOIN market_meta m ON q.market_id = m.market_id
    """)

    con.execute("""
        CREATE VIEW trades_enriched AS
        SELECT t.*,
            m.market_type
        FROM trades t
        LEFT JOIN market_meta m ON t.market_id = m.market_id
    """)

    # --- Spread distribution ---
    spread_by_type = con.execute("""
        SELECT
            market_type,
            COUNT(*) AS n,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p75,
            ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p99
        FROM quotes_enriched
        WHERE best_ask IS NOT NULL AND best_bid IS NOT NULL AND best_ask >= best_bid
        GROUP BY market_type
        ORDER BY n DESC
    """).fetchdf().to_string(index=False)

    spread_by_tte = con.execute("""
        SELECT
            tte_bucket,
            COUNT(*) AS n,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p75,
            ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS p99
        FROM quotes_enriched
        WHERE best_ask IS NOT NULL AND best_bid IS NOT NULL AND best_ask >= best_bid
        GROUP BY tte_bucket
        ORDER BY tte_bucket
    """).fetchdf().to_string(index=False)

    _print_section("SPREAD DISTRIBUTION (best_ask - best_bid)", f"""
By market type:
{spread_by_type}

By time-to-end bucket:
{spread_by_tte}
""")

    # --- Top-of-book size distribution ---
    size_by_type = con.execute("""
        SELECT
            market_type,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN ask_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS ask_null_pct,
            ROUND(100.0 * SUM(CASE WHEN bid_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS bid_null_pct,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ask_size), 1) AS ask_p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask_size), 1) AS ask_median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ask_size), 1) AS ask_p75,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY bid_size), 1) AS bid_p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY bid_size), 1) AS bid_median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY bid_size), 1) AS bid_p75
        FROM quotes_enriched
        GROUP BY market_type
        ORDER BY n DESC
    """).fetchdf().to_string(index=False)

    size_by_tte = con.execute("""
        SELECT
            tte_bucket,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN ask_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS ask_null_pct,
            ROUND(100.0 * SUM(CASE WHEN bid_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS bid_null_pct,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask_size), 1) AS ask_median,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY bid_size), 1) AS bid_median
        FROM quotes_enriched
        GROUP BY tte_bucket
        ORDER BY tte_bucket
    """).fetchdf().to_string(index=False)

    _print_section("TOP-OF-BOOK SIZE DISTRIBUTION", f"""
By market type:
{size_by_type}

By time-to-end bucket:
{size_by_tte}
""")

    # --- Null size % and crossed/locked quotes ---
    quality = con.execute("""
        SELECT
            COUNT(*) AS total_quotes,
            ROUND(100.0 * SUM(CASE WHEN ask_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS ask_null_pct,
            ROUND(100.0 * SUM(CASE WHEN bid_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS bid_null_pct,
            ROUND(100.0 * SUM(CASE WHEN best_bid IS NOT NULL AND best_ask IS NOT NULL
                AND best_bid > best_ask THEN 1 ELSE 0 END) / COUNT(*), 4) AS crossed_pct,
            ROUND(100.0 * SUM(CASE WHEN best_bid IS NOT NULL AND best_ask IS NOT NULL
                AND best_bid = best_ask THEN 1 ELSE 0 END) / COUNT(*), 4) AS locked_pct,
            ROUND(100.0 * SUM(CASE WHEN best_bid IS NULL OR best_ask IS NULL THEN 1 ELSE 0 END)
                / COUNT(*), 2) AS missing_bba_pct
        FROM quotes_enriched
    """).fetchdf().to_string(index=False)

    _print_section("QUOTE QUALITY", quality)

    # --- Trade frequency ---
    trade_freq = con.execute("""
        SELECT
            market_type,
            COUNT(*) AS total_trades,
            COUNT(DISTINCT market_id) AS markets,
            ROUND(COUNT(*) * 1.0 / GREATEST(COUNT(DISTINCT DATE_TRUNC('hour', capture_ts::TIMESTAMP)), 1), 1) AS trades_per_hour,
            ROUND(COUNT(*) * 1.0 / GREATEST(COUNT(DISTINCT market_id), 1), 1) AS trades_per_market
        FROM trades_enriched
        GROUP BY market_type
        ORDER BY total_trades DESC
    """).fetchdf().to_string(index=False)

    _print_section("TRADE FREQUENCY", trade_freq)

    # --- Quote update frequency ---
    quote_freq = con.execute("""
        SELECT
            market_type,
            COUNT(*) AS total_quotes,
            COUNT(DISTINCT market_id) AS markets,
            ROUND(COUNT(*) * 1.0 / GREATEST(COUNT(DISTINCT DATE_TRUNC('hour', capture_ts::TIMESTAMP)), 1), 0) AS quotes_per_hour,
            ROUND(COUNT(*) * 1.0 / GREATEST(COUNT(DISTINCT market_id), 1), 0) AS quotes_per_market
        FROM quotes_enriched
        GROUP BY market_type
        ORDER BY total_quotes DESC
    """).fetchdf().to_string(index=False)

    _print_section("QUOTE UPDATE FREQUENCY", quote_freq)

    # --- Summary ---
    summary = con.execute("""
        SELECT
            COUNT(DISTINCT q.market_id) AS unique_markets,
            MIN(q.capture_ts) AS first_ts,
            MAX(q.capture_ts) AS last_ts,
            (SELECT COUNT(*) FROM trades) AS total_trades,
            COUNT(*) AS total_quotes
        FROM quotes q
    """).fetchdf().to_string(index=False)

    _print_section("DATA COVERAGE", summary)


def near_dated_report(con: duckdb.DuckDBPyConnection, snapshot_path: str) -> None:
    """Run near-dated market profile with taker-fill viability."""
    _build_market_meta_table(con)

    # Load snapshot and enforce 14-day filter inside the script.
    # The snapshot may already be horizon-tightened, but we don't assume that.
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    con.execute("CREATE TABLE near_markets (market_id VARCHAR, question VARCHAR, end_date VARCHAR, volume DOUBLE)")
    included = 0
    excluded = 0
    for m in snapshot:
        end = m.get("end_date", "")
        if not end or end > cutoff:
            excluded += 1
            continue
        con.execute(
            "INSERT INTO near_markets VALUES (?, ?, ?, ?)",
            [m["market_id"], m.get("question", "")[:60], end, m.get("volume", 0)]
        )
        included += 1
    print(f"Snapshot: {len(snapshot)} markets, {included} within 14-day horizon, {excluded} excluded")

    # --- Per-market profile ---
    per_market = con.execute("""
        SELECT
            nm.market_id,
            nm.question,
            nm.end_date,
            ROUND(nm.volume, 0) AS volume,
            COUNT(q.market_id) AS quote_count,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.best_ask - q.best_bid), 4) AS med_spread,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.ask_size), 1) AS med_ask_size,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.bid_size), 1) AS med_bid_size,
            ROUND(100.0 * SUM(CASE WHEN q.ask_size IS NULL THEN 1 ELSE 0 END) / GREATEST(COUNT(*), 1), 1) AS null_size_pct,
            ROUND(100.0 * SUM(CASE WHEN q.best_bid IS NOT NULL AND q.best_ask IS NOT NULL
                AND q.best_bid >= q.best_ask THEN 1 ELSE 0 END) / GREATEST(COUNT(*), 1), 2) AS crossed_locked_pct,
            (SELECT COUNT(*) FROM trades t WHERE t.market_id = nm.market_id) AS trade_count,
            CASE
                WHEN PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.best_ask - q.best_bid) > 0.05 THEN 'wide'
                WHEN PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.ask_size) < 10 THEN 'thin'
                ELSE 'ok'
            END AS spread_flag,
            CASE
                WHEN PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.ask_size) < 10 THEN 'thin'
                WHEN PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.ask_size) < 50 THEN 'modest'
                ELSE 'ok'
            END AS size_flag
        FROM near_markets nm
        LEFT JOIN quotes q ON q.market_id = nm.market_id
        WHERE q.best_ask IS NOT NULL AND q.best_bid IS NOT NULL
        GROUP BY nm.market_id, nm.question, nm.end_date, nm.volume
        ORDER BY nm.volume DESC
    """).fetchdf()

    # Compute viability verdict
    def viability(row):
        if row["med_spread"] is None or row["quote_count"] == 0:
            return "no data"
        if row["med_spread"] > 0.05:
            return "poor"
        if row["med_ask_size"] is not None and row["med_ask_size"] < 10:
            return "poor"
        if row["med_spread"] > 0.03 or (row["med_ask_size"] is not None and row["med_ask_size"] < 50):
            return "marginal"
        return "good"

    per_market["taker_viability"] = per_market.apply(viability, axis=1)

    _print_section("NEAR-DATED MARKET PROFILE (snapshot markets)", "")

    # Summary counts
    viability_counts = per_market["taker_viability"].value_counts()
    print(f"Viability breakdown: {dict(viability_counts)}")
    print(f"Total markets: {len(per_market)}\n")

    # Top markets table
    display_cols = ["question", "end_date", "volume", "quote_count", "trade_count",
                    "med_spread", "med_ask_size", "null_size_pct", "taker_viability"]
    print(per_market[display_cols].to_string(index=False))

    # --- Aggregate near-dated stats ---
    agg = con.execute("""
        SELECT
            COUNT(*) AS total_quotes,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY best_ask - best_bid), 4) AS med_spread,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ask_size), 1) AS med_ask_size,
            ROUND(100.0 * SUM(CASE WHEN ask_size IS NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS null_size_pct,
            ROUND(100.0 * SUM(CASE WHEN best_bid >= best_ask THEN 1 ELSE 0 END) / COUNT(*), 2) AS crossed_locked_pct
        FROM quotes q
        JOIN near_markets nm ON q.market_id = nm.market_id
        WHERE q.best_ask IS NOT NULL AND q.best_bid IS NOT NULL
    """).fetchdf().to_string(index=False)

    _print_section("NEAR-DATED AGGREGATE", agg)

    # --- Per-market first-vs-last-day liquidity comparison ---
    # Tests whether individual markets thin into resolution, not just
    # whether the aggregate stays stable.
    first_last = con.execute("""
        WITH per_market_day AS (
            SELECT
                q.market_id,
                q.capture_ts::DATE AS dt,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.best_ask - q.best_bid) AS med_spread,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY q.ask_size) AS med_ask_size,
                COUNT(*) AS n
            FROM quotes q
            JOIN near_markets nm ON q.market_id = nm.market_id
            WHERE q.best_ask IS NOT NULL AND q.best_bid IS NOT NULL
            GROUP BY q.market_id, dt
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY dt ASC) AS rn_first,
                ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY dt DESC) AS rn_last
            FROM per_market_day
        ),
        first_day AS (SELECT market_id, med_spread, med_ask_size, n, dt FROM ranked WHERE rn_first = 1),
        last_day AS (SELECT market_id, med_spread, med_ask_size, n, dt FROM ranked WHERE rn_last = 1)
        SELECT
            nm.question,
            f.dt AS first_dt,
            ROUND(f.med_spread, 4) AS first_spread,
            ROUND(f.med_ask_size, 1) AS first_ask_size,
            l.dt AS last_dt,
            ROUND(l.med_spread, 4) AS last_spread,
            ROUND(l.med_ask_size, 1) AS last_ask_size,
            CASE
                WHEN f.dt = l.dt THEN 'single_day'
                WHEN l.med_spread > f.med_spread * 2.0 THEN 'WIDENED'
                WHEN l.med_ask_size IS NOT NULL AND f.med_ask_size IS NOT NULL
                     AND l.med_ask_size < f.med_ask_size * 0.25 THEN 'THINNED'
                ELSE 'stable'
            END AS into_resolution
        FROM first_day f
        JOIN last_day l ON f.market_id = l.market_id
        JOIN near_markets nm ON nm.market_id = f.market_id
        WHERE f.dt != l.dt
        ORDER BY nm.volume DESC
    """).fetchdf()

    if len(first_last) > 0:
        widened = (first_last["into_resolution"] == "WIDENED").sum()
        thinned = (first_last["into_resolution"] == "THINNED").sum()
        stable = (first_last["into_resolution"] == "stable").sum()
        print(f"Markets with multi-day data: {len(first_last)}")
        print(f"  stable: {stable}, WIDENED (spread 2x+): {widened}, THINNED (size <25%): {thinned}\n")
        display_cols = ["question", "first_dt", "first_spread", "first_ask_size",
                        "last_dt", "last_spread", "last_ask_size", "into_resolution"]
        print(first_last[display_cols].to_string(index=False))
    else:
        print("Insufficient multi-day data for per-market comparison.")

    _print_section("NEAR-DATED: PER-MARKET FIRST vs LAST DAY", "")


def main():
    parser = argparse.ArgumentParser(description="Microstructure report on normalized Parquet")
    parser.add_argument("--normalized-dir", required=True, help="Path to normalized/ Parquet directory")
    parser.add_argument("--full-report", action="store_true", help="Run full microstructure report")
    parser.add_argument("--near-dated-report", action="store_true", help="Run near-dated market profile")
    parser.add_argument("--snapshot", default=None, help="Path to markets_snapshot.json (for near-dated)")
    args = parser.parse_args()

    if not args.full_report and not args.near_dated_report:
        parser.error("Specify --full-report, --near-dated-report, or both")

    con = _connect(args.normalized_dir)

    if args.full_report:
        print("\n" + "=" * 60)
        print("  MICROSTRUCTURE REPORT — FULL")
        print("=" * 60)
        full_report(con)

    if args.near_dated_report:
        if not args.snapshot:
            parser.error("--near-dated-report requires --snapshot")
        print("\n" + "=" * 60)
        print("  MICROSTRUCTURE REPORT — NEAR-DATED")
        print("=" * 60)
        near_dated_report(con, args.snapshot)

    con.close()


if __name__ == "__main__":
    main()
