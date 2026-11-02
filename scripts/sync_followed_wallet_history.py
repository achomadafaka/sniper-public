#!/usr/bin/env python3
"""Backfill immutable Polymarket activity for followed/research-scope wallets."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import (
    followed_wallet_research_scope_wallets,
    get_connection,
    get_followed_wallets,
    init_db,
    upsert_followed_wallet_activity,
)
from sniper.markets.polymarket_api import build_session, fetch_user_activity

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _wallets_for_scope(conn, scope: str) -> list[str]:
    if scope == "followed":
        return [str(row["wallet"] or "") for row in get_followed_wallets(status="active")]
    return followed_wallet_research_scope_wallets(conn)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", help="Sync only this wallet")
    parser.add_argument(
        "--scope",
        choices=("followed", "research"),
        default="research",
        help="Wallet universe when --wallet is omitted",
    )
    parser.add_argument("--limit", type=int, default=200, help="Rows per API page")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum pages per wallet")
    args = parser.parse_args()

    conn = get_connection()
    init_db(conn)
    session = build_session()

    if args.wallet:
        wallets = [args.wallet]
    else:
        wallets = _wallets_for_scope(conn, args.scope)
    wallets = [wallet for wallet in wallets if WALLET_RE.fullmatch(wallet)]

    total_rows = 0
    for wallet in wallets:
        rows = fetch_user_activity(session, wallet, limit=max(1, args.limit), max_pages=max(1, args.max_pages))
        saved = upsert_followed_wallet_activity(
            conn,
            wallet=wallet,
            activities=rows,
            source_endpoint="activity",
        )
        total_rows += saved
        print(f"{wallet}: fetched={len(rows)} saved={saved}")

    print(f"Completed wallet-history sync for {len(wallets)} wallet(s); processed {total_rows} activity rows.")


if __name__ == "__main__":
    main()
