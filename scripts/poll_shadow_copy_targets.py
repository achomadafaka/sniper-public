#!/usr/bin/env python3
"""Poll Polymarket Data API for target-wallet activity and feed the
shadow_copy_pending_signals queue.

This is the data-source half of shadow_copy_v1. The plugin only consumes
from the queue — this script writes to it.

Per CLAUDE.md no-live-API doctrine, this script is OPT-IN only:
- requires --enable-live-api flag (refuses to run otherwise)
- requires explicit --wallets list (no auto-discovery)
- caps total API calls per run via --max-wallets

Usage:
    python3 scripts/poll_shadow_copy_targets.py \\
        --db sniper_tracker.db \\
        --wallets 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee \\
        --since-minutes 30 \\
        --enable-live-api

When --enable-live-api is omitted, runs in DRY mode: shows what would be
polled but makes no API calls. Useful for sanity-checking config.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.strategies.shadow_copy_v1 import (  # noqa: E402
    ensure_shadow_copy_pending_signals_table,
    insert_pending_signal,
)

logger = logging.getLogger("poll_shadow_copy_targets")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _fetch_wallet_activity(
    wallet: str, since_ts: int
) -> list[dict[str, Any]]:
    """Fetch wallet activity from Polymarket Data API.

    Returns rows with at least:
    - conditionId, asset, outcome, side (BUY/SELL), price, size, timestamp,
      market_category (if known), title, end_date_iso (if known)

    Uses ``sniper.markets.polymarket_api.fetch_user_activity`` if available.
    """
    from sniper.markets.polymarket_api import fetch_user_activity
    # fetch_user_activity(session, wallet, ...) — session positional (None uses
    # the module client); the lower time bound param is `start_ts`, not `since_ts`.
    raw = fetch_user_activity(None, wallet, start_ts=since_ts or None)
    rows = list(raw or [])
    if since_ts:
        rows = [r for r in rows if int(r.get("timestamp") or 0) >= int(since_ts)]
    return rows


def _row_to_pending(
    *,
    wallet: str,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    """Normalize one activity row into pending-signal columns. Returns None for
    rows we don't insert (non-TRADE, missing required fields)."""
    if str(row.get("type") or row.get("activity_type") or "").upper() != "TRADE":
        return None
    side = str(row.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    if not row.get("conditionId") or not row.get("asset"):
        return None
    return {
        "wallet": wallet,
        "condition_id": str(row.get("conditionId") or ""),
        "asset_id": str(row.get("asset") or ""),
        "outcome": str(row.get("outcome") or "").upper(),
        "side": side,
        "price": float(row.get("price") or 0.0),
        "size_usdc": float(row.get("size") or 0.0) * float(row.get("price") or 0.0),
        "market_category": str(row.get("market_category") or row.get("category") or "data_unavailable"),
        "market_type": str(row.get("market_type") or "binary"),
        "outcome_role": str(row.get("outcome_role") or "data_unavailable"),
        "title": str(row.get("title") or ""),
        "end_date_iso": (str(row.get("endDate") or row.get("end_date") or "") or None),
        "observed_ts": int(row.get("timestamp") or 0),
    }


def poll_once(
    conn: sqlite3.Connection,
    *,
    wallets: list[str],
    since_minutes: int,
    enable_live_api: bool,
    max_wallets: int,
) -> dict[str, Any]:
    """Run one poll cycle. Returns a summary dict."""
    ensure_shadow_copy_pending_signals_table(conn)
    since_ts = int(time.time()) - since_minutes * 60
    summary = {
        "wallets_requested": len(wallets),
        "wallets_polled": 0,
        "rows_fetched": 0,
        "rows_inserted": 0,
        "rows_skipped_non_trade": 0,
        "rows_skipped_dedup": 0,
        "errors": [],
    }

    capped = wallets[:max_wallets]
    if not enable_live_api:
        logger.info(
            "[DRY] would poll %d wallets (since=%ds): %s",
            len(capped),
            since_minutes * 60,
            capped,
        )
        return summary

    for wallet in capped:
        try:
            rows = _fetch_wallet_activity(wallet, since_ts)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{wallet}: {type(exc).__name__}: {exc}")
            logger.exception("fetch_user_activity failed for %s", wallet)
            continue
        summary["wallets_polled"] += 1
        summary["rows_fetched"] += len(rows)
        for row in rows:
            normalized = _row_to_pending(wallet=wallet, row=row)
            if not normalized:
                summary["rows_skipped_non_trade"] += 1
                continue
            inserted = insert_pending_signal(conn, **normalized)
            if inserted is None:
                summary["rows_skipped_dedup"] += 1
            else:
                summary["rows_inserted"] += 1

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll Polymarket for target-wallet trades and feed shadow_copy_pending_signals."
    )
    parser.add_argument("--db", required=True, type=Path, help="SQLite path")
    parser.add_argument(
        "--wallets", nargs="+", required=True,
        help="Whitelist of wallet addresses to poll.",
    )
    parser.add_argument(
        "--since-minutes", type=int, default=30,
        help="Fetch activity newer than N minutes (default 30).",
    )
    parser.add_argument(
        "--max-wallets", type=int, default=10,
        help="Hard cap on wallets per run (default 10).",
    )
    parser.add_argument(
        "--enable-live-api", action="store_true",
        help="REQUIRED to actually hit the Polymarket Data API. Without it, runs DRY.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.since_minutes <= 0:
        print("# error: --since-minutes must be positive", file=sys.stderr)
        return 2
    if not args.db.exists() and args.enable_live_api:
        print(f"# error: db not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        summary = poll_once(
            conn,
            wallets=[w.lower().strip() for w in args.wallets if w],
            since_minutes=args.since_minutes,
            enable_live_api=args.enable_live_api,
            max_wallets=args.max_wallets,
        )
    finally:
        conn.close()

    logger.info("summary=%s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
