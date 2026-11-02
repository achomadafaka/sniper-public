"""Forward-only chart-truth collector. MEASUREMENT ONLY — never writes a funded decision surface.

One hourly pass records the chart price at the minute the SOURCE TRADER bought, so his own route
premium and the coin's true move stop being one inseparable ratio. Design:
`docs/superpowers/specs/2026-08-27-memecoin-chart-truth-at-source-buy-design.md`.

FORWARD ONLY. A GeckoTerminal page reaches 1,000 minutes back — about 16.7 hours, not the full
history. A six-week backfill would need ~60 pages for each of ~1,641 pools, roughly 98,000 requests
or 55 hours of continuous polling. Do not attempt it.

A THREE-MINUTE WINDOW, NOT A POINT. `memecoin_events.created_at` is a feed-side clock, not a
settled fill time: 44% of buys carry a `created_at` LATER than our own observation of them, spread
about +-27s, which puts ~21% of them in the wrong minute. One page already returns 1,000 candles,
so the neighbours cost no extra request. A single candle would state a false precision.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sniper.execution.geckoterminal_quote import (GT_OHLCV_NETWORK, GtRateLimited,
                                                  gt_pool_ohlcv_minute)
from sniper.persistence import db_memecoin_chart_truth as store
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso

logger = logging.getLogger(__name__)

PAGE_MINUTES = 1000                  # one GT page; the deferral deadline follows from it
WINDOW_MINUTES = (-1, 0, 1)          # the buy minute and its neighbours


def _minute_iso(dt: datetime) -> str:
    """ISO minute in Python — never SQLite `datetime('now')`, which renders a SPACE separator and
    sorts before 'T', silently widening a 24-hour window to about 40 hours (2026-08-23)."""
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def window_minutes(buy_at: datetime):
    """The buy minute and its neighbours, as ISO minutes."""
    base = buy_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return [_minute_iso(base + timedelta(minutes=k)) for k in WINDOW_MINUTES]


def discover(conn, *, since_iso, now_iso, limit=2000, pool_window_s=172800):
    """Work items from BUY EVENTS, which are admission-independent. Returns
    `(items, skipped_no_pool, skipped_chain)`.

    WHY EVENTS AND NOT ENTRY SNAPSHOTS. This read `memecoin_entry_snapshots`, which is written
    inside the paper-arm admission path — and once the empty-arms guard landed, a dead lane wrote
    almost none. Measured 2026-08-28: 355 buy events since 09:00 against 60 snapshots, so the
    collector saw 17% of its own population and would have starved completely with the lane down.
    A collector must not depend on an ADMISSION DECISION to learn that a trade happened.

    `memecoin_events` is the receipt we already have: written at the ingest seam
    (`memecoin_follow_activity` / `memecoin_runner`), upstream of arms, and `copy_event_to_arms`
    never touches it. So no new table and no ingest-path change is needed — the decoupling the
    review asked for already exists in the schema.

    ZERO PROVIDER CALLS HERE. The pool comes from `memecoin_quote_observations_v2.pair_address`
    (74% populated; 93% of recent solana buy events resolve) with `memecoin_entry_snapshots` as a
    second source where it exists. An event whose pool is unknown is SKIPPED AND COUNTED, never
    resolved on this path: resolving costs a request, and this function runs before the yield check.

    Cost still scales with distinct POOLS, not events: median 10/hour, p90 16, max 16 over 28 hours
    — inside the 20/hour budget with no hour over it.
    """
    import json
    pool_since = _minute_iso(_parse(now_iso) - timedelta(seconds=pool_window_s))
    pools = {}
    for mint, pair in conn.execute(
            "SELECT mint, pair_address FROM memecoin_quote_observations_v2 "
            "WHERE observed_at >= ? AND pair_address IS NOT NULL", (pool_since,)):
        pools.setdefault(mint, pair)
    for features_json, in conn.execute(
            "SELECT features_json FROM memecoin_entry_snapshots WHERE decided_at >= ?",
            (pool_since,)):
        try:
            f = json.loads(features_json or "{}")
        except (TypeError, ValueError):
            continue
        if f.get("mint") and f.get("pair_address"):
            pools.setdefault(f["mint"], f["pair_address"])

    rows = conn.execute(
        "SELECT mint, network_id, created_at FROM memecoin_events "
        "WHERE side='buy' AND created_at >= ? AND created_at < ? ORDER BY created_at LIMIT ?",
        (since_iso, now_iso, int(limit))).fetchall()
    out, skipped_no_pool, skipped_chain = [], 0, 0
    for mint, network_id, created_at in rows:
        buy_at = _parse(created_at)
        if not mint or not network_id or buy_at is None:
            skipped_no_pool += 1
            continue
        # UNMAPPED CHAIN IS OUT OF SCOPE, NOT WORK. `GT_OHLCV_NETWORK` is an allowlist of
        # live-verified slugs, so a chain absent from it can never be fetched. Queuing it would park
        # an item that must age out, and the age-out counter means REAL DATA LOSS.
        if str(network_id) not in GT_OHLCV_NETWORK:
            skipped_chain += 1
            continue
        pool = pools.get(mint)
        if not pool:
            skipped_no_pool += 1          # a missing pool SKIPS; it never guesses one
            continue
        for minute in window_minutes(buy_at):
            out.append((str(network_id), str(pool), minute))
    return out, skipped_no_pool, skipped_chain


def run_once(conn, ledger=None, *, now_fn=real_utc_now_iso, fetch_fn=gt_pool_ohlcv_minute,
             sleep_fn=time.sleep, lookback_s=3600, max_pools=None):
    """One cycle. Returns a dict of counters; never raises for a provider problem.

    `conn` is THIS LANE'S OWN database (`data/memecoin_chart_truth.db`). `ledger` is a READ-ONLY
    handle on `sniper_tracker.db`, where both the work to discover and the yield signal live. A
    measurement lane never holds a writable handle on the funded ledger. `ledger=None` means "the
    same connection", which is only for tests using one in-memory database.
    """
    ledger = conn if ledger is None else ledger
    now_iso = now_fn()
    now = _parse(now_iso)
    store.ensure_tables(conn)

    # RULE 1+2 — yield to the funded lane. GeckoTerminal has no key and limits by IP, and the
    # funded mark lane shares that IP. This is the Jupiter trap: on 2026-08-21 a research retry loop
    # burned ~7,000 requests of a funded quota in four hours by assuming spare capacity.
    # DISCOVER AND ENQUEUE FIRST, ALWAYS — a yield defers the FETCH, never the work.
    # This returned before discovery, so a yielded cycle queued nothing and the lane is forward-only
    # with a bounded lookback: those events were gone, not delayed. That is the precise rule
    # f0c28426 exists to enforce, broken in the function it was written for. Reproduced: 51 funded
    # marks plus one new event queued ZERO rows.
    items, no_pool, no_chain = discover(ledger,
                                        since_iso=_minute_iso(now - timedelta(seconds=lookback_s)),
                                        now_iso=now_iso)
    # DEADLINE FOLLOWS THE SOURCE WINDOW, NOT THE FETCH CLOCK. A page reaches PAGE_MINUTES back from
    # NOW, so an item's real deadline is ITS OWN buy minute plus that span. Deriving it from fetch
    # time expired fresh rows up to an hour before their source window actually closed.
    queued = store.enqueue_with_source_deadlines(conn, items, now_iso=now_iso,
                                                page_minutes=PAGE_MINUTES)
    aged_out = store.expire(conn, now_iso=now_iso)
    if aged_out:
        logger.warning("chart_truth: %d work items aged out past the page window — REAL DATA LOSS",
                       aged_out)

    since = _minute_iso(now - timedelta(seconds=300))
    funded_marks = store.funded_gt_marks_since(ledger, since)
    if funded_marks > cfg.MEMECOIN_CHART_TRUTH_YIELD_MARKS:
        logger.info("chart_truth: yielding the fetch, funded wrote %d GT marks in 5m; "
                    "%d items queued and deferred", funded_marks, len(items))
        return {"skipped_cycle": 1, "funded_marks": funded_marks, "fetched": 0,
                "aged_out": aged_out, "queued": queued, "rows": 0, "pools": 0,
                "no_candle": 0, "provider_error": 0, "no_pool": no_pool,
                "unmapped_chain": no_chain, "rate_limited": 0}
    budget = int(max_pools if max_pools is not None else cfg.MEMECOIN_CHART_TRUTH_MAX_POOLS)
    work = store.due(conn, now_iso=now_iso, limit=budget * len(WINDOW_MINUTES))
    # one request per POOL, not per minute — a page carries every minute we need
    by_pool = {}
    for w in work:
        by_pool.setdefault((w["network_id"], w["pool_address"]), []).append(w["buy_minute"])

    counters = {"skipped_cycle": 0, "funded_marks": funded_marks, "queued": queued,
                "pools": 0, "fetched": 0,
                "rows": 0, "no_candle": 0, "provider_error": 0, "aged_out": aged_out,
                "no_pool": no_pool, "unmapped_chain": no_chain, "rate_limited": 0}
    gap = cfg.MEMECOIN_CHART_TRUTH_MIN_GAP_MS / 1000.0
    first = True
    for (net, pool), minutes in list(by_pool.items())[:budget]:
        if not first:
            sleep_fn(gap)               # RULE 3 — hold under 24 req/min against a 30/min budget
        first = False
        counters["pools"] += 1
        try:
            page = fetch_fn(net, pool)
        except GtRateLimited:
            # RULE 4 — stop the cycle on the FIRST 429. No retry. Everything unfetched stays queued.
            counters["rate_limited"] = 1
            logger.warning("chart_truth: 429 from GeckoTerminal, stopping this cycle")
            break
        except Exception:
            logger.exception("chart_truth: provider error on %s/%s", net, pool)
            counters["provider_error"] += 1
            continue
        if not page:
            counters["provider_error"] += 1
            continue
        counters["fetched"] += 1
        by_minute = {_minute_iso(datetime.fromtimestamp(ts, timezone.utc)): v
                     for ts, v in page.items()}
        found = {m: by_minute[m] for m in minutes if m in by_minute}
        # A MISSING MINUTE WRITES NOTHING. It does not substitute a neighbour: an absent row means
        # "unknown", which is a truthful answer, and a substituted one is a fabricated observation.
        counters["no_candle"] += len(minutes) - len(found)
        if found:
            counters["rows"] += store.save_candles(conn, net, pool, found, now_iso=now_iso)
        for m in minutes:
            if m in found:
                store.resolve(conn, net, pool, m)
            else:
                store.bump_attempt(conn, net, pool, m)
        conn.commit()
    return counters
