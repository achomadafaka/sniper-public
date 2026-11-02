#!/usr/bin/env python3
"""Build per-(wallet, tag) fit scores from wallet_tag_pnl.

Spec: B2 tag-archetype-fit (spec/tag-archetype-fit-2026-04-24).

Usage:
    python3 scripts/build_wallet_tag_fit.py --all
    python3 scripts/build_wallet_tag_fit.py --wallet 0xABC...
    python3 scripts/build_wallet_tag_fit.py --tag sports
    python3 scripts/build_wallet_tag_fit.py --all --min-peers 5
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

# Make repo root importable when invoked as a script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.analysis.wallet_tag_fit import (  # noqa: E402
    compute_all_fits,
    ensure_wallet_tag_fit_table,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("build_wallet_tag_fit")

DEFAULT_DB_PATH = os.getenv(
    "SNIPER_DB_PATH",
    os.path.join(_REPO_ROOT, "sniper_tracker.db"),
)
DEFAULT_MIN_PEERS = int(os.getenv("TAG_WALLET_FIT_MIN_PEERS", "10"))


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute wallet_tag_fit rows (B2 tag-archetype-fit)."
    )
    parser.add_argument(
        "--wallet", help="Filter output to a specific wallet address", default=None
    )
    parser.add_argument(
        "--all", action="store_true", help="Compute fits for all wallets in wallet_tag_pnl"
    )
    parser.add_argument(
        "--tag", help="Filter output to a specific tag", default=None
    )
    parser.add_argument(
        "--min-peers", type=int, default=DEFAULT_MIN_PEERS,
        help=f"Minimum peer count to include a tag (default {DEFAULT_MIN_PEERS})",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="Path to sqlite DB",
    )
    args = parser.parse_args(argv)

    if not args.wallet and not args.all and not args.tag:
        parser.error("must pass --wallet <addr>, --tag <tag>, or --all")

    conn = _connect(args.db)
    try:
        ensure_wallet_tag_fit_table(conn)
        rows = list(compute_all_fits(conn, min_peers=args.min_peers))

        # Optional post-filter for --wallet / --tag (compute_all_fits handles all)
        if args.wallet:
            rows = [r for r in rows if r.wallet == args.wallet]
        if args.tag:
            rows = [r for r in rows if r.tag == args.tag]

        logger.info(
            "done — %d (wallet, tag) fit rows computed (min_peers=%d)",
            len(rows), args.min_peers,
        )
        for fr in sorted(rows, key=lambda r: (r.tag, r.fit_rank)):
            logger.info(
                "wallet=%s tag=%s fit_score=%.4f fit_rank=%d peer_count=%d flag=%s",
                fr.wallet, fr.tag, fr.fit_score, fr.fit_rank,
                fr.peer_count, fr.confidence_flag,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
