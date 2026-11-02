#!/usr/bin/env python3
"""Verify REDEEM timestamps post-date market resolution.

M5 closure check: if a REDEEM can appear before the corresponding market
resolution, flip-rate logic that treats redemptions as post-resolution exits
would be wrong. This script is read-only: it fetches wallet redemptions from
Goldsky and compares them to local `market_outcomes.resolved_at`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.wallet_protocol.subgraph_reader import (
    fetch_order_filled_events,
    fetch_redemptions,
)


def _condition_id(row: dict[str, Any]) -> str:
    return str(row.get("condition") or row.get("condition_id") or row.get("conditionId") or "").strip()


def _redeem_ts(row: dict[str, Any]) -> int:
    return int(row.get("timestamp") or row.get("redeem_ts") or 0)


def _tx_hash_from_redeem(row: dict[str, Any]) -> str:
    explicit = row.get("transactionHash") or row.get("transaction_hash")
    if explicit:
        return str(explicit).lower()
    row_id = str(row.get("id") or "")
    return row_id.split("_", 1)[0].lower() if row_id.startswith("0x") else ""


def _trade_tx_hashes(rows: list[dict[str, Any]] | None) -> set[str]:
    hashes: set[str] = set()
    for row in rows or []:
        tx_hash = row.get("transactionHash") or row.get("transaction_hash")
        if tx_hash:
            hashes.add(str(tx_hash).lower())
    return hashes


def _resolution_ts(conn: sqlite3.Connection, condition_id: str) -> int | None:
    try:
        row = conn.execute(
            """
            SELECT MAX(resolved_at) AS resolved_at
            FROM market_outcomes
            WHERE condition_id = ?
              AND resolved_at > 0
            """,
            (condition_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if row is None:
        return None
    value = row["resolved_at"] if isinstance(row, sqlite3.Row) else row[0]
    return int(value) if value is not None else None


def evaluate_redeems(
    conn: sqlite3.Connection,
    wallet: str,
    redemptions: list[dict[str, Any]],
    *,
    trade_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return M5 verification summary for pre-fetched redemption rows."""
    matched = 0
    unmatched = 0
    pre_resolution: list[dict[str, Any]] = []
    trade_hashes = _trade_tx_hashes(trade_rows)
    overlap_hashes: list[str] = []
    for redeem in redemptions:
        tx_hash = _tx_hash_from_redeem(redeem)
        if tx_hash and tx_hash in trade_hashes:
            overlap_hashes.append(tx_hash)
        condition_id = _condition_id(redeem)
        if not condition_id:
            unmatched += 1
            continue
        resolved_at = _resolution_ts(conn, condition_id)
        if resolved_at is None:
            unmatched += 1
            continue
        matched += 1
        redeem_ts = _redeem_ts(redeem)
        if redeem_ts < resolved_at:
            pre_resolution.append(
                {
                    "redeem_id": redeem.get("id"),
                    "condition_id": condition_id,
                    "redeem_ts": redeem_ts,
                    "resolved_at": resolved_at,
                }
            )

    return {
        "wallet": wallet.lower(),
        "n_redeems": len(redemptions),
        "n_matched_to_market_outcomes": matched,
        "n_unmatched_conditions": unmatched,
        "n_pre_resolution_redeems": len(pre_resolution),
        "pre_resolution_examples": pre_resolution[:20],
        "resolution_reference_status": (
            "inconsistent" if pre_resolution else "pass"
        ),
        "n_redeem_trade_tx_overlaps": len(overlap_hashes),
        "redeem_trade_tx_overlap_examples": sorted(set(overlap_hashes))[:20],
        "status": "fail" if overlap_hashes else "pass",
    }


def _wallets_from_args(args: argparse.Namespace) -> list[str]:
    wallets: list[str] = []
    wallets.extend(args.wallet or [])
    for path in args.wallet_file or []:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                value = line.strip()
                if value and not value.startswith("#"):
                    wallets.append(value)
    return wallets


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", action="append", default=[])
    parser.add_argument("--wallet-file", action="append", default=[])
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument(
        "--skip-trade-overlap-check",
        action="store_true",
        help="Skip Data API TRADE fetch; output only resolution-reference fields.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    wallets = _wallets_from_args(args)
    if not wallets:
        print("provide --wallet or --wallet-file", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    session = requests.Session()
    try:
        results = []
        for wallet in wallets:
            redemptions = fetch_redemptions(
                wallet.lower(),
                session=session,
                max_rows=args.max_rows,
            )
            trade_rows = None
            if not args.skip_trade_overlap_check:
                trade_rows = fetch_order_filled_events(
                    wallet.lower(),
                    side="both",
                    session=session,
                    max_rows=args.max_rows,
                )
            results.append(
                evaluate_redeems(
                    conn,
                    wallet,
                    redemptions,
                    trade_rows=trade_rows,
                )
            )
    finally:
        conn.close()

    payload: Any = results[0] if len(results) == 1 else results
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        for result in results:
            print(
                f"{result['wallet']} status={result['status']} "
                f"redeems={result['n_redeems']} matched={result['n_matched_to_market_outcomes']} "
                f"pre_resolution={result['n_pre_resolution_redeems']} "
                f"trade_tx_overlaps={result['n_redeem_trade_tx_overlaps']}"
            )
    return 1 if any(r["status"] == "fail" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
