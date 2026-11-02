#!/usr/bin/env python3
"""Backfill the stink-bid backtest DuckDB snapshot.

The script is intentionally idempotent: each step upserts or skips rows already
present in the snapshot, so interrupted runs can be resumed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.backtest.schema import open_backtest_db
from data.polymarket_client import PolymarketClient
from data.sackmann import (
    AlignedMatch,
    SackmannMatch,
    load_all,
    normalize_name,
    player_sides_from_title,
    title_looks_like_doubles,
    title_player_candidate_sets,
)

LOG = logging.getLogger(__name__)

STEP_NAMES = ("gamma_events", "prices_history", "trades", "sackmann_load", "alignment")
NON_MATCH_MARKET_TYPES = {
    "tennis_first_set_winner",
    "tennis_first_set_totals",
    "tennis_match_totals",
    "tennis_set_handicap",
    "tennis_set_totals",
}


def _dt_to_ts(value) -> Optional[int]:
    """Convert DuckDB timestamp/string values to unix seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _dt_to_iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _window_for_event(start_dt, end_dt) -> tuple[int, int]:
    end_ts = _dt_to_ts(end_dt) or int(time.time())
    start_ts = _dt_to_ts(start_dt) or (end_ts - 7 * 86400)
    return (start_ts - 7 * 86400, end_ts + 86400)


def _market_title(event_title: str, market: dict) -> str:
    question = (market.get("question") or "").strip()
    event_sides = player_sides_from_title(event_title)
    if event_sides[0] and event_sides[1] and not title_looks_like_doubles(event_title):
        return event_title
    return question


def _is_h2h_moneyline_market(event_title: str, market: dict) -> bool:
    if not market.get("conditionId"):
        return False
    if (market.get("sportsMarketType") or "").strip() in NON_MATCH_MARKET_TYPES:
        return False
    question = (market.get("question") or "").strip()
    title = _market_title(event_title, market)
    side1, side2 = player_sides_from_title(title)
    if not side1 or not side2 or title_looks_like_doubles(title):
        return False
    if (market.get("sportsMarketType") or "").strip() == "moneyline":
        return True
    return question == event_title


def _select_h2h_market(event: dict) -> tuple[Optional[dict], str]:
    title = (event.get("title") or "").strip()
    candidates = [market for market in event.get("markets") or [] if _is_h2h_moneyline_market(title, market)]
    if not candidates:
        return (None, "")
    candidates.sort(
        key=lambda market: (
            (market.get("question") or "").strip() != title,
            (market.get("sportsMarketType") or "").strip() != "moneyline",
            str(market.get("groupItemThreshold") or "") != "0",
        )
    )
    market = candidates[0]
    return (market, _market_title(title, market))


def _extract_h2h_event(event: dict) -> Optional[dict]:
    tags = [tag.get("slug", "") if isinstance(tag, dict) else str(tag) for tag in (event.get("tags") or [])]
    if "tennis" not in tags:
        return None
    title = (event.get("title") or "").strip()
    if not re.search(r"\bvs?\.?\b", title, re.IGNORECASE):
        return None
    h2h, match_title = _select_h2h_market(event)
    if h2h is None:
        return None
    try:
        token_ids = json.loads(h2h.get("clobTokenIds", "[]"))
    except Exception:
        return None
    if not token_ids:
        return None
    slug = (event.get("slug") or "").strip()
    return {
        "condition_id": h2h["conditionId"],
        "event_id": event.get("id"),
        "title": match_title,
        "event_slug": slug,
        "tour_prefix": slug.split("-", 1)[0] if slug else "",
        "token_id_yes": token_ids[0],
        "token_id_no": token_ids[1] if len(token_ids) > 1 else None,
        "start_date": event.get("startDate") or h2h.get("startDate"),
        "end_date": event.get("endDate") or h2h.get("endDate"),
    }


def _load_gamma_events_jsonl(path: str | Path, since_ts: int, until_ts: int) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    seen = set()
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            end_ts = _dt_to_ts(event.get("endDate"))
            if end_ts is None or end_ts < since_ts or end_ts > until_ts:
                continue
            row = _extract_h2h_event(event)
            if row is None or row["condition_id"] in seen:
                continue
            seen.add(row["condition_id"])
            out.append(row)
    return out


def step_gamma_events(
    con,
    client: PolymarketClient,
    since_year: int,
    until_year: int,
    gamma_jsonl: str | Path | None = None,
    offline_gamma: bool = False,
) -> int:
    since_ts = int(datetime(since_year, 1, 1, tzinfo=timezone.utc).timestamp())
    until_ts = int(datetime(until_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    events = [] if offline_gamma else client.get_resolved_tennis_markets(since_ts=since_ts, until_ts=until_ts)
    if not events and gamma_jsonl:
        events = _load_gamma_events_jsonl(gamma_jsonl, since_ts=since_ts, until_ts=until_ts)
        LOG.info("gamma_events: live fetch empty, loaded %d events from %s", len(events), gamma_jsonl)
    LOG.info("gamma_events: upserting %d events", len(events))
    for event in events:
        con.execute(
            """
            INSERT OR REPLACE INTO gamma_events
            (condition_id, event_id, title, event_slug, tour_prefix,
             token_id_yes, token_id_no, start_date, end_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event["condition_id"],
                event.get("event_id"),
                event.get("title"),
                event.get("event_slug"),
                event.get("tour_prefix"),
                event.get("token_id_yes"),
                event.get("token_id_no"),
                event.get("start_date"),
                event.get("end_date"),
            ],
        )
    return len(events)


def _tokens_missing_price_history(con, token_ids: Iterable[str], fidelity: int) -> list[str]:
    missing = []
    for token_id in token_ids:
        if not token_id:
            continue
        row = con.execute(
            "SELECT 1 FROM prices_history WHERE token_id = ? AND fidelity = ? LIMIT 1",
            [token_id, fidelity],
        ).fetchone()
        if row is None:
            missing.append(token_id)
    return missing


def _flush_price_rows(con, rows: list[tuple[str, int, float, int]]) -> None:
    if not rows:
        return
    fd, tmp_path = tempfile.mkstemp(prefix="stink_bid_prices_", suffix=".csv")
    os.close(fd)
    try:
        with open(tmp_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["token_id", "timestamp_sec", "price", "fidelity"])
            writer.writerows(rows)
        con.execute(
            """
            CREATE TEMP TABLE tmp_prices_history (
                token_id VARCHAR,
                timestamp_sec BIGINT,
                price DOUBLE,
                fidelity INTEGER
            )
            """
        )
        con.execute(f"COPY tmp_prices_history FROM '{tmp_path}' (HEADER, DELIMITER ',')")
        con.execute(
            """
            INSERT OR IGNORE INTO prices_history
            (token_id, timestamp_sec, price, fidelity)
            SELECT token_id, timestamp_sec, price, fidelity
            FROM tmp_prices_history
            """
        )
        con.execute("DROP TABLE tmp_prices_history")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def step_prices_history(
    con,
    client: PolymarketClient,
    fidelity: int = 1,
    sleep_s: float = 0.15,
    aligned_only: bool = False,
    limit_markets: Optional[int] = None,
    offset_markets: int = 0,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
) -> int:
    join_alignment = "JOIN event_alignment a USING(condition_id)" if aligned_only else ""
    limit_clause = "LIMIT ?" if limit_markets else ""
    offset_clause = "OFFSET ?" if offset_markets else ""
    year_expr = "CAST(substr(a.sack_date, 1, 4) AS INTEGER)" if aligned_only else "year(g.end_date)"
    year_filters = []
    year_params = []
    if since_year is not None:
        year_filters.append(f"{year_expr} >= ?")
        year_params.append(since_year)
    if until_year is not None:
        year_filters.append(f"{year_expr} <= ?")
        year_params.append(until_year)
    year_clause = f"AND {' AND '.join(year_filters)}" if year_filters else ""
    page_params = []
    if limit_markets:
        page_params.append(limit_markets)
    if offset_markets:
        page_params.append(offset_markets)
    rows = con.execute(
        f"""
        SELECT g.condition_id, g.token_id_yes, g.token_id_no, g.start_date, g.end_date
        FROM gamma_events g
        {join_alignment}
        WHERE (
            NOT EXISTS (
                SELECT 1 FROM prices_history p
                WHERE p.token_id = g.token_id_yes AND p.fidelity = ?
            ) OR (
                g.token_id_no IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM prices_history p
                    WHERE p.token_id = g.token_id_no AND p.fidelity = ?
                )
            )
        )
        {year_clause}
        ORDER BY end_date NULLS LAST, condition_id
        {limit_clause}
        {offset_clause}
        """,
        [fidelity, fidelity, *year_params, *page_params],
    ).fetchall()
    inserted = 0
    fetches = 0
    pending_rows: list[tuple[str, int, float, int]] = []
    for _cid, token_yes, token_no, start_dt, end_dt in rows:
        tokens = _tokens_missing_price_history(con, [token_yes, token_no], fidelity)
        if not tokens:
            continue
        start_ts, end_ts = _window_for_event(start_dt, end_dt)
        for token_id in tokens:
            fetches += 1
            series = client.get_prices_series(token_id, start_ts, end_ts, fidelity=fidelity)
            if series:
                pending_rows.extend((token_id, point["timestamp"], point["price"], fidelity) for point in series)
                if len(pending_rows) >= 100_000:
                    _flush_price_rows(con, pending_rows)
                    pending_rows.clear()
            inserted += len(series)
            if sleep_s:
                time.sleep(sleep_s)
    _flush_price_rows(con, pending_rows)
    LOG.info("prices_history: %d token fetches, %d points seen", fetches, inserted)
    return inserted


def trade_row_hash(condition_id: str, trade: dict) -> str:
    parts = [
        condition_id,
        str(trade.get("transactionHash") or trade.get("hash") or ""),
        str(trade.get("asset") or ""),
        str(trade.get("price") or ""),
        str(trade.get("size") or ""),
        str(trade.get("side") or ""),
        str(trade.get("outcome") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _flush_trade_rows(con, rows: list[tuple]) -> None:
    if not rows:
        return
    fd, tmp_path = tempfile.mkstemp(prefix="stink_bid_trades_", suffix=".csv")
    os.close(fd)
    try:
        with open(tmp_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "row_hash",
                    "condition_id",
                    "transaction_hash",
                    "asset",
                    "timestamp_sec",
                    "outcome",
                    "price",
                    "size",
                    "side",
                ]
            )
            writer.writerows(rows)
        con.execute(
            """
            CREATE TEMP TABLE tmp_trades (
                row_hash VARCHAR,
                condition_id VARCHAR,
                transaction_hash VARCHAR,
                asset VARCHAR,
                timestamp_sec BIGINT,
                outcome VARCHAR,
                price DOUBLE,
                size DOUBLE,
                side VARCHAR
            )
            """
        )
        con.execute(f"COPY tmp_trades FROM '{tmp_path}' (HEADER, DELIMITER ',', NULL '')")
        con.execute(
            """
            INSERT OR IGNORE INTO trades
            (row_hash, condition_id, transaction_hash, asset, timestamp_sec,
             outcome, price, size, side)
            SELECT row_hash, condition_id, transaction_hash, asset, timestamp_sec,
                   outcome, price, size, side
            FROM tmp_trades
            """
        )
        con.execute("DROP TABLE tmp_trades")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _upsert_trade_coverage(
    con,
    *,
    condition_id: str,
    start_ts: int,
    end_ts: int,
    page_size: int,
    max_pages: int,
    pages_fetched: int,
    rows_fetched: int,
    rows_inserted: int,
    hit_page_cap: bool,
    first_trade_ts: Optional[int],
    last_trade_ts: Optional[int],
    error: Optional[str] = None,
) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO trade_coverage
        (condition_id, start_ts, end_ts, page_size, max_pages, pages_fetched,
         rows_fetched, rows_inserted, hit_page_cap, first_trade_ts, last_trade_ts,
         fetched_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """,
        [
            condition_id,
            start_ts,
            end_ts,
            page_size,
            max_pages,
            pages_fetched,
            rows_fetched,
            rows_inserted,
            hit_page_cap,
            first_trade_ts,
            last_trade_ts,
            error,
        ],
    )


def step_trades(
    con,
    client: PolymarketClient,
    sleep_s: float = 0.15,
    aligned_only: bool = False,
    limit_markets: Optional[int] = None,
    offset_markets: int = 0,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    page_size: int = 200,
    max_pages: int = 50,
    refetch_complete: bool = False,
    refetch_incomplete: bool = True,
    progress_every: int = 25,
) -> int:
    join_alignment = "JOIN event_alignment a USING(condition_id)" if aligned_only else ""
    limit_clause = "LIMIT ?" if limit_markets else ""
    offset_clause = "OFFSET ?" if offset_markets else ""
    year_expr = "CAST(substr(a.sack_date, 1, 4) AS INTEGER)" if aligned_only else "year(g.end_date)"
    year_filters = []
    year_params = []
    if since_year is not None:
        year_filters.append(f"{year_expr} >= ?")
        year_params.append(since_year)
    if until_year is not None:
        year_filters.append(f"{year_expr} <= ?")
        year_params.append(until_year)
    year_clause = f"AND {' AND '.join(year_filters)}" if year_filters else ""
    page_params = []
    if limit_markets:
        page_params.append(limit_markets)
    if offset_markets:
        page_params.append(offset_markets)
    rows = con.execute(
        f"""
        SELECT g.condition_id, g.start_date, g.end_date
        FROM gamma_events g
        {join_alignment}
        LEFT JOIN trade_coverage tc ON tc.condition_id = g.condition_id
        WHERE (
            ? OR tc.condition_id IS NULL OR (? AND tc.hit_page_cap)
        )
        {year_clause}
        ORDER BY g.end_date NULLS LAST, g.condition_id
        {limit_clause}
        {offset_clause}
        """,
        [refetch_complete, refetch_incomplete, *year_params, *page_params],
    ).fetchall()
    inserted = 0
    pending_rows = []
    for idx, (condition_id, start_dt, end_dt) in enumerate(rows, start=1):
        start_ts, end_ts = _window_for_event(start_dt, end_dt)
        try:
            result = client.get_trades_window_with_meta(
                condition_id,
                start_ts,
                end_ts,
                page_size=page_size,
                max_pages=max_pages,
            )
            trades = result["trades"]
            fetch_error = None
        except Exception as exc:
            LOG.exception("trades: fetch failed condition_id=%s", condition_id)
            trades = []
            result = {
                "pages_fetched": 0,
                "hit_page_cap": False,
                "first_trade_ts": None,
                "last_trade_ts": None,
            }
            fetch_error = str(exc)
        before_count = con.execute("SELECT COUNT(*) FROM trades WHERE condition_id = ?", [condition_id]).fetchone()[0]
        for trade in trades:
            price = trade.get("price")
            size = trade.get("size")
            pending_rows.append(
                (
                    trade_row_hash(condition_id, trade),
                    condition_id,
                    trade.get("transactionHash") or trade.get("hash"),
                    trade.get("asset"),
                    trade.get("_ts_sec"),
                    trade.get("outcome"),
                    float(price) if price is not None else None,
                    float(size) if size is not None else None,
                    trade.get("side"),
                )
            )
            if len(pending_rows) >= 50_000:
                _flush_trade_rows(con, pending_rows)
                pending_rows.clear()
        _flush_trade_rows(con, pending_rows)
        pending_rows.clear()
        after_count = con.execute("SELECT COUNT(*) FROM trades WHERE condition_id = ?", [condition_id]).fetchone()[0]
        rows_inserted = max(0, int(after_count) - int(before_count))
        inserted += len(trades)
        _upsert_trade_coverage(
            con,
            condition_id=condition_id,
            start_ts=start_ts,
            end_ts=end_ts,
            page_size=page_size,
            max_pages=max_pages,
            pages_fetched=int(result["pages_fetched"]),
            rows_fetched=len(trades),
            rows_inserted=rows_inserted,
            hit_page_cap=bool(result["hit_page_cap"]),
            first_trade_ts=result["first_trade_ts"],
            last_trade_ts=result["last_trade_ts"],
            error=fetch_error,
        )
        if progress_every and (idx == 1 or idx % progress_every == 0 or idx == len(rows)):
            LOG.info(
                "trades progress: %d/%d markets rows_seen=%d hit_cap=%s condition_id=%s",
                idx,
                len(rows),
                inserted,
                bool(result["hit_page_cap"]),
                condition_id,
            )
        if sleep_s:
            time.sleep(sleep_s)
    _flush_trade_rows(con, pending_rows)
    LOG.info("trades: %d markets fetched, %d rows seen", len(rows), inserted)
    return inserted


def step_sackmann_load(con, cache_root: str | Path, years: Iterable[int]) -> int:
    matches = load_all(Path(cache_root), years=years)
    LOG.info("sackmann_load: upserting %d matches", len(matches))
    if not matches:
        return 0

    fd, tmp_path = tempfile.mkstemp(prefix="stink_bid_sackmann_", suffix=".csv")
    os.close(fd)
    try:
        with open(tmp_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "tour",
                    "tourney_date",
                    "winner_name",
                    "loser_name",
                    "tourney_name",
                    "surface",
                    "tourney_level",
                    "minutes",
                    "score",
                    "source",
                ]
            )
            for match in matches:
                writer.writerow(
                    [
                        match.tour,
                        match.tourney_date,
                        match.winner_name,
                        match.loser_name,
                        match.tourney_name,
                        match.surface,
                        match.tourney_level,
                        match.minutes,
                        match.score,
                        match.source,
                    ]
                )
        con.execute(
            """
            CREATE TEMP TABLE tmp_sackmann_matches (
                tour VARCHAR,
                tourney_date VARCHAR,
                winner_name VARCHAR,
                loser_name VARCHAR,
                tourney_name VARCHAR,
                surface VARCHAR,
                tourney_level VARCHAR,
                minutes INTEGER,
                score VARCHAR,
                source VARCHAR
            )
            """
        )
        con.execute(f"COPY tmp_sackmann_matches FROM '{tmp_path}' (HEADER, DELIMITER ',')")
        con.execute(
            """
            INSERT OR REPLACE INTO sackmann_matches
            (tour, tourney_date, winner_name, loser_name, tourney_name, surface,
             tourney_level, minutes, score, source)
            SELECT tour, tourney_date, winner_name, loser_name, tourney_name,
                   surface, tourney_level, minutes, score, source
            FROM tmp_sackmann_matches
            """
        )
        con.execute("DROP TABLE tmp_sackmann_matches")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return len(matches)


def _load_sackmann_from_db(con) -> list[SackmannMatch]:
    rows = con.execute(
        """
        SELECT tour, winner_name, loser_name, tourney_date, tourney_name,
               surface, tourney_level, minutes, score, source
        FROM sackmann_matches
        """
    ).fetchall()
    return [SackmannMatch(*row) for row in rows]


def _date_from_yyyymmdd(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _surname_candidates(name: str) -> set[str]:
    """Return compact surname candidates for full and abbreviated tennis names."""
    normalized = normalize_name(name)
    tokens = normalized.split()
    if not tokens:
        return set()
    candidates = {tokens[-1]}
    if len(tokens) > 1 and len(tokens[-1]) == 1:
        surname_tokens = tokens[:-1]
        candidates.add("".join(surname_tokens))
        candidates.update(surname_tokens)
    return {candidate.replace(" ", "") for candidate in candidates if candidate}


def _build_match_index(matches: list[SackmannMatch]) -> dict[tuple[str, str], list[SackmannMatch]]:
    index: dict[tuple[str, str], list[SackmannMatch]] = {}
    for match in matches:
        winner_candidates = _surname_candidates(match.winner_name)
        loser_candidates = _surname_candidates(match.loser_name)
        for winner in winner_candidates:
            for loser in loser_candidates:
                key = tuple(sorted((winner, loser)))
                index.setdefault(key, []).append(match)
    return index


def _align_event_indexed(
    title: str,
    end_date_iso: str,
    match_index: dict[tuple[str, str], list[SackmannMatch]],
    window_days: int,
) -> Optional[AlignedMatch]:
    side1_candidates, side2_candidates = title_player_candidate_sets(title)
    if not side1_candidates or not side2_candidates or title_looks_like_doubles(title):
        return None
    candidates = []
    seen_ids = set()
    for name1 in side1_candidates:
        for name2 in side2_candidates:
            key = tuple(sorted((name1, name2)))
            for match in match_index.get(key, []):
                ident = (match.tour, match.tourney_date, match.winner_name, match.loser_name)
                if ident in seen_ids:
                    continue
                seen_ids.add(ident)
                candidates.append(match)
    if not candidates:
        return None
    try:
        target = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    best: Optional[AlignedMatch] = None
    for match in candidates:
        match_date = _date_from_yyyymmdd(match.tourney_date)
        if match_date is None:
            continue
        delta = abs((match_date - target).days)
        if delta > window_days:
            continue
        confidence = max(0.0, 1.0 - delta / window_days)
        if best is None or confidence > best.alignment_confidence:
            best = AlignedMatch(match, date_delta_days=delta, alignment_confidence=confidence)
    return best


def step_alignment(con, window_days: int = 21) -> int:
    events = con.execute("SELECT condition_id, title, end_date FROM gamma_events").fetchall()
    matches = _load_sackmann_from_db(con)
    match_index = _build_match_index(matches)
    hits = 0
    con.execute("DELETE FROM event_alignment WHERE condition_id IN (SELECT condition_id FROM gamma_events)")
    for condition_id, title, end_dt in events:
        aligned = _align_event_indexed(
            title=title or "",
            end_date_iso=_dt_to_iso(end_dt),
            match_index=match_index,
            window_days=window_days,
        )
        if aligned is None:
            continue
        hits += 1
        con.execute(
            """
            INSERT OR REPLACE INTO event_alignment
            (condition_id, sack_tour, sack_date, sack_winner, sack_loser,
             date_delta_days, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                condition_id,
                aligned.sackmann.tour,
                aligned.sackmann.tourney_date,
                aligned.sackmann.winner_name,
                aligned.sackmann.loser_name,
                aligned.date_delta_days,
                aligned.alignment_confidence,
            ],
        )
    total = len(events)
    LOG.info("alignment: %d/%d events aligned (%.1f%%)", hits, total, 100 * hits / max(total, 1))
    return hits


def run_steps(args) -> None:
    client = PolymarketClient(max_retries=args.max_retries, retry_delay=args.retry_delay, timeout=args.timeout)
    con = open_backtest_db(args.db)
    years = range(args.since_year, args.until_year + 1)
    steps = [
        (
            "gamma_events",
            lambda: step_gamma_events(
                con,
                client,
                args.since_year,
                args.until_year,
                gamma_jsonl=args.gamma_jsonl,
                offline_gamma=args.offline_gamma,
            ),
        ),
        (
            "prices_history",
            lambda: step_prices_history(
                con,
                client,
                sleep_s=args.sleep,
                aligned_only=args.aligned_only,
                limit_markets=args.limit_markets,
                offset_markets=args.offset_markets,
                since_year=args.since_year,
                until_year=args.until_year,
                page_size=args.trade_page_size,
                max_pages=args.trade_max_pages,
                refetch_complete=args.refetch_complete_trades,
                refetch_incomplete=not args.no_refetch_incomplete_trades,
                progress_every=args.progress_every,
            ),
        ),
        (
            "trades",
            lambda: step_trades(
                con,
                client,
                sleep_s=args.sleep,
                aligned_only=args.aligned_only,
                limit_markets=args.limit_markets,
                offset_markets=args.offset_markets,
                since_year=args.since_year,
                until_year=args.until_year,
            ),
        ),
        ("sackmann_load", lambda: step_sackmann_load(con, args.sackmann_cache, years)),
        ("alignment", lambda: step_alignment(con, window_days=args.window_days)),
    ]
    try:
        for name, fn in steps:
            if args.only and args.only != name:
                continue
            if name in args.skip:
                continue
            t0 = time.time()
            if args.dry_run:
                LOG.info("dry-run: would run step %s", name)
                continue
            n = fn()
            LOG.info("step %s: returned=%s elapsed=%.1fs", name, n, time.time() - t0)
    finally:
        con.close()


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since-year", type=int, default=2024)
    parser.add_argument("--until-year", type=int, default=datetime.now(timezone.utc).year)
    parser.add_argument("--sackmann-cache", default="data/feasibility_cache")
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument(
        "--gamma-jsonl",
        default="docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl",
        help="Fallback Gamma events JSONL used when live Gamma fetch returns no events.",
    )
    parser.add_argument("--offline-gamma", action="store_true", help="Skip live Gamma API and load --gamma-jsonl directly.")
    parser.add_argument("--skip", action="append", default=[], choices=STEP_NAMES)
    parser.add_argument("--only", choices=STEP_NAMES)
    parser.add_argument("--aligned-only", action="store_true", help="For price/trade steps, fetch only aligned events.")
    parser.add_argument("--limit-markets", type=int, help="Limit markets considered by price/trade steps.")
    parser.add_argument("--offset-markets", type=int, default=0, help="Offset into ordered price/trade candidates.")
    parser.add_argument("--window-days", type=int, default=21)
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--trade-page-size", type=int, default=200)
    parser.add_argument("--trade-max-pages", type=int, default=50)
    parser.add_argument("--refetch-complete-trades", action="store_true")
    parser.add_argument("--no-refetch-incomplete-trades", action="store_true")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_steps(parse_args(argv))


if __name__ == "__main__":
    main()
