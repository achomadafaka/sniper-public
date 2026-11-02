"""Becker candidate feed (batch-runner Option A) — a broad, NON-survivorship wallet
population enumerated by OUTCOME-NEUTRAL opened activity as-of T.

The eligibility floor counts positions OPENED, never resolved/closed (S1: "resolved" is a
soft winner-filter). An opened position = a wallet BUYING a non-USDC outcome token with
USDC. From the Becker trades parquet (USDC asset_id = '0'):
  - maker_asset_id == '0'  -> the maker gave USDC and bought the taker_asset_id token
  - taker_asset_id == '0'  -> the taker gave USDC and bought the maker_asset_id token
The buyer is the opener; opened_count = COUNT(DISTINCT bought token_id) per wallet. No PnL,
no rank, no amount threshold — buying requires no future outcome.

Block→epoch is exact via the dense `blocks` table (block_number -> ISO timestamp). `as_of`
is resolved to the max block whose timestamp ≤ T; trades with block_number ≤ that are
pre-T. (Block timing is good enough for a day-level as-of batch, NOT sub-minute claims.)

Wallets are ordered by `hash(wallet)` — deterministic and unbiased by address prefix or
activity — then capped at `limit`. Globs are injectable so the SQL is unit-tested on small
synthetic parquet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Candidate:
    wallet: str
    opened: int          # outcome-neutral opened-position count as-of T


def _con(memory_limit: str | None, temp_dir: str | None):
    import duckdb
    con = duckdb.connect()
    if memory_limit:
        con.execute(f"PRAGMA memory_limit='{memory_limit}'")
    if temp_dir:
        con.execute(f"PRAGMA temp_directory='{temp_dir}'")
    return con


def _iso(as_of_ts: int) -> str:
    return datetime.fromtimestamp(int(as_of_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def block_for_epoch(blocks_glob: str, as_of_ts: int, *, memory_limit: str | None = None,
                    temp_dir: str | None = None) -> int | None:
    """Max block_number whose timestamp ≤ as_of_ts (None if none qualify)."""
    con = _con(memory_limit, temp_dir)
    try:
        row = con.execute(
            "SELECT MAX(block_number) FROM read_parquet(?) WHERE timestamp <= ?",
            [blocks_glob, _iso(as_of_ts)],
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row and row[0] is not None else None


def wallet_opened_counts(trades_glob: "str | list[str]", *, as_of_block: int, min_opened: int,
                         limit: int, memory_limit: str | None = "4GB",
                         temp_dir: str | None = None) -> list[Candidate]:
    """Wallets with ≥ min_opened distinct bought tokens as-of `as_of_block`, hash-ordered.

    `trades_glob` is a DuckDB read_parquet target — a glob pattern string
    (`.../trades/*.parquet`) for the full archive, or a list of file paths for a bounded
    subset. DuckDB prunes files after `as_of_block` via parquet row-group statistics."""
    con = _con(memory_limit, temp_dir)
    sql = """
        WITH buys AS (
            SELECT
                lower(CASE WHEN maker_asset_id = '0' THEN maker ELSE taker END) AS wallet,
                CASE WHEN maker_asset_id = '0' THEN taker_asset_id ELSE maker_asset_id END AS token_id
            FROM read_parquet(?)
            WHERE block_number <= ?
              AND ((maker_asset_id = '0') <> (taker_asset_id = '0'))   -- exactly one side USDC
        )
        SELECT wallet, COUNT(DISTINCT token_id) AS opened
        FROM buys
        GROUP BY wallet
        HAVING COUNT(DISTINCT token_id) >= ?
        ORDER BY hash(wallet)
        LIMIT ?
    """
    try:
        rows = con.execute(sql, [trades_glob, int(as_of_block), int(min_opened), int(limit)]).fetchall()
    finally:
        con.close()
    return [Candidate(wallet=str(w), opened=int(n)) for w, n in rows]


def becker_candidate_feed(as_of_ts: int, *, trades_glob: str, blocks_glob: str,
                          min_opened: int = 30, limit: int = 100,
                          memory_limit: str | None = "4GB", temp_dir: str | None = None
                          ) -> list[Candidate]:
    """Broad activity-based candidate feed as-of T. Returns hash-ordered Candidates."""
    block_t = block_for_epoch(blocks_glob, as_of_ts, memory_limit=memory_limit, temp_dir=temp_dir)
    if block_t is None:
        return []
    return wallet_opened_counts(trades_glob, as_of_block=block_t, min_opened=min_opened,
                                limit=limit, memory_limit=memory_limit, temp_dir=temp_dir)
