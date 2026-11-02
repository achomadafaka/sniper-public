import argparse
import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List

from db import get_connection, init_db, upsert_market_outcome
from sniper.markets.polymarket_api import build_session

CLOB_BASE_URL = "https://clob.polymarket.com"
OUTCOME_POLL_INTERVAL_SECONDS = int(os.getenv("OUTCOME_POLL_INTERVAL_SECONDS", "900"))
OUTCOME_POLL_MAX_PAGES = max(1, int(os.getenv("OUTCOME_POLL_MAX_PAGES", "25")))
OUTCOME_POLL_TIMEOUT_SECONDS = int(os.getenv("OUTCOME_POLL_TIMEOUT_SECONDS", "25"))
OUTCOME_WINNER_PRICE_MIN = float(os.getenv("OUTCOME_WINNER_PRICE_MIN", "0.99"))
OUTCOME_LOSER_PRICE_MAX = float(os.getenv("OUTCOME_LOSER_PRICE_MAX", "0.01"))
OUTCOME_ENABLE_LBT_BACKFILL = str(os.getenv("OUTCOME_ENABLE_LBT_BACKFILL", "1")).strip().lower() in {"1", "true", "yes"}
OUTCOME_BACKFILL_MAX_CONDITION_IDS = max(0, int(os.getenv("OUTCOME_BACKFILL_MAX_CONDITION_IDS", "0")))
DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")
_LBT_BACKFILL_DONE = False


def _parse_any_ts(value: Any) -> int:
    if value is None:
        return int(time.time())
    if isinstance(value, (int, float)):
        ivalue = int(value)
        if ivalue > 10_000_000_000:
            return ivalue // 1000
        return ivalue
    text = str(value).strip()
    if not text:
        return int(time.time())
    try:
        ivalue = int(float(text))
        if ivalue > 10_000_000_000:
            return ivalue // 1000
        return ivalue
    except Exception:
        pass
    # ISO timestamps from CLOB/Gamma are usually UTC with trailing Z.
    text = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return int(time.time())


def _market_outcome_rows(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not bool(market.get("closed")):
        return rows
    condition_id = str(market.get("condition_id") or market.get("conditionId") or "").strip().lower()
    if not condition_id:
        return rows
    tokens = market.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        return rows

    resolved_at = _parse_any_ts(
        market.get("closed_time")
        or market.get("closedTime")
        or market.get("end_date_iso")
        or market.get("endDate")
    )
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        if not outcome:
            continue
        winner = token.get("winner")
        if isinstance(winner, bool):
            resolved_price = 1.0 if winner else 0.0
        else:
            try:
                price = float(token.get("price") or 0.0)
            except Exception:
                continue
            if price >= OUTCOME_WINNER_PRICE_MIN:
                resolved_price = 1.0
            elif price <= OUTCOME_LOSER_PRICE_MAX:
                resolved_price = 0.0
            else:
                # No trustworthy settlement inference for this token.
                continue
        rows.append(
            {
                "condition_id": condition_id,
                "outcome": outcome,
                "resolved_price": resolved_price,
                "resolved_at": resolved_at,
            }
        )
    return rows


def _fetch_market_by_condition_id(session: Any, condition_id: str) -> Dict[str, Any]:
    condition_id = str(condition_id or "").strip().lower()
    if not condition_id:
        return {}
    resp = session.get(
        f"{CLOB_BASE_URL}/markets/{condition_id}",
        timeout=OUTCOME_POLL_TIMEOUT_SECONDS,
    )
    if int(resp.status_code or 0) == 404:
        return {}
    resp.raise_for_status()
    payload = resp.json() if resp.content else {}
    return payload if isinstance(payload, dict) else {}


def _iter_missing_lbt_condition_ids(
    conn: sqlite3.Connection,
    max_condition_ids: int = 0,
) -> Iterable[str]:
    sql = """
        SELECT DISTINCT LOWER(TRIM(t.condition_id)) AS condition_id
        FROM low_buy_trades t
        LEFT JOIN market_outcomes mo
          ON LOWER(mo.condition_id) = LOWER(TRIM(t.condition_id))
        WHERE TRIM(COALESCE(t.condition_id, '')) <> ''
          AND mo.condition_id IS NULL
        ORDER BY condition_id
    """
    params: tuple[Any, ...] = ()
    if max_condition_ids > 0:
        sql += " LIMIT ?"
        params = (int(max_condition_ids),)
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        cid = row[0] if not hasattr(row, "keys") else row["condition_id"]
        value = str(cid or "").strip().lower()
        if value:
            yield value


def backfill_lbt_outcomes_once(
    conn: sqlite3.Connection,
    session: Any,
    max_condition_ids: int = OUTCOME_BACKFILL_MAX_CONDITION_IDS,
) -> Dict[str, int]:
    """Backfill outcomes only for condition IDs seen in low_buy_trades."""
    condition_ids = list(_iter_missing_lbt_condition_ids(conn, max_condition_ids=max_condition_ids))
    looked_up = 0
    closed_markets = 0
    upserted = 0
    failures = 0
    for condition_id in condition_ids:
        try:
            market = _fetch_market_by_condition_id(session, condition_id)
            looked_up += 1
        except Exception:
            failures += 1
            continue
        if not market:
            continue
        rows = _market_outcome_rows(market)
        if rows:
            closed_markets += 1
        for row in rows:
            upsert_market_outcome(
                conn,
                row["condition_id"],
                row["outcome"],
                float(row["resolved_price"]),
                int(row["resolved_at"]),
                source="clob_market_by_condition_id",
            )
            upserted += 1
    conn.commit()
    return {
        "candidate_condition_ids": len(condition_ids),
        "looked_up": looked_up,
        "closed_markets": closed_markets,
        "outcomes_upserted": upserted,
        "failures": failures,
    }


def sync_market_outcomes_once(max_pages: int = OUTCOME_POLL_MAX_PAGES) -> Dict[str, int]:
    global _LBT_BACKFILL_DONE
    conn = get_connection(DB_PATH)
    init_db(conn)
    session = build_session()

    backfill_stats = {
        "candidate_condition_ids": 0,
        "looked_up": 0,
        "closed_markets": 0,
        "outcomes_upserted": 0,
        "failures": 0,
    }
    if OUTCOME_ENABLE_LBT_BACKFILL and not _LBT_BACKFILL_DONE:
        backfill_stats = backfill_lbt_outcomes_once(
            conn,
            session,
            max_condition_ids=OUTCOME_BACKFILL_MAX_CONDITION_IDS,
        )
        _LBT_BACKFILL_DONE = True

    pages = 0
    markets_scanned = 0
    outcomes_upserted = 0
    next_cursor = ""
    while pages < max_pages:
        params: Dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        resp = session.get(
            f"{CLOB_BASE_URL}/markets",
            params=params or None,
            timeout=OUTCOME_POLL_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list) or not data:
            break

        pages += 1
        for market in data:
            if not isinstance(market, dict):
                continue
            markets_scanned += 1
            for row in _market_outcome_rows(market):
                upsert_market_outcome(
                    conn,
                    row["condition_id"],
                    row["outcome"],
                    float(row["resolved_price"]),
                    int(row["resolved_at"]),
                    source="clob_markets_tokens_winner",
                )
                outcomes_upserted += 1
        conn.commit()

        next_cursor = str(payload.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in {"LTE=", "END", "NONE"}:
            break

    conn.close()
    return {
        "backfill_candidate_condition_ids": backfill_stats["candidate_condition_ids"],
        "backfill_looked_up": backfill_stats["looked_up"],
        "backfill_closed_markets": backfill_stats["closed_markets"],
        "backfill_outcomes_upserted": backfill_stats["outcomes_upserted"],
        "backfill_failures": backfill_stats["failures"],
        "pages": pages,
        "markets_scanned": markets_scanned,
        "outcomes_upserted": outcomes_upserted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate market_outcomes from Polymarket CLOB markets.")
    parser.add_argument("--once", action="store_true", help="Run one sync cycle and exit.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.info(
        "outcome-poller starting (interval=%ss, max_pages=%s, db=%s)",
        OUTCOME_POLL_INTERVAL_SECONDS,
        OUTCOME_POLL_MAX_PAGES,
        DB_PATH,
    )

    while True:
        try:
            stats = sync_market_outcomes_once(OUTCOME_POLL_MAX_PAGES)
            logging.info(
                "outcome-poller sync complete pages=%s markets=%s outcomes_upserted=%s",
                stats["pages"],
                stats["markets_scanned"],
                stats["outcomes_upserted"],
            )
            if stats.get("backfill_looked_up", 0) > 0 or stats.get("backfill_failures", 0) > 0:
                logging.info(
                    "outcome-poller backfill looked_up=%s closed_markets=%s outcomes_upserted=%s failures=%s",
                    stats.get("backfill_looked_up", 0),
                    stats.get("backfill_closed_markets", 0),
                    stats.get("backfill_outcomes_upserted", 0),
                    stats.get("backfill_failures", 0),
                )
        except Exception:
            logging.exception("outcome-poller sync failed")

        if args.once:
            return
        time.sleep(max(10, OUTCOME_POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
