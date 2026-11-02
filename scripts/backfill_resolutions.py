#!/usr/bin/env python3
"""Backfill market_resolved events for markets that resolved during collection.

The meta sync's closed-market fetch was broken (returned ancient junk from
the Gamma API). This script:
  1. Scans all raw JSONL files to find market_ids with WS data (trades/quotes)
  2. Fetches resolved markets from the Gamma API for the collection window
  3. Cross-references — any tracked market that has since resolved gets a
     market_metadata + market_resolved event pair injected
  4. Writes injected events to a new .jsonl.gz file in the raw directory

Usage:
  python3 scripts/backfill_resolutions.py --raw-dir ~/sniper/raw --dry-run
  python3 scripts/backfill_resolutions.py --raw-dir ~/sniper/raw
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from sniper.sim.collector.meta import parse_gamma_market, build_metadata_events, _has_clear_winner
from sniper.sim.schema import capture_ts_now
from sniper.sim.serialize import event_to_jsonl

LOG = logging.getLogger(__name__)


def scan_tracked_markets(raw_dir: str, venue: str = "polymarket") -> set[str]:
    """Scan all raw JSONL files and return the set of venue_market_ids with WS data."""
    venue_dir = os.path.join(raw_dir, venue)
    if not os.path.isdir(venue_dir):
        LOG.error("No venue directory: %s", venue_dir)
        return set()

    market_ids: set[str] = set()
    file_count = 0
    for root, _dirs, files in os.walk(venue_dir):
        for fname in sorted(files):
            if not fname.endswith(".jsonl.gz"):
                continue
            fp = os.path.join(root, fname)
            file_count += 1
            try:
                with gzip.open(fp, "rt") as f:
                    for line in f:
                        try:
                            d = json.loads(line.strip())
                            # Only count WS data events (not metadata events from meta sync)
                            et = d.get("event_type", "")
                            if et in ("book_delta", "best_bba", "book_snapshot", "trade"):
                                vmid = d.get("venue_market_id", "")
                                if vmid:
                                    market_ids.add(vmid)
                        except (json.JSONDecodeError, KeyError):
                            continue
            except Exception as e:
                LOG.warning("Error reading %s: %s", fp, e)

    LOG.info("Scanned %d files, found %d unique markets with WS data", file_count, len(market_ids))
    return market_ids


def fetch_resolved_markets(
    gamma_url: str,
    end_date_min: str,
    end_date_max: str,
    max_pages: int = 10,
    page_size: int = 100,
) -> list[dict]:
    """Fetch resolved markets from Gamma API with valid outcome_prices."""
    all_markets = []
    for page in range(max_pages):
        params = {
            "closed": "true",
            "end_date_min": end_date_min,
            "end_date_max": end_date_max,
            "limit": str(page_size),
            "offset": str(page * page_size),
        }
        resp = requests.get(f"{gamma_url}/markets", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        all_markets.extend(data)
        if len(data) < page_size:
            break

    # Parse and filter for valid resolutions
    valid = []
    for raw in all_markets:
        m = parse_gamma_market(raw)
        if m is None:
            continue
        if not m.get("closed"):
            continue
        prices = m.get("outcome_prices", [])
        if not _has_clear_winner(prices):
            continue
        valid.append(m)

    LOG.info("Fetched %d raw closed markets, %d with clear winners", len(all_markets), len(valid))
    return valid


def main():
    parser = argparse.ArgumentParser(description="Backfill market_resolved events")
    parser.add_argument("--raw-dir", default=os.path.expanduser("~/sniper/raw"))
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--venue", default="polymarket")
    parser.add_argument("--lookback-days", type=int, default=14,
                        help="How far back to search for resolved markets")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report overlap without writing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Step 1: Scan raw data for tracked markets
    LOG.info("Step 1: Scanning raw data for tracked markets...")
    tracked = scan_tracked_markets(args.raw_dir, args.venue)
    if not tracked:
        LOG.error("No tracked markets found in raw data")
        return

    # Step 2: Fetch resolved markets from Gamma API
    now = datetime.now(timezone.utc)
    end_date_min = (now - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
    end_date_max = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    LOG.info("Step 2: Fetching resolved markets (end_date %s to %s)...", end_date_min, end_date_max)
    resolved = fetch_resolved_markets(args.gamma_url, end_date_min, end_date_max)

    # Step 3: Cross-reference
    resolved_cids = {m["condition_id"] for m in resolved}
    overlap = tracked & resolved_cids
    LOG.info("Step 3: Cross-reference — %d tracked, %d resolved, %d overlap",
             len(tracked), len(resolved_cids), len(overlap))

    if not overlap:
        LOG.warning("No overlap between tracked WS markets and resolved markets.")
        LOG.info("Tracked markets have not resolved yet. Options:")
        LOG.info("  1. Wait for tracked markets to resolve (end dates are weeks/months out)")
        LOG.info("  2. Add soon-ending markets to the universe filter")
        LOG.info("  3. Use --lookback-days to expand the search window")

        # Still useful: report which resolved markets are closest to tracked ones
        LOG.info("\nResolved markets available (top 10 by recency):")
        by_end = sorted(resolved, key=lambda m: m.get("end_date", ""), reverse=True)
        for m in by_end[:10]:
            q = m.get("question", "")[:60]
            end = m.get("end_date", "")[:10]
            cid = m["condition_id"][:16]
            LOG.info("  %s... end=%s q=%s", cid, end, q)
        return

    # Step 4: Build resolution events for overlapping markets
    overlap_markets = [m for m in resolved if m["condition_id"] in overlap]
    capture_ts = capture_ts_now()
    all_events = build_metadata_events(
        overlap_markets,
        ingest_seq_start=0,  # backfill events get seq 0; compactor dedupes by manifest
        capture_ts=capture_ts,
    )

    LOG.info("Step 4: %d events to inject for %d markets", len(all_events), len(overlap_markets))
    for m in overlap_markets:
        LOG.info("  %s — %s", m["condition_id"][:16], m.get("question", "")[:60])

    if args.dry_run:
        LOG.info("DRY RUN — not writing anything")
        return

    # Step 5: Write to a backfill JSONL file
    today = now.strftime("%Y-%m-%d")
    out_dir = os.path.join(args.raw_dir, args.venue, today)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "backfill_resolutions.jsonl.gz")

    with gzip.open(out_file, "wt") as f:
        for event in all_events:
            f.write(event_to_jsonl(event) + "\n")

    LOG.info("Step 5: Wrote %d events to %s", len(all_events), out_file)
    LOG.info("Next: run the compactor to process this file into Parquet")


if __name__ == "__main__":
    main()
