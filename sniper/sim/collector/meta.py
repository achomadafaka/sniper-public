"""Metadata sync — Gamma API polling, universe filtering, snapshot persistence, settlement detection."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

from sniper.sim.collector.config import CollectorConfig
from sniper.sim.collector.writer import RawWriter
from sniper.sim.ids import make_market_id
from sniper.sim.market_type import classify
from sniper.sim.schema import RawEvent, EventType
from sniper.sim.universe import UniverseFilter, MarketCandidate

LOG = logging.getLogger(__name__)


def parse_gamma_market(raw: dict[str, Any]) -> Optional[dict]:
    """Parse a single Gamma API market response into a normalized dict.

    Returns None if the market is missing required fields.
    """
    condition_id = raw.get("conditionId", "")
    if not condition_id:
        return None

    question = raw.get("question", "")

    try:
        outcomes = json.loads(raw.get("outcomes", "[]"))
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    try:
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))
    except (json.JSONDecodeError, TypeError):
        token_ids = []

    try:
        outcome_prices = json.loads(raw.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        outcome_prices = []

    try:
        volume = float(raw.get("volume", 0))
    except (TypeError, ValueError):
        volume = 0.0

    active = bool(raw.get("active", False))
    closed = bool(raw.get("closed", False))
    end_date = raw.get("endDate", "")

    tokens = []
    for i, (outcome, tid) in enumerate(zip(outcomes, token_ids)):
        tokens.append({"token_id": str(tid), "label": str(outcome), "index": i})

    # Fee/tradability metadata — these are distinct fields in Gamma.
    fees_enabled_raw = raw.get("feesEnabled")
    if isinstance(fees_enabled_raw, bool):
        fees_enabled: Optional[bool] = fees_enabled_raw
    elif isinstance(fees_enabled_raw, str) and fees_enabled_raw.lower() in {"true", "false"}:
        fees_enabled = fees_enabled_raw.lower() == "true"
    else:
        fees_enabled = None

    enable_order_book_raw = raw.get("enableOrderBook")
    if isinstance(enable_order_book_raw, bool):
        enable_order_book: Optional[bool] = enable_order_book_raw
    elif isinstance(enable_order_book_raw, str) and enable_order_book_raw.lower() in {"true", "false"}:
        enable_order_book = enable_order_book_raw.lower() == "true"
    else:
        enable_order_book = None

    fee = raw.get("fee")

    return {
        "condition_id": condition_id,
        "question": question,
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "tokens": tokens,
        "volume": volume,
        "active": active,
        "closed": closed,
        "end_date": end_date,
        "fees_enabled": fees_enabled,
        "enable_order_book": enable_order_book,
        "fee": fee,
        "market_id": make_market_id("polymarket", condition_id),
        "market_type": classify("polymarket", question),
    }


def filter_and_classify(
    raw_markets: list[dict[str, Any]],
    max_markets: int = 200,
    min_volume: float = 10_000.0,
    max_days_to_end: int = 90,
) -> list[dict]:
    """Parse, filter, and classify a batch of Gamma API markets."""
    uf = UniverseFilter(
        min_volume=min_volume,
        max_days_to_end=max_days_to_end,
        max_active=max_markets,
    )

    parsed = []
    for raw in raw_markets:
        m = parse_gamma_market(raw)
        if m is None:
            continue
        candidate = MarketCandidate(
            condition_id=m["condition_id"],
            question=m["question"],
            outcomes=m["outcomes"],
            volume=m["volume"],
            end_date=m["end_date"],
            active=m["active"],
            closed=m["closed"],
        )
        if uf.qualifies(candidate):
            parsed.append(m)

    # Apply cap by volume
    parsed.sort(key=lambda m: m["volume"], reverse=True)
    return parsed[:max_markets]


def write_market_snapshot(path: str, markets: list[dict[str, Any]]) -> None:
    """Write filtered markets to a JSON file for the WS collector to load."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(markets, f, separators=(",", ":"))


def build_metadata_events(
    markets: list[dict[str, Any]],
    *,
    ingest_seq_start: int,
    capture_ts: str,
) -> list[RawEvent]:
    """Build MARKET_METADATA and (if resolved) MARKET_RESOLVED events for each market."""
    events: list[RawEvent] = []
    seq = ingest_seq_start
    for market in markets:
        events.append(
            RawEvent(
                venue="polymarket",
                market_id=market["market_id"],
                venue_market_id=market["condition_id"],
                market_type=market["market_type"],
                event_type=EventType.MARKET_METADATA,
                capture_ts=capture_ts,
                ingest_seq=seq,
                payload=market,
            )
        )
        seq += 1
        if market.get("closed") and market.get("outcome_prices"):
            events.append(
                RawEvent(
                    venue="polymarket",
                    market_id=market["market_id"],
                    venue_market_id=market["condition_id"],
                    market_type=market["market_type"],
                    event_type=EventType.MARKET_RESOLVED,
                    capture_ts=capture_ts,
                    ingest_seq=seq,
                    payload={
                        "outcome_prices": market["outcome_prices"],
                        "resolved_ts": capture_ts,
                    },
                )
            )
            seq += 1
    return events


async def _fetch_paginated(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str],
    max_pages: int = 10,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Fetch paginated results from the Gamma API."""
    all_results: list[dict[str, Any]] = []
    for page in range(max_pages):
        page_params = {**params, "limit": str(page_size), "offset": str(page * page_size)}
        async with session.get(url, params=page_params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not isinstance(data, list) or not data:
            break
        all_results.extend(data)
        if len(data) < page_size:
            break
    return all_results


async def fetch_gamma_markets(
    session: aiohttp.ClientSession,
    config: CollectorConfig,
    max_pages: int = 10,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Fetch active markets from the Gamma API with pagination."""
    all_markets: list[dict[str, Any]] = []
    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(page_size),
            "offset": str(page * page_size),
        }
        async with session.get(f"{config.gamma_url}/markets", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not isinstance(data, list) or not data:
            break
        all_markets.extend(data)
        if len(data) < page_size:
            break
    return all_markets


def _has_clear_winner(outcome_prices: list) -> bool:
    """Check if outcome_prices contains at least one price >= 0.99.

    Rejects junk like ["0","0"] that the Gamma API returns for ancient markets.
    """
    try:
        return any(float(p) >= 0.99 for p in outcome_prices)
    except (TypeError, ValueError):
        return False


def detect_resolutions(
    all_parsed: list[dict],
    previously_known: dict[str, dict],
) -> list[dict]:
    """Detect markets that transitioned from active to closed between poll cycles.

    Returns the list of newly-resolved markets (closed=True, with a clear winner
    in outcome_prices — at least one price >= 0.99).
    """
    resolved = []
    for m in all_parsed:
        cid = m["condition_id"]
        was_known = cid in previously_known
        now_closed = m.get("closed", False)
        prices = m.get("outcome_prices", [])
        has_winner = bool(prices) and _has_clear_winner(prices)
        if was_known and now_closed and has_winner:
            prev = previously_known[cid]
            if not prev.get("closed", False):
                resolved.append(m)
    return resolved


async def run_meta_loop(config: CollectorConfig) -> None:
    """Main metadata sync loop — poll Gamma API, filter, write snapshot and raw events."""
    writer = RawWriter(raw_dir=config.raw_dir, venue=config.venue, rotation_seconds=config.rotation_seconds)
    seq = 0
    known_markets: dict[str, dict] = {}  # condition_id -> last known state

    async with aiohttp.ClientSession() as session:
        while True:
            # Fetch active markets (for snapshot + WS subscription)
            raw_markets = await fetch_gamma_markets(session, config)

            # Fetch recently-closed markets (for settlement detection).
            # Use end_date_min to avoid ancient junk (2020/2021 markets with
            # outcome_prices=["0","0"]). A 7-day lookback is ample — the poll
            # runs every 30 min, so any transition will be caught within hours.
            lookback = datetime.now(timezone.utc) - timedelta(days=7)
            end_date_min = lookback.strftime("%Y-%m-%d")
            raw_closed = await _fetch_paginated(
                session,
                f"{config.gamma_url}/markets",
                params={"closed": "true", "end_date_min": end_date_min},
                max_pages=5,
                page_size=100,
            )

            # Parse ALL markets (active + recently closed) to detect resolutions
            all_parsed = []
            for raw in raw_markets + raw_closed:
                m = parse_gamma_market(raw)
                if m is not None:
                    all_parsed.append(m)

            # Detect markets that just resolved (were active last cycle, now closed)
            from sniper.sim.schema import capture_ts_now
            capture_ts = capture_ts_now()
            newly_resolved = detect_resolutions(all_parsed, known_markets)
            for m in newly_resolved:
                events = build_metadata_events([m], ingest_seq_start=seq, capture_ts=capture_ts)
                for event in events:
                    writer.write(event)
                seq += len(events)
                LOG.info("Market resolved: %s (%s)", m["condition_id"], m.get("question", "")[:60])

            # Update known state for next cycle
            for m in all_parsed:
                known_markets[m["condition_id"]] = m

            # Filter for active markets → snapshot for WS collector
            markets = filter_and_classify(
                raw_markets,
                max_markets=config.max_markets,
                min_volume=config.min_volume,
                max_days_to_end=config.max_days_to_end,
            )
            write_market_snapshot(config.snapshot_path, markets)

            # Write metadata events for active markets
            capture_ts = capture_ts_now()
            events = build_metadata_events(markets, ingest_seq_start=seq, capture_ts=capture_ts)
            for event in events:
                writer.write(event)
            writer.flush()
            seq += len(events)

            LOG.info("Metadata sync: %d active, %d resolved, refreshed %s",
                     len(markets), len(newly_resolved), config.snapshot_path)
            await asyncio.sleep(config.meta_poll_interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run_meta_loop(CollectorConfig.from_env()))
