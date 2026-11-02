#!/usr/bin/env python3
"""Poll target-wallet activity into source-position observations.

Dry-run by default. Requires --enable-live-api to call Polymarket.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    ensure_source_position_copy_tables,
    insert_source_observation,
)

logger = logging.getLogger("poll_source_position_copy_targets")


def _fetch_wallet_activity(wallet: str, since_ts: int) -> list[dict[str, Any]]:
    from sniper.markets.polymarket_api import fetch_user_activity

    rows = list(fetch_user_activity(None, wallet, start_ts=since_ts or None) or [])
    if since_ts:
        rows = [row for row in rows if int(row.get("timestamp") or 0) >= int(since_ts)]
    return rows


def _activity_key(wallet: str, row: dict[str, Any]) -> str:
    tx_hash = row.get("transactionHash") or row.get("transaction_hash") or row.get("txHash")
    log_index = row.get("logIndex")
    if log_index is None:
        log_index = row.get("log_index")
    if tx_hash:
        return f"{tx_hash}:{log_index if log_index is not None else 0}"
    return ":".join(
        [
            str(wallet).lower().strip(),
            str(row.get("conditionId") or row.get("condition_id") or ""),
            str(row.get("asset") or row.get("asset_id") or ""),
            str(row.get("side") or "").upper(),
            str(int(row.get("timestamp") or 0)),
            str(row.get("price") or 0),
            str(row.get("size") or 0),
        ]
    )


def _extract_tags(row: dict[str, Any]) -> list[str]:
    raw = row.get("tags") or row.get("platform_tags") or []
    if isinstance(raw, str):
        raw = [raw]
    return sorted({str(tag).lower().strip() for tag in raw if str(tag).strip()})


def _row_to_source_observation(*, wallet: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if str(row.get("type") or row.get("activity_type") or "").upper() != "TRADE":
        return None
    side = str(row.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return None
    condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
    asset_id = str(row.get("asset") or row.get("asset_id") or "")
    if not condition_id or not asset_id:
        return None
    price = float(row.get("price") or 0.0)
    size = float(row.get("size") or 0.0)
    normalized_wallet = str(wallet).lower().strip()
    return {
        "activity_key": _activity_key(normalized_wallet, row),
        "wallet": normalized_wallet,
        "condition_id": condition_id,
        "asset_id": asset_id,
        "outcome": str(row.get("outcome") or "").upper(),
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": price * size,
        "observed_ts": int(row.get("timestamp") or 0),
        "title": str(row.get("title") or ""),
        "event_slug": str(row.get("eventSlug") or row.get("event_slug") or row.get("slug") or ""),
        "event_id": str(row.get("eventId") or row.get("event_id") or ""),
        "tags": _extract_tags(row),
        "source_endpoint": "data-api-user-activity",
        "raw_json": dict(row),
    }


def poll_once(
    *,
    db_path: Path,
    wallets: list[str],
    since_minutes: int,
    enable_live_api: bool,
    max_wallets: int,
    fetch_wallet_activity: Callable[[str, int], list[dict[str, Any]]] = _fetch_wallet_activity,
) -> dict[str, Any]:
    since_ts = int(time.time()) - int(since_minutes) * 60
    capped = [str(wallet).lower().strip() for wallet in wallets if str(wallet).strip()][:max_wallets]
    summary: dict[str, Any] = {
        "dry_run": not enable_live_api,
        "wallets_requested": len(wallets),
        "wallets_polled": 0,
        "rows_fetched": 0,
        "rows_inserted": 0,
        "rows_skipped_non_trade": 0,
        "rows_skipped_dedup": 0,
        "errors": [],
    }
    if not enable_live_api:
        logger.info("[DRY] would poll %d wallets since %s", len(capped), since_ts)
        return summary

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_source_position_copy_tables(conn)
    try:
        for wallet in capped:
            try:
                rows = fetch_wallet_activity(wallet, since_ts)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{wallet}: {type(exc).__name__}: {exc}")
                continue
            summary["wallets_polled"] += 1
            summary["rows_fetched"] += len(rows)
            for row in rows:
                normalized = _row_to_source_observation(wallet=wallet, row=row)
                if normalized is None:
                    summary["rows_skipped_non_trade"] += 1
                    continue
                inserted = insert_source_observation(conn, **normalized)
                if inserted is None:
                    summary["rows_skipped_dedup"] += 1
                else:
                    summary["rows_inserted"] += 1
    finally:
        conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll source wallet activity into source-position observations."
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--wallets", nargs="+", required=True)
    parser.add_argument("--since-minutes", type=int, default=30)
    parser.add_argument("--max-wallets", type=int, default=10)
    parser.add_argument("--enable-live-api", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if args.since_minutes <= 0:
        print("# error: --since-minutes must be positive", file=sys.stderr)
        return 2
    if args.enable_live_api and not args.db.exists():
        print(f"# error: db not found: {args.db}", file=sys.stderr)
        return 2

    summary = poll_once(
        db_path=args.db,
        wallets=args.wallets,
        since_minutes=args.since_minutes,
        enable_live_api=args.enable_live_api,
        max_wallets=args.max_wallets,
    )
    logger.info("summary=%s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
