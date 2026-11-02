#!/usr/bin/env python3
"""Refresh market_metadata with current Bitcoin/crypto markets from Polymarket Gamma API.

Run via cron every 6 hours to keep BTC binary markets fresh for crypto_momentum_v1 strategy.
"""
import json
import logging
import sqlite3
import sys
import time

import requests

DB_PATH = "/home/sniper/sniper/sniper_tracker.db"
GAMMA_API = "https://gamma-api.polymarket.com/events"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def refresh_crypto_markets() -> dict:
    """Fetch active crypto events from Gamma API and upsert into market_metadata."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    stats = {"events": 0, "markets_upserted": 0, "errors": 0}

    try:
        # Fetch in batches
        offset = 0
        all_events = []
        while True:
            resp = requests.get(
                GAMMA_API,
                params={
                    "active": "true",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
            if not events:
                break
            all_events.extend(events)
            offset += len(events)
            if len(events) < 100:
                break
            time.sleep(0.25)  # Rate limit

        # Filter for crypto-related events
        crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"]
        crypto_events = []
        for e in all_events:
            title = (e.get("title", "") or "").lower()
            if any(kw in title for kw in crypto_keywords):
                crypto_events.append(e)

        stats["events"] = len(crypto_events)
        logger.info("Found %d crypto events from %d total", len(crypto_events), len(all_events))

        now_ts = int(time.time())
        for e in crypto_events:
            for m in e.get("markets") or []:
                cid = m.get("conditionId", "")
                if not cid:
                    continue
                title = m.get("question", "") or m.get("groupItemTitle", "") or e.get("title", "")
                end_date = m.get("endDate", "") or e.get("endDate", "")
                vol = float(m.get("volume24hr", 0) or 0)

                try:
                    conn.execute(
                        """INSERT INTO market_metadata (condition_id, title, end_date_iso, is_open, volume_24h, category, last_synced_ts)
                           VALUES (?, ?, ?, 1, ?, 'crypto', ?)
                           ON CONFLICT(condition_id) DO UPDATE SET
                               title = excluded.title,
                               end_date_iso = excluded.end_date_iso,
                               is_open = 1,
                               volume_24h = excluded.volume_24h,
                               category = excluded.category,
                               last_synced_ts = excluded.last_synced_ts""",
                        (cid, title, end_date, vol, now_ts),
                    )
                    stats["markets_upserted"] += 1
                except Exception:
                    stats["errors"] += 1

        conn.commit()
        logger.info(
            "Refresh complete: %d events, %d markets upserted, %d errors",
            stats["events"], stats["markets_upserted"], stats["errors"],
        )

    except Exception:
        logger.exception("refresh_crypto_markets failed")
        stats["errors"] += 1
    finally:
        conn.close()

    return stats


if __name__ == "__main__":
    stats = refresh_crypto_markets()
    if "--quiet" not in sys.argv:
        print(json.dumps(stats, indent=2))
