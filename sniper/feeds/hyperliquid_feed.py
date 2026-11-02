#!/usr/bin/env python3
"""Hyperliquid perpetual futures data feed.

Polls Hyperliquid REST API every 5 minutes for:
- Mark prices, funding rates, open interest for BTC, ETH, SOL, HYPE
- Stores snapshots in SQLite for trend analysis
- Writes summary to Supabase brain (markets/hyperliquid_state)

No SDK dependency -- uses raw requests to https://api.hyperliquid.xyz/info

Usage:
    python3 feeds/hyperliquid_feed.py              # one-shot fetch
    python3 feeds/hyperliquid_feed.py --quiet       # cron-friendly
    python3 feeds/hyperliquid_feed.py --all-assets  # track all 100+ assets
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Project root bootstrapping
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Load .env
_env_path = os.path.join(ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()

logger = logging.getLogger("hyperliquid_feed")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "sniper_tracker.db"))
HL_API_URL = "https://api.hyperliquid.xyz/info"

# Default tracked assets (top perps by OI)
TRACKED_ASSETS = {"BTC", "ETH", "SOL", "HYPE"}


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def fetch_meta_and_asset_ctxs() -> tuple[list[dict], list[dict]]:
    """POST to Hyperliquid /info with type=metaAndAssetCtxs.

    Returns (universe_list, asset_ctx_list) where:
    - universe_list: [{"name": "BTC", "szDecimals": 5, ...}, ...]
    - asset_ctx_list: [{"funding": "0.0001", "openInterest": "1234.5",
                         "markPx": "68500.0", ...}, ...]

    Raises on HTTP error or malformed response.
    """
    import requests

    resp = requests.post(
        HL_API_URL,
        json={"type": "metaAndAssetCtxs"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or len(data) < 2:
        raise ValueError(f"Unexpected response shape: {type(data)}, len={len(data) if isinstance(data, list) else 'N/A'}")

    universe = data[0].get("universe", []) if isinstance(data[0], dict) else []
    asset_ctxs = data[1] if isinstance(data[1], list) else []

    if len(universe) != len(asset_ctxs):
        logger.warning(
            "Universe/asset_ctxs length mismatch: %d vs %d",
            len(universe), len(asset_ctxs),
        )

    return universe, asset_ctxs


def parse_snapshots(
    universe: list[dict],
    asset_ctxs: list[dict],
    tracked: set[str] | None = None,
) -> list[dict]:
    """Parse API response into snapshot dicts.

    Each snapshot has: asset, mark_price, funding_rate, open_interest (USD),
    volume_24h.

    If tracked is None, returns all assets. Otherwise filters to the set.
    """
    snapshots = []
    for meta, ctx in zip(universe, asset_ctxs):
        name = meta.get("name", "")
        if tracked and name not in tracked:
            continue

        try:
            mark_price = float(ctx.get("markPx", 0))
            funding_rate = float(ctx.get("funding", 0))
            oi_raw = float(ctx.get("openInterest", 0))
            volume_24h = float(ctx.get("dayNtlVlm", 0))
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse %s: %s", name, e)
            continue

        if mark_price <= 0:
            logger.debug("Skipping %s: mark_price=%.4f", name, mark_price)
            continue

        # OI from API is in contracts/coins -- convert to USD
        oi_usd = oi_raw * mark_price

        snapshots.append({
            "asset": name,
            "mark_price": mark_price,
            "funding_rate": funding_rate,
            "open_interest": oi_usd,
            "volume_24h": volume_24h,
        })

    return snapshots


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _get_db_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def store_snapshots(conn, snapshots: list[dict]) -> int:
    """Insert snapshots into hyperliquid_snapshots. Returns count stored."""
    from sniper.persistence.db_snapshots import ensure_hyperliquid_snapshots_table, insert_hyperliquid_snapshot

    ensure_hyperliquid_snapshots_table(conn)
    stored = 0
    for snap in snapshots:
        row_id = insert_hyperliquid_snapshot(
            conn,
            asset=snap["asset"],
            mark_price=snap["mark_price"],
            funding_rate=snap["funding_rate"],
            open_interest=snap["open_interest"],
            volume_24h=snap["volume_24h"],
        )
        if row_id is not None:
            stored += 1
    return stored


# ---------------------------------------------------------------------------
# Brain write (Supabase)
# ---------------------------------------------------------------------------

def write_brain_summary(snapshots: list[dict]) -> bool:
    """Write summary to Supabase knowledge/markets/hyperliquid_state."""
    try:
        from bot.supabase_helper import upsert_memory

        summary: Dict[str, Any] = {}
        for snap in snapshots:
            asset_lower = snap["asset"].lower()
            summary[asset_lower] = {
                "price": round(snap["mark_price"], 2),
                "funding_8h": round(snap["funding_rate"] * 100, 4),  # as percentage
                "oi_usd": round(snap["open_interest"]),
                "volume_24h": round(snap["volume_24h"]),
            }

        now = datetime.now(timezone.utc).isoformat()
        content = json.dumps({"timestamp": now, "assets": summary}, indent=2)

        upsert_memory(
            type="knowledge",
            name="markets/hyperliquid_state",
            description=f"Hyperliquid perp snapshot at {now[:16]}",
            content=content,
            source="vps",
        )
        return True
    except Exception as e:
        logger.warning("Failed to write brain summary: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(*, quiet: bool = False, all_assets: bool = False) -> dict:
    """Execute one fetch cycle.

    Returns summary dict with: assets_fetched, rows_stored, brain_written.
    """
    result = {"assets_fetched": 0, "rows_stored": 0, "brain_written": False}

    tracked = None if all_assets else TRACKED_ASSETS

    # 1. Fetch from API
    universe, asset_ctxs = fetch_meta_and_asset_ctxs()
    snapshots = parse_snapshots(universe, asset_ctxs, tracked=tracked)
    result["assets_fetched"] = len(snapshots)

    if not snapshots:
        logger.warning("No snapshots parsed from API response")
        return result

    # 2. Store in SQLite
    conn = _get_db_conn()
    try:
        stored = store_snapshots(conn, snapshots)
        result["rows_stored"] = stored
    finally:
        conn.close()

    # 3. Write brain summary
    result["brain_written"] = write_brain_summary(snapshots)

    # 4. Print summary
    if not quiet:
        print(f"Hyperliquid feed: {len(snapshots)} assets fetched, {stored} rows stored")
        for snap in snapshots:
            funding_pct = snap["funding_rate"] * 100
            print(
                f"  {snap['asset']:>5s}: ${snap['mark_price']:>10,.2f}  "
                f"funding={funding_pct:+.4f}%  "
                f"OI=${snap['open_interest']:>13,.0f}  "
                f"vol24h=${snap['volume_24h']:>13,.0f}"
            )

    logger.info(
        "Hyperliquid feed: %d assets, %d stored, brain=%s",
        len(snapshots), stored, result["brain_written"],
    )
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid perpetual futures data feed"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout output")
    parser.add_argument("--all-assets", action="store_true", help="Track all assets (not just BTC/ETH/SOL/HYPE)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        run(quiet=args.quiet, all_assets=args.all_assets)
    except Exception:
        logger.exception("Hyperliquid feed failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
