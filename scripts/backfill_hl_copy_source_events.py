#!/usr/bin/env python3
"""Recover strictly provable legacy copy event times from free Hyperliquid fills.

Dry-run is the default. ``--apply`` creates a SQLite snapshot before guarded updates.
Receive, decision, book, and mirror timestamps are never changed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.hyperlive import source_backfill as SB  # noqa: E402
from sniper.markets import hyperliquid_api as HL  # noqa: E402


def _open(path: Path, *, apply: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = (
        sqlite3.connect(path)
        if apply
        else sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    )
    conn.row_factory = sqlite3.Row
    required = {
        "id", "target", "coin", "side", "detect_ts_ms", "target_szi",
        "is_cold_start", "source_event_ts_ms", "source_received_ts_ms",
        "source_latency_status", "source_event_tid",
    }
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hl_copy_book)")}
    if not required.issubset(columns):
        conn.close()
        raise RuntimeError("hl_copy_book source-capture schema is incomplete")
    return conn


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = db_path.with_name(f"{db_path.name}.pre-source-backfill-{stamp}.bak")
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return path


def eligible_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT id,target,coin,side,detect_ts_ms,target_szi,is_cold_start "
        "FROM hl_copy_book WHERE source_latency_status='historical_not_captured' "
        "AND source_event_ts_ms IS NULL AND source_received_ts_ms IS NULL "
        "ORDER BY lower(target),detect_ts_ms,id"
    )]


def run(conn: sqlite3.Connection, *, apply: bool, fetch, max_wallets: int | None = None) -> dict:
    grouped = defaultdict(list)
    for row in eligible_rows(conn):
        grouped[str(row["target"]).lower()].append(row)
    wallets = sorted(grouped)
    if max_wallets is not None:
        wallets = wallets[:max(0, int(max_wallets))]
    counts = Counter()
    fetched = 0
    ceilings = 0
    for wallet in wallets:
        rows = grouped[wallet]
        start_ms = min(int(row["detect_ts_ms"]) for row in rows) - SB.MAX_CAUSAL_WINDOW_MS
        end_ms = max(int(row["detect_ts_ms"]) for row in rows)
        try:
            history = SB.fetch_wallet_history(
                wallet, start_ms=start_ms, end_ms=end_ms, fetch=fetch,
            )
        except Exception as exc:  # provider failures stay explicit and do not abort later wallets
            counts["provider_error"] += len(rows)
            print(json.dumps({"wallet": wallet, "status": "provider_error", "error": str(exc)}))
            continue
        fills = history["fills"]
        fetched += len(fills)
        ceilings += int(history["ceiling_reached"])
        current_detect = None
        previous_distinct = None
        wallet_counts = Counter()
        for row in rows:
            detect = int(row["detect_ts_ms"])
            if current_detect is None or detect > current_detect:
                previous_distinct = current_detect
                current_detect = detect
            after = max(
                detect - SB.MAX_CAUSAL_WINDOW_MS,
                previous_distinct if previous_distinct is not None else detect - SB.MAX_CAUSAL_WINDOW_MS,
            )
            result = SB.reconstruct_entry_event(row, fills, after_ts_ms=after)
            status = str(result["status"])
            counts[status] += 1
            wallet_counts[status] += 1
            if apply and status == "matched":
                changed = IS.backfill_copy_source_event(
                    conn,
                    copy_id=int(row["id"]),
                    source_event_ts_ms=int(result["source_event_ts_ms"]),
                    tids=result["tids"],
                )
                counts["updated"] += int(changed)
                wallet_counts["updated"] += int(changed)
        if apply:
            conn.commit()
        print(json.dumps({
            "wallet": wallet,
            "fills": len(fills),
            "provider_ceiling": bool(history["ceiling_reached"]),
            "counts": dict(sorted(wallet_counts.items())),
        }, sort_keys=True))
    return {
        "mode": "apply" if apply else "dry_run",
        "wallets": len(wallets),
        "fills_fetched": fetched,
        "provider_ceiling_wallets": ceilings,
        "counts": dict(sorted(counts.items())),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=Path("data/hl_intel.sqlite"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-wallets", type=int)
    args = parser.parse_args(argv)
    conn = _open(args.db_path, apply=args.apply)
    backup = _backup(conn, args.db_path) if args.apply else None
    try:
        summary = run(
            conn, apply=args.apply, fetch=HL.fetch_user_fills_by_time,
            max_wallets=args.max_wallets,
        )
        if backup is not None:
            summary["backup"] = str(backup)
        print(json.dumps(summary, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
