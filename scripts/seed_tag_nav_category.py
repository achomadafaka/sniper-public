#!/usr/bin/env python3
"""Seed the tag_nav_category table from data/tag_nav_category_seed.yaml.

Idempotent. Reads the YAML, validates, upserts. Reports coverage against
the live `market_tags` table so operators can see how many tags are
unmapped (will fall through to nav_category='other' or be excluded from
nav-grain inference).

Usage:
    python3 scripts/seed_tag_nav_category.py [--db sniper_tracker.db] [--dry-run]
    python3 scripts/seed_tag_nav_category.py --report-only   # just print coverage
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

VALID_NAV_CATEGORIES = {
    "politics", "sports", "crypto", "esports", "iran",
    "finance", "geopolitics", "tech", "culture", "economy",
    "weather", "mentions", "elections", "other",
}

SEED_PATH = os.path.join(_REPO_ROOT, "data", "tag_nav_category_seed.yaml")


def load_seed(path: str = SEED_PATH) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    for key in ("mappings", "skip", "ambiguous"):
        if key not in data:
            data[key] = {} if key != "skip" else []
    return data


def validate(seed: dict) -> list[str]:
    """Return list of validation error messages (empty if clean)."""
    errors = []
    for tag, nav in seed["mappings"].items():
        if nav not in VALID_NAV_CATEGORIES:
            errors.append(f"  tag={tag!r}: nav_category={nav!r} not in {sorted(VALID_NAV_CATEGORIES)}")
    if not isinstance(seed["skip"], list):
        errors.append("  'skip' must be a list of tag strings")
    return errors


def upsert_table(conn: sqlite3.Connection, seed: dict, dry_run: bool = False) -> dict:
    """Idempotent upsert. Returns counts."""
    # Create table directly — avoid full init_db (which has unrelated row_factory
    # quirks in other modules). Schema mirrors db.py:2126-2138.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tag_nav_category (
            tag           TEXT    PRIMARY KEY,
            nav_category  TEXT    NOT NULL,
            confidence    TEXT    NOT NULL DEFAULT 'manual',
            updated_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tag_nav_category_nav
            ON tag_nav_category (nav_category);
    """)
    now = int(time.time())

    counts = {
        "mappings_loaded": 0,
        "skip_loaded": 0,
        "ambiguous_loaded": 0,
    }

    for tag, nav in seed["mappings"].items():
        if not dry_run:
            conn.execute(
                """
                INSERT INTO tag_nav_category (tag, nav_category, confidence, updated_at)
                VALUES (?, ?, 'manual', ?)
                ON CONFLICT(tag) DO UPDATE SET
                    nav_category = excluded.nav_category,
                    confidence   = excluded.confidence,
                    updated_at   = excluded.updated_at
                """,
                (tag, nav, now),
            )
        counts["mappings_loaded"] += 1

    # skip-list and ambiguous tags are stored too — with sentinel nav_category
    # so the protocol layer can distinguish "explicitly skipped" from "unmapped".
    for tag in seed.get("skip", []):
        if not dry_run:
            conn.execute(
                """
                INSERT INTO tag_nav_category (tag, nav_category, confidence, updated_at)
                VALUES (?, 'other', 'skip', ?)
                ON CONFLICT(tag) DO UPDATE SET
                    confidence = 'skip',
                    updated_at = excluded.updated_at
                """,
                (tag, now),
            )
        counts["skip_loaded"] += 1

    for tag in seed.get("ambiguous", {}):
        if not dry_run:
            conn.execute(
                """
                INSERT INTO tag_nav_category (tag, nav_category, confidence, updated_at)
                VALUES (?, 'other', 'ambiguous', ?)
                ON CONFLICT(tag) DO UPDATE SET
                    confidence = 'ambiguous',
                    updated_at = excluded.updated_at
                """,
                (tag, now),
            )
        counts["ambiguous_loaded"] += 1

    if not dry_run:
        conn.commit()
    return counts


def report_coverage(conn: sqlite3.Connection) -> None:
    """Print coverage summary: unmapped tags by frequency."""
    conn.row_factory = sqlite3.Row

    total_tags = conn.execute(
        "SELECT COUNT(DISTINCT tag) FROM market_tags"
    ).fetchone()[0]
    mapped = conn.execute(
        "SELECT COUNT(*) FROM tag_nav_category"
    ).fetchone()[0]

    print(f"\n=== Coverage report ===")
    print(f"  total distinct tags in market_tags: {total_tags}")
    print(f"  tags in tag_nav_category:            {mapped}")
    print(f"  coverage:                            {mapped/total_tags*100:.1f}%")

    print(f"\n  by confidence:")
    for r in conn.execute(
        "SELECT confidence, COUNT(*) FROM tag_nav_category GROUP BY confidence ORDER BY COUNT(*) DESC"
    ):
        print(f"    {r[0]:12s}  {r[1]}")

    print(f"\n  by nav_category (mapped only — excludes skip/ambiguous):")
    for r in conn.execute(
        """SELECT nav_category, COUNT(*) FROM tag_nav_category
            WHERE confidence='manual'
         GROUP BY nav_category ORDER BY COUNT(*) DESC"""
    ):
        print(f"    {r[0]:12s}  {r[1]}")

    print(f"\n  TOP 30 UNMAPPED tags by market_tags frequency:")
    rows = conn.execute(
        """
        SELECT mt.tag, COUNT(*) AS n
          FROM market_tags mt
          LEFT JOIN tag_nav_category tnc ON tnc.tag = mt.tag
         WHERE tnc.tag IS NULL
         GROUP BY mt.tag
         ORDER BY n DESC
         LIMIT 30
        """
    ).fetchall()
    for r in rows:
        print(f"    {r['tag']:35s}  {r['n']:>6d}")
    if not rows:
        print("    (all tags mapped)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + report but don't write to DB")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip seed, just print coverage of current state")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        if args.report_only:
            report_coverage(conn)
            return 0

        seed = load_seed()
        errors = validate(seed)
        if errors:
            print("Validation errors:")
            for e in errors:
                print(e)
            return 1

        counts = upsert_table(conn, seed, dry_run=args.dry_run)
        action = "DRY RUN — nothing written" if args.dry_run else "WROTE"
        print(f"{action}:")
        print(f"  mappings:  {counts['mappings_loaded']}")
        print(f"  skip:      {counts['skip_loaded']}")
        print(f"  ambiguous: {counts['ambiguous_loaded']}")

        report_coverage(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
