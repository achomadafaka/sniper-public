#!/usr/bin/env python3
"""Backfill `market_tags` from live Polymarket gamma /events.

Stage 1 of the wallet-taxonomy migration
(docs/superpowers/specs/2026-04-23-wallet-taxonomy-migration.md).

Usage:
    python3 scripts/backfill_market_tags.py              # default DB
    python3 scripts/backfill_market_tags.py --db-path /path/to/db
    python3 scripts/backfill_market_tags.py --max-pages 50  # dev/test

Idempotent. Uses `INSERT OR IGNORE` so re-running won't duplicate rows
or overwrite prior ingestions. Prints summary stats on exit:
    - number of conditions touched
    - number of (condition, tag) pairs inserted
    - number of `market_metadata` conditions that still lack any tags
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_db_path, get_connection
from sniper.markets.polymarket_api import GAMMA_BASE_URL, _extract_raw_tags, _request_json, build_session

logger = logging.getLogger("backfill_market_tags")


@dataclass(frozen=True)
class BackfillStats:
    pages_seen: int = 0
    malformed_pages: int = 0
    failed_pages: int = 0
    conditions_seen: int = 0
    inserted_rows: int = 0
    page_cap_hit: bool = False


@dataclass(frozen=True)
class GammaEventPage:
    events: object
    params: dict
    page_cap_hit: bool = False


def ensure_market_tags_schema(conn: sqlite3.Connection) -> None:
    """Create only the market_tags table/indexes needed by this backfill."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_tags (
            condition_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            tag_label TEXT,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_tags_tag ON market_tags(tag)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_tags_condition_id ON market_tags(condition_id)"
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()[0]
        > 0
    )


def extract_tags_by_condition_from_events(events: object) -> dict[str, list[tuple[str, str]]]:
    """Extract condition_id -> raw gamma tag pairs from one /events page."""
    if not isinstance(events, list):
        logger.warning("malformed gamma events page: expected list, got %s", type(events).__name__)
        return {}
    if not events:
        logger.warning("empty gamma events page")
        return {}

    out: dict[str, list[tuple[str, str]]] = {}
    seen_by_cid: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, dict):
            logger.warning("malformed gamma event skipped: expected object, got %s", type(event).__name__)
            continue
        raw_tags = _extract_raw_tags(event)
        if not raw_tags:
            continue
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            logger.warning("malformed gamma event markets skipped: expected list")
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            cid = str(market.get("conditionId") or market.get("condition_id") or "").strip().lower()
            if not cid:
                continue
            pairs = out.setdefault(cid, [])
            seen = seen_by_cid.setdefault(cid, set())
            for slug, label in raw_tags:
                slug_norm = str(slug or "").strip().lower()
                if not slug_norm or slug_norm in seen:
                    continue
                pairs.append((slug_norm, label or ""))
                seen.add(slug_norm)
    return out


def iter_gamma_event_pages(
    session,
    *,
    max_pages: int = 200,
    include_closed: bool = True,
    limit: int = 100,
) -> Iterator[GammaEventPage]:
    """Yield raw Gamma /events pages in bulk pages.

    `limit` defaults to 100 because the Gamma /events endpoint silently caps
    each page at 100 events regardless of a larger requested limit (verified
    live 2026-05-28). Requesting 500 returns 100, which trips the
    `len(events) < limit` short-page break on page 1 AND skips 400 events per
    offset step — the bug that made this backfill insert almost nothing.
    Match the request to the real cap so offset math and the break condition
    are both correct.

    Events are ordered by startDate descending (newest first) so the most
    recent markets — the ones missing from a stale tag table — are covered in
    the earliest pages rather than requiring a deep crawl of all history.
    """
    order = {"order": "startDate", "ascending": "false"}
    queries = [{"active": True, "closed": False, **order}]
    if include_closed:
        queries.append({"active": False, "closed": True, **order})

    for base in queries:
        for page in range(max_pages):
            params = dict(base)
            params.update({"limit": limit, "offset": page * limit})
            try:
                events = _request_json(session, f"{GAMMA_BASE_URL}/events", params=params)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status != 422:
                    raise  # a real error (404/4xx) — do not mask as a clean cap
                # Gamma rejects offset > 10000 with HTTP 422. Hitting that wall
                # mid-walk means there ARE more events we cannot reach — a
                # truncation, not a clean finish. Flag it so completion is never
                # falsely reported, then stop this query pass.
                logger.warning(
                    "gamma offset wall (HTTP 422) at offset=%d — crawl truncated, "
                    "results incomplete", page * limit,
                )
                yield GammaEventPage(events=[], params=params, page_cap_hit=True)
                break
            if not isinstance(events, list):
                yield GammaEventPage(events=events, params=params, page_cap_hit=False)
                break
            if not events:
                break
            page_cap_hit = page == max_pages - 1 and len(events) >= limit
            yield GammaEventPage(events=events, params=params, page_cap_hit=page_cap_hit)
            if len(events) < limit:
                break


def backfill_market_tags_from_event_pages(
    conn: sqlite3.Connection,
    event_pages: Iterable[object],
    *,
    page_cap_hit: bool = False,
) -> BackfillStats:
    """Backfill page-by-page so partial success is committed and resumable."""
    ensure_market_tags_schema(conn)
    pages_seen = malformed_pages = failed_pages = conditions_seen = inserted_rows = 0
    try:
        iterator = iter(event_pages)
        while True:
            try:
                page = next(iterator)
            except StopIteration:
                break
            except Exception as exc:
                failed_pages += 1
                # An aborted walk means the crawl did not complete — results are
                # incomplete. Report that via page_cap_hit so a truncated run is
                # never mistaken for a clean finish.
                page_cap_hit = True
                logger.warning("gamma events page fetch failed after partial backfill: %s", exc)
                break
            pages_seen += 1
            raw_page = page.events if isinstance(page, GammaEventPage) else page
            if isinstance(page, GammaEventPage) and page.page_cap_hit:
                page_cap_hit = True
            tags_by_condition = extract_tags_by_condition_from_events(raw_page)
            if not isinstance(raw_page, list):
                malformed_pages += 1
                continue
            conditions_seen += len(tags_by_condition)
            inserted_rows += insert_tags(conn, tags_by_condition)
    finally:
        conn.commit()
    return BackfillStats(
        pages_seen=pages_seen,
        malformed_pages=malformed_pages,
        failed_pages=failed_pages,
        conditions_seen=conditions_seen,
        inserted_rows=inserted_rows,
        page_cap_hit=page_cap_hit,
    )


def insert_tags(
    conn: sqlite3.Connection,
    tags_by_condition: dict[str, list[tuple[str, str]]],
) -> int:
    """Insert (condition_id, tag, tag_label) rows. Returns # new rows inserted."""
    ensure_market_tags_schema(conn)
    inserted = 0
    for condition_id, pairs in tags_by_condition.items():
        cid = str(condition_id or "").strip().lower()
        if not cid:
            continue
        for slug, label in pairs:
            slug_norm = str(slug or "").strip().lower()
            if not slug_norm:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO market_tags (condition_id, tag, tag_label) VALUES (?, ?, ?)",
                (cid, slug_norm, label or None),
            )
            if cur.rowcount:
                inserted += 1
    conn.commit()
    return inserted


def coverage_gap(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (metadata_count, untagged_count).

    untagged = markets in `market_metadata` with no row in `market_tags`.
    """
    ensure_market_tags_schema(conn)
    if not _table_exists(conn, "market_metadata"):
        return 0, 0
    total = conn.execute("SELECT COUNT(*) FROM market_metadata").fetchone()[0]
    untagged = conn.execute(
        """
        SELECT COUNT(*)
        FROM market_metadata m
        WHERE NOT EXISTS (
            SELECT 1 FROM market_tags t WHERE t.condition_id = m.condition_id
        )
        """
    ).fetchone()[0]
    return total, untagged


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill market_tags from gamma /events")
    parser.add_argument("--db-path", default=None, help="SQLite DB path (defaults to db.get_db_path())")
    parser.add_argument("--max-pages", type=int, default=200, help="Gamma pagination cap")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    db_path = args.db_path or get_db_path()
    logger.info("backfill_market_tags → db=%s max_pages=%d", db_path, args.max_pages)

    session = build_session()
    conn = get_connection(db_path)
    try:
        stats = backfill_market_tags_from_event_pages(
            conn,
            iter_gamma_event_pages(session, max_pages=args.max_pages),
            page_cap_hit=False,
        )
        logger.info(
            "processed %d pages; conditions_seen=%d inserted_rows=%d malformed_pages=%d failed_pages=%d",
            stats.pages_seen,
            stats.conditions_seen,
            stats.inserted_rows,
            stats.malformed_pages,
            stats.failed_pages,
        )
        if stats.conditions_seen == 0:
            logger.warning("no tags returned from gamma — check network / gamma response")
            return 1
        if stats.page_cap_hit:
            logger.warning(
                "gamma pagination did not prove completion (max_pages=%d cap, "
                "offset>10000 wall, or fetch error) — results may be incomplete",
                args.max_pages,
            )
        total, untagged = coverage_gap(conn)
        logger.info(
            "inserted %d new (condition, tag) rows; market_metadata total=%d, untagged=%d (%.1f%%)",
            stats.inserted_rows, total, untagged, (untagged / total * 100) if total else 0.0,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
