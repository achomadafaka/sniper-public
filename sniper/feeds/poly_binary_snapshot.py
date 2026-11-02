#!/usr/bin/env python3
"""
poly_binary_snapshot.py — Fetch active binary (Yes/No) Polymarket markets
from the Gamma API and insert price snapshots into poly_price_snapshots.

Targets political, financial, crypto, and world-events markets. Excludes
sports markets. Designed to run standalone (cron) or be called as a module.

Schedule: runs every 30 minutes alongside discovery_alert.py.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Allow running directly from the repo root or as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_connection  # noqa: E402
from sniper.persistence.db_snapshots import (  # noqa: E402
    ensure_poly_price_snapshots_table,
    insert_poly_price_snapshot,
)

log = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# Tag slugs that indicate sports markets — we exclude these.
SPORTS_TAG_SLUGS = frozenset(
    {
        "sports",
        "sport",
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "soccer",
        "football",
        "tennis",
        "golf",
        "boxing",
        "ufc",
        "mma",
        "cricket",
        "baseball",
        "basketball",
        "hockey",
        "racing",
        "esports",
        "olympics",
    }
)

# Minimum USDC volume to consider a market worth snapshotting.
MIN_VOLUME = 500.0

# How many markets to fetch per page (Gamma API max is 500).
PAGE_LIMIT = 500

# Maximum pages to fetch (safety cap — 500*20 = 10k markets).
MAX_PAGES = 20

# Request timeout in seconds.
REQUEST_TIMEOUT = 20


def _parse_json_field(raw: Any) -> list:
    """Parse a Gamma API field that may be a JSON string or already a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _is_sports(events: Any) -> bool:
    """Return True if any event tag on this market indicates sports."""
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        tags = event.get("tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            slug = str(tag.get("slug") or "").strip().lower()
            label = str(tag.get("label") or "").strip().lower()
            if slug in SPORTS_TAG_SLUGS or label in SPORTS_TAG_SLUGS:
                return True
    return False


def fetch_binary_markets(session: requests.Session) -> list[dict]:
    """
    Fetch all active binary Yes/No markets from the Gamma API.

    Returns a list of dicts with keys:
        condition_id, question, yes_price, no_price, volume
    """
    markets: list[dict] = []
    offset = 0

    for page in range(MAX_PAGES):
        try:
            resp = session.get(
                f"{GAMMA_BASE_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            page_data = resp.json()
        except requests.exceptions.RequestException as exc:
            log.warning("Gamma API request failed (page %d, offset %d): %s", page, offset, exc)
            break

        if not isinstance(page_data, list) or not page_data:
            break

        for m in page_data:
            if not isinstance(m, dict):
                continue

            # Must be active and not closed.
            if not m.get("active") or m.get("closed"):
                continue

            # Parse outcomes — must be exactly ["Yes", "No"].
            outcomes = _parse_json_field(m.get("outcomes"))
            if len(outcomes) != 2:
                continue
            outcomes_lower = [str(o).strip().lower() for o in outcomes]
            if outcomes_lower != ["yes", "no"]:
                continue

            # Parse outcomePrices — must have exactly 2 valid floats.
            prices_raw = _parse_json_field(m.get("outcomePrices"))
            if len(prices_raw) != 2:
                continue
            try:
                yes_price = float(prices_raw[0])
                no_price = float(prices_raw[1])
            except (TypeError, ValueError):
                continue
            if not (0.0 < yes_price < 1.0 and 0.0 < no_price < 1.0):
                continue

            # Minimum volume filter.
            try:
                volume = float(m.get("volume") or 0.0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < MIN_VOLUME:
                continue

            # Skip sports markets.
            if _is_sports(m.get("events")):
                continue

            condition_id = str(m.get("conditionId") or "").strip()
            if not condition_id:
                continue

            question = str(m.get("question") or "").strip()
            if not question:
                continue

            markets.append(
                {
                    "condition_id": condition_id,
                    "question": question,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": volume,
                }
            )

        offset += PAGE_LIMIT
        if len(page_data) < PAGE_LIMIT:
            break

        # Polite pacing — 2 pages/second max.
        time.sleep(0.5)

    return markets


def run_snapshot(db_path: str | None = None) -> dict:
    """
    Main entry point: fetch binary markets and insert price snapshots.

    Returns a summary dict with keys: fetched, inserted, skipped, errors.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "sniper-binary-snapshot/1.0"})

    log.info("Fetching binary markets from Gamma API …")
    try:
        markets = fetch_binary_markets(session)
    except Exception as exc:
        log.error("Fatal error fetching binary markets: %s", exc, exc_info=True)
        return {"fetched": 0, "inserted": 0, "skipped": 0, "errors": 1}

    log.info("Fetched %d active binary markets", len(markets))

    conn = get_connection(db_path)
    ensure_poly_price_snapshots_table(conn)

    inserted = 0
    skipped = 0
    errors = 0

    for m in markets:
        cid = m["condition_id"]
        title = m["question"]
        try:
            insert_poly_price_snapshot(
                conn,
                condition_id=cid,
                title=title,
                outcome="yes",
                price=m["yes_price"],
                market_type="binary",
            )
            insert_poly_price_snapshot(
                conn,
                condition_id=cid,
                title=title,
                outcome="no",
                price=m["no_price"],
                market_type="binary",
            )
            inserted += 2
        except Exception as exc:
            log.warning("Failed to insert snapshot for %s: %s", cid, exc)
            errors += 1
            skipped += 1
            continue

    conn.commit()
    conn.close()
    session.close()  # I30: prevent connection pool leak

    log.info(
        "Binary snapshot complete: %d markets → %d rows inserted, %d errors",
        len(markets),
        inserted,
        errors,
    )
    return {
        "fetched": len(markets),
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Snapshot binary Polymarket markets")
    parser.add_argument("--db", default=None, help="Path to SQLite DB (overrides DB_PATH env)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, do not write to DB")
    args = parser.parse_args()

    if args.dry_run:
        session = requests.Session()
        session.headers.update({"User-Agent": "sniper-binary-snapshot/1.0"})
        markets = fetch_binary_markets(session)
        print(f"Dry run: {len(markets)} binary markets found")
        for m in markets[:10]:
            print(
                f"  {m['condition_id'][:12]}…  yes={m['yes_price']:.3f}  "
                f"vol={m['volume']:.0f}  {m['question'][:70]}"
            )
        if len(markets) > 10:
            print(f"  … and {len(markets) - 10} more")
    else:
        summary = run_snapshot(db_path=args.db)
        print(
            f"Done: {summary['fetched']} fetched, {summary['inserted']} rows inserted, "
            f"{summary['errors']} errors"
        )
