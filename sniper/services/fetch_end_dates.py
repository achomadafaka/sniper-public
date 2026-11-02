"""
Background task: fetch endDateIso from Gamma API for all open position_signals
that are missing end_date_iso. Runs at startup and periodically.
"""
import logging
import sqlite3
import time
from threading import Thread
from typing import Optional

import requests

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
BATCH_DELAY = 0.12   # seconds between requests — ~8 RPS
MAX_RETRIES = 2


def _fetch_end_date_for_condition(session: requests.Session, condition_id: str) -> Optional[str]:
    """Return endDateIso string (YYYY-MM-DD) for the given condition_id, or None.

    Uses the condition_ids query param which is the authoritative lookup —
    the slug param matches the market slug (not eventSlug) and we don't store
    market slugs in position_signals, so condition_id is the only safe key.
    """
    if not condition_id:
        return None
    try:
        r = session.get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": condition_id, "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, list):
            return data[0].get("endDateIso") or None
    except Exception as exc:
        log.debug("fetch_end_date condition_id=%s error: %s", condition_id, exc)
    return None


def backfill_end_dates(db_path: str, stop_event=None) -> int:
    """
    Fetch endDateIso for all signals (open or closed) missing end_date_iso.
    Looks up by condition_id via the Gamma API — guaranteed correct regardless
    of slug naming conventions.
    Returns number of rows updated.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row

    # Distinct condition_ids missing end_date_iso — include closed positions too
    # since historical rows are also missing the field.
    rows = conn.execute("""
        SELECT DISTINCT condition_id
        FROM position_signals
        WHERE (end_date_iso IS NULL OR end_date_iso = '')
          AND condition_id IS NOT NULL
          AND condition_id != ''
        ORDER BY analyzed_ts DESC
    """).fetchall()

    if not rows:
        log.info("fetch_end_dates: nothing to backfill")
        conn.close()
        return 0

    log.info("fetch_end_dates: backfilling %d markets", len(rows))
    session = requests.Session()
    session.headers["User-Agent"] = "sniper-end-date-fetcher/1.0"

    updated = 0
    for row in rows:
        if stop_event and stop_event.is_set():
            break
        cid = row["condition_id"]
        end_date = _fetch_end_date_for_condition(session, cid)
        if end_date:
            conn.execute(
                "UPDATE position_signals SET end_date_iso = ? WHERE condition_id = ?",
                (end_date, cid),
            )
            conn.commit()
            updated += 1
            log.debug("fetch_end_dates: %s -> %s", cid[:12], end_date)
        time.sleep(BATCH_DELAY)

    conn.close()
    log.info("fetch_end_dates: updated %d rows", updated)
    return updated


def start_background_fetcher(db_path: str, stop_event=None, interval_hours: float = 6.0):
    """
    Start a daemon thread that backfills end dates at startup and then
    every `interval_hours` hours.
    """
    def _loop():
        while True:
            try:
                backfill_end_dates(db_path, stop_event=stop_event)
            except Exception as exc:
                log.warning("fetch_end_dates loop error: %s", exc)
            # Sleep until next run
            sleep_secs = interval_hours * 3600
            slept = 0
            while slept < sleep_secs:
                if stop_event and stop_event.is_set():
                    return
                time.sleep(min(60, sleep_secs - slept))
                slept += 60

    t = Thread(target=_loop, daemon=True, name="end-date-fetcher")
    t.start()
    return t
