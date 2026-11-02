#!/usr/bin/env python3
"""Audit trade-fetch coverage for the stink-bid backtest snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.backtest.schema import open_backtest_db


def _where(tour: Optional[str]) -> tuple[str, list]:
    clauses = [
        "EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_yes AND p.fidelity = 1)",
        "EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_no AND p.fidelity = 1)",
    ]
    params = []
    if tour:
        clauses.append("LOWER(a.sack_tour) LIKE ?")
        params.append(f"{tour.lower()}%")
    return " AND ".join(clauses), params


def audit(db_path: str | Path, tour: Optional[str] = None) -> dict:
    con = open_backtest_db(str(db_path))
    try:
        where, params = _where(tour)
        totals = con.execute(
            f"""
            WITH target AS (
                SELECT g.condition_id, a.sack_tour, g.token_id_yes, g.token_id_no
                FROM gamma_events g
                JOIN event_alignment a USING(condition_id)
                WHERE {where}
            )
            SELECT
                COUNT(*) AS target_markets,
                COUNT(tc.condition_id) AS coverage_rows,
                COUNT(*) FILTER (WHERE tc.condition_id IS NULL) AS missing_coverage,
                COUNT(*) FILTER (WHERE tc.hit_page_cap) AS hit_page_cap,
                COUNT(*) FILTER (WHERE tc.error IS NOT NULL AND tc.error <> '') AS fetch_errors,
                COALESCE(SUM(tc.rows_fetched), 0) AS rows_fetched,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM trades tr
                        WHERE tr.condition_id = target.condition_id
                    )
                ) AS markets_with_any_trade,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM trades tr
                        WHERE tr.condition_id = target.condition_id
                          AND tr.asset IN (target.token_id_yes, target.token_id_no)
                    )
                ) AS markets_with_token_trade,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM trades tr
                        WHERE tr.condition_id = target.condition_id
                          AND tr.asset IN (target.token_id_yes, target.token_id_no)
                          AND tr.size >= 50
                    )
                ) AS markets_with_token_trade_size_gte_50
            FROM target
            LEFT JOIN trade_coverage tc USING(condition_id)
            """,
            params,
        ).fetchone()
        by_tour = con.execute(
            f"""
            WITH target AS (
                SELECT g.condition_id, a.sack_tour, g.token_id_yes, g.token_id_no
                FROM gamma_events g
                JOIN event_alignment a USING(condition_id)
                WHERE {where}
            )
            SELECT
                CASE WHEN LOWER(sack_tour) LIKE 'atp%' THEN 'atp'
                     WHEN LOWER(sack_tour) LIKE 'wta%' THEN 'wta'
                     ELSE 'other' END AS tour,
                COUNT(*) AS target_markets,
                COUNT(tc.condition_id) AS coverage_rows,
                COUNT(*) FILTER (WHERE tc.condition_id IS NULL) AS missing_coverage,
                COUNT(*) FILTER (WHERE tc.hit_page_cap) AS hit_page_cap,
                COALESCE(SUM(tc.rows_fetched), 0) AS rows_fetched,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM trades tr
                        WHERE tr.condition_id = target.condition_id
                          AND tr.asset IN (target.token_id_yes, target.token_id_no)
                          AND tr.size >= 50
                    )
                ) AS markets_with_token_trade_size_gte_50
            FROM target
            LEFT JOIN trade_coverage tc USING(condition_id)
            GROUP BY 1
            ORDER BY 1
            """,
            params,
        ).fetchall()
        missing_examples = con.execute(
            f"""
            WITH target AS (
                SELECT g.condition_id, g.title, a.sack_tour, g.end_date
                FROM gamma_events g
                JOIN event_alignment a USING(condition_id)
                WHERE {where}
            )
            SELECT condition_id, title, sack_tour, CAST(end_date AS VARCHAR) AS end_date
            FROM target
            LEFT JOIN trade_coverage tc USING(condition_id)
            WHERE tc.condition_id IS NULL
            ORDER BY end_date NULLS LAST, condition_id
            LIMIT 20
            """,
            params,
        ).fetchall()
        capped_examples = con.execute(
            f"""
            WITH target AS (
                SELECT g.condition_id, g.title, a.sack_tour, g.end_date
                FROM gamma_events g
                JOIN event_alignment a USING(condition_id)
                WHERE {where}
            )
            SELECT condition_id, title, sack_tour, tc.rows_fetched, tc.pages_fetched,
                   tc.page_size, tc.max_pages, CAST(greatest(tc.first_trade_ts, 0) AS BIGINT) AS first_trade_ts,
                   CAST(greatest(tc.last_trade_ts, 0) AS BIGINT) AS last_trade_ts
            FROM target
            JOIN trade_coverage tc USING(condition_id)
            WHERE tc.hit_page_cap
            ORDER BY tc.rows_fetched DESC, condition_id
            LIMIT 20
            """,
            params,
        ).fetchall()
        keys = [
            "target_markets",
            "coverage_rows",
            "missing_coverage",
            "hit_page_cap",
            "fetch_errors",
            "rows_fetched",
            "markets_with_any_trade",
            "markets_with_token_trade",
            "markets_with_token_trade_size_gte_50",
        ]
        out = {
            "db_path": str(db_path),
            "tour": tour,
            "summary": dict(zip(keys, totals)),
            "by_tour": [
                {
                    "tour": row[0],
                    "target_markets": row[1],
                    "coverage_rows": row[2],
                    "missing_coverage": row[3],
                    "hit_page_cap": row[4],
                    "rows_fetched": row[5],
                    "markets_with_token_trade_size_gte_50": row[6],
                }
                for row in by_tour
            ],
            "missing_examples": [
                {"condition_id": row[0], "title": row[1], "sack_tour": row[2], "end_date": row[3]}
                for row in missing_examples
            ],
            "hit_page_cap_examples": [
                {
                    "condition_id": row[0],
                    "title": row[1],
                    "sack_tour": row[2],
                    "rows_fetched": row[3],
                    "pages_fetched": row[4],
                    "page_size": row[5],
                    "max_pages": row[6],
                    "first_trade_ts": row[7],
                    "last_trade_ts": row[8],
                }
                for row in capped_examples
            ],
        }
        total = max(int(out["summary"]["target_markets"] or 0), 1)
        out["summary"]["coverage_pct"] = float(out["summary"]["coverage_rows"] or 0) / total
        out["summary"]["missing_coverage_pct"] = float(out["summary"]["missing_coverage"] or 0) / total
        return out
    finally:
        con.close()


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--output", help="Write JSON report to this path.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    out = audit(args.db, tour=args.tour)
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
