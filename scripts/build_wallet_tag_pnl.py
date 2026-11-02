#!/usr/bin/env python3
"""Build per-(wallet, tag) realized-PnL rows from followed_wallet_activity.

Spec: B1 tag_wallet_pnl (docs/superpowers/specs/tag-wallet-pnl-2026-04-24).

Usage:
    python3 scripts/build_wallet_tag_pnl.py --wallet 0xABC...
    python3 scripts/build_wallet_tag_pnl.py --all
    python3 scripts/build_wallet_tag_pnl.py --all --source user-analysis --limit 150
    python3 scripts/build_wallet_tag_pnl.py --all --source user-analysis --limit 150 --skip-existing
    python3 scripts/build_wallet_tag_pnl.py --all --source user-analysis --limit 150 --report-only
    python3 scripts/build_wallet_tag_pnl.py --wallet 0xABC --min-sample 5
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys

# Make repo root importable when invoked as a script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.analysis.tag_wallet_pnl import (  # noqa: E402
    compute_wallet_tag_pnl,
    ensure_wallet_tag_pnl_table,
    upsert_wallet_tag_pnl,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("build_wallet_tag_pnl")


DEFAULT_DB_PATH = os.getenv(
    "SNIPER_DB_PATH",
    os.path.join(_REPO_ROOT, "sniper_tracker.db"),
)
DEFAULT_MIN_SAMPLE = int(
    os.getenv("TAG_WALLET_PNL_MIN_SAMPLE", "3")
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _all_wallets(conn: sqlite3.Connection) -> list[str]:
    return _wallets_from_source(conn, "followed-wallet-activity")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone())


def _wallets_from_source(conn: sqlite3.Connection, source: str) -> list[str]:
    if source == "followed-wallet-activity":
        table = "followed_wallet_activity"
        order_by = "wallet"
    elif source == "user-analysis":
        table = "user_analysis"
        order_by = (
            "COALESCE(closed_sample_size, 0) DESC, "
            "COALESCE(position_count, 0) DESC, wallet"
        )
    elif source == "wallet-registry":
        table = "wallet_registry"
        order_by = "wallet"
    else:
        raise ValueError(f"unsupported wallet source: {source}")
    if not _table_exists(conn, table):
        return []
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "closed_sample_size" not in cols:
        order_by = "wallet"
    rows = conn.execute(
        f"SELECT DISTINCT wallet FROM {table} "
        f"WHERE wallet IS NOT NULL AND wallet != '' ORDER BY {order_by}"
    ).fetchall()
    return [r[0] for r in rows]


def _print_report(conn: sqlite3.Connection, wallets: list[str]) -> None:
    existing_wallets = 0
    qualifying_tags: list[tuple[str, int]] = []
    if _table_exists(conn, "wallet_tag_pnl"):
        existing_wallets = conn.execute(
            "SELECT COUNT(DISTINCT wallet) FROM wallet_tag_pnl"
        ).fetchone()[0]
        qualifying_tags = conn.execute(
            "SELECT tag, COUNT(*) AS peers "
            "FROM wallet_tag_pnl "
            "WHERE n_held_to_resolution >= 10 "
            "GROUP BY tag HAVING COUNT(*) >= 10 "
            "ORDER BY peers DESC, tag ASC"
        ).fetchall()
    logger.info("candidate_wallets=%d", len(wallets))
    logger.info("existing_wallet_tag_pnl_wallets=%d", existing_wallets)
    logger.info(
        "qualifying_tags=%s",
        ", ".join(f"{tag}:{peers}" for tag, peers in qualifying_tags[:25]) or "none",
    )


def _existing_wallets(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "wallet_tag_pnl"):
        return set()
    rows = conn.execute("SELECT DISTINCT wallet FROM wallet_tag_pnl").fetchall()
    return {str(r[0]).lower() for r in rows if r[0]}


def run_for_wallet(
    conn: sqlite3.Connection, wallet: str, min_sample: int
) -> dict:
    rows = compute_wallet_tag_pnl(conn, wallet, min_sample=min_sample)
    if rows:
        upsert_wallet_tag_pnl(conn, wallet, rows)
    return rows


class WalletTimeoutError(TimeoutError):
    pass


def _run_for_wallet_with_timeout(
    conn: sqlite3.Connection,
    wallet: str,
    min_sample: int,
    timeout_s: int,
) -> dict:
    if timeout_s <= 0:
        return run_for_wallet(conn, wallet, min_sample)

    def _timeout(_signum, _frame):
        raise WalletTimeoutError(f"wallet {wallet} exceeded {timeout_s}s")

    old_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(timeout_s)
    try:
        return run_for_wallet(conn, wallet, min_sample)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute wallet_tag_pnl rows for one wallet or all."
    )
    parser.add_argument(
        "--wallet", help="Specific wallet address to process", default=None
    )
    parser.add_argument(
        "--all", action="store_true", help="Process every wallet in activity table"
    )
    parser.add_argument(
        "--source",
        choices=("followed-wallet-activity", "user-analysis", "wallet-registry"),
        default="followed-wallet-activity",
        help="Wallet source used with --all",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum wallets to process or report when using --all",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print candidate and current peer-pool coverage without computing rows",
    )
    parser.add_argument(
        "--wallet-timeout-s",
        type=int,
        default=0,
        help="Maximum seconds to spend on one wallet before skipping it. 0 disables.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="With --all, skip wallets already present in wallet_tag_pnl before applying --limit",
    )
    parser.add_argument(
        "--min-sample", type=int, default=DEFAULT_MIN_SAMPLE,
        help=f"Min positions per tag (default {DEFAULT_MIN_SAMPLE})",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help="Path to sqlite DB",
    )
    args = parser.parse_args(argv)

    if not args.wallet and not args.all:
        parser.error("must pass --wallet <addr> or --all")

    conn = _connect(args.db)
    try:
        if args.wallet:
            wallets = [args.wallet]
        else:
            wallets = _wallets_from_source(conn, args.source)
            logger.info("processing %d wallets from %s", len(wallets), args.source)
        if args.skip_existing:
            existing = _existing_wallets(conn)
            wallets = [w for w in wallets if w.lower() not in existing]
            logger.info("after skip_existing wallets=%d", len(wallets))
        if args.limit is not None:
            wallets = wallets[: max(int(args.limit), 0)]

        if args.report_only:
            _print_report(conn, wallets)
            return 0

        ensure_wallet_tag_pnl_table(conn)

        total_rows = 0
        processed = 0
        skipped = 0
        for w in wallets:
            try:
                rows = _run_for_wallet_with_timeout(
                    conn, w, args.min_sample, int(args.wallet_timeout_s),
                )
                processed += 1
            except Exception:
                skipped += 1
                logger.exception("failed to compute tag pnl wallet=%s", w)
                continue
            if rows:
                total_rows += len(rows)
                if args.wallet or len(wallets) < 20:
                    for tag, tpr in sorted(rows.items()):
                        logger.info(
                            "wallet=%s tag=%s n=%d pnl=%.2f wr=%.3f "
                            "concentration=%.3f",
                            w, tag, tpr.n_positions, tpr.realized_pnl,
                            tpr.win_rate,
                            tpr.conviction_concentration,
                        )
        logger.info(
            "done — %d (wallet, tag) rows upserted across %d wallets",
            total_rows, processed,
        )
        if skipped:
            logger.info("skipped_wallets=%d", skipped)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
