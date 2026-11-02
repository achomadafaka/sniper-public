# Stink Bid Paper Trading — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper-test a maker strategy on Polymarket tennis match-winner markets — resting bids 30% below favorite, fill detection from real WS trades, ESPN set-state gating, dual-label evaluation (strict/loose).

**Architecture:** Five new modules layered on the existing strategy plugin system. `feeds/tennis_scores.py` polls ESPN for set scores. `feeds/polymarket_trade_tap.py` opens a dedicated WS connection for watched tokens. `paper_limit_engine.py` manages the paper order lifecycle (place/reprice/fill/cancel/expire). `strategies/stink_bid_v1.py` provides shared tennis-market helpers and manages already-open positions, while `stink_bid_runner.py` handles discovery, qualification, order placement, ESPN gating, and trade-tap fill detection. On strict fill, `paper_limit_engine` directly inserts into `strategy_positions` (the normal discover→enter path is for synchronous taker entries; maker fills are async).

**Tech Stack:** Python 3.12, SQLite (WAL), websockets, aiohttp, pytest

**Spec:** `docs/superpowers/specs/2026-04-06-stink-bid-paper-trading-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `feeds/tennis_scores.py` | ESPN score feed — poll ATP/WTA scoreboards, match lookup by surname+tour+date, MatchState dataclass |
| `feeds/polymarket_trade_tap.py` | Lightweight WS trade feed — subscribe to specific token_ids, emit unfiltered `last_trade_price` events, dynamic watchlist |
| `paper_limit_engine.py` | Paper limit order manager — place/reprice/fill-check/cancel/expire, DB tables, position bridge on strict fill |
| `strategies/stink_bid_v1.py` | Strategy plugin — tennis-market helpers + management/exit logic for filled paper positions |
| `stink_bid_runner.py` | Runtime loop — discover/qualify/place, ESPN gating, trade tap fill detection, watchlist sync |
| `tests/test_tennis_scores.py` | ESPN feed tests |
| `tests/test_trade_tap.py` | Trade tap tests |
| `tests/test_paper_limit_engine.py` | Paper limit engine tests |
| `tests/test_stink_bid_strategy.py` | Strategy plugin tests |

---

## Task 1: DB Schema — Paper Limit Order Tables

**Files:**
- Modify: `db.py` (add table creation to `init_db()`)
- Create: `tests/test_paper_limit_schema.py`

### Design decisions

Three new tables: `paper_limit_orders`, `paper_limit_fills`, `paper_limit_events`. Separate from `order_intents`/`order_fills` which are for real exchange orders. The `paper_limit_orders` table has a `UNIQUE(strategy_id, condition_id)` index enforcing one-order-per-match.

- [ ] **Step 1: Write the schema test**

```python
# tests/test_paper_limit_schema.py
import sqlite3
import tempfile
import os

def _init_test_db():
    """Create a fresh DB with paper limit tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Import and run init_db or just the paper limit DDL
    from db import init_db
    init_db(path)
    return conn, path


def test_paper_limit_orders_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_orders")
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_orders_unique_constraint():
    """Only one order per (strategy_id, condition_id)."""
    conn, path = _init_test_db()
    try:
        conn.execute("""
            INSERT INTO paper_limit_orders
            (strategy_id, condition_id, market_title, favorite_token_id,
             favorite_token_label, favorite_price_at_entry, bid_price,
             dip_pct, placed_at, updated_at)
            VALUES (1, 'cond_1', 'Test Match', 'tok_1', 'Player A',
                    0.70, 0.49, 0.30, '2026-04-06T12:00:00Z', '2026-04-06T12:00:00Z')
        """)
        conn.commit()
        # Second insert with same (strategy_id, condition_id) should fail
        try:
            conn.execute("""
                INSERT INTO paper_limit_orders
                (strategy_id, condition_id, market_title, favorite_token_id,
                 favorite_token_label, favorite_price_at_entry, bid_price,
                 dip_pct, placed_at, updated_at)
                VALUES (1, 'cond_1', 'Test Match 2', 'tok_2', 'Player B',
                        0.60, 0.42, 0.30, '2026-04-06T13:00:00Z', '2026-04-06T13:00:00Z')
            """)
            conn.commit()
            assert False, "Should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_fills_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_fills")
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_events_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_events")
    finally:
        conn.close()
        os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_paper_limit_schema.py -v`
Expected: FAIL — `no such table: paper_limit_orders`

- [ ] **Step 3: Add DDL to db.py init_db()**

Find the `init_db()` function in `db.py` and add the three table definitions. The exact insertion point is after the existing `order_intents` / `order_fills` table creation.

```sql
CREATE TABLE IF NOT EXISTS paper_limit_orders (
    order_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id           INTEGER NOT NULL,
    condition_id          TEXT NOT NULL,
    market_title          TEXT NOT NULL,
    event_slug            TEXT NOT NULL DEFAULT '',
    favorite_token_id     TEXT NOT NULL,
    favorite_token_label  TEXT NOT NULL,
    favorite_price_at_entry REAL NOT NULL,
    bid_price             REAL NOT NULL,
    dip_pct               REAL NOT NULL,
    min_fill_size         REAL NOT NULL DEFAULT 50,
    state                 TEXT NOT NULL DEFAULT 'resting'
        CHECK (state IN ('resting', 'filled', 'cancelled', 'expired')),
    cancel_reason         TEXT NOT NULL DEFAULT '',
    placed_at             TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    closed_at             TEXT NOT NULL DEFAULT '',
    espn_event_id         TEXT NOT NULL DEFAULT '',
    tour                  TEXT NOT NULL DEFAULT '',
    player1               TEXT NOT NULL DEFAULT '',
    player2               TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_limit_orders_strategy_match
    ON paper_limit_orders(strategy_id, condition_id);

CREATE TABLE IF NOT EXISTS paper_limit_fills (
    fill_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER NOT NULL REFERENCES paper_limit_orders(order_id),
    fill_label        TEXT NOT NULL CHECK (fill_label IN ('strict', 'loose')),
    trade_price       REAL NOT NULL,
    trade_size        REAL NOT NULL,
    trade_side        TEXT NOT NULL DEFAULT '',
    trade_token_id    TEXT NOT NULL DEFAULT '',
    trade_tx_hash     TEXT NOT NULL DEFAULT '',
    trade_ts          TEXT NOT NULL,
    bid_price         REAL NOT NULL,
    slippage          REAL NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_limit_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER,
    event_type        TEXT NOT NULL,
    state_before      TEXT NOT NULL DEFAULT '',
    state_after       TEXT NOT NULL DEFAULT '',
    details_json      TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_paper_limit_schema.py -v`
Expected: 4 passed

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -x -q 2>&1 | tail -5`
Expected: all existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_paper_limit_schema.py
git commit -m "schema: paper_limit_orders + fills + events tables for stink bid"
```

---

## Task 2: ESPN Score Feed

**Files:**
- Create: `feeds/tennis_scores.py`
- Create: `tests/test_tennis_scores.py`

### Design decisions

Standalone module. Polls ESPN ATP/WTA scoreboard endpoints. Parses the nested response: `events → groupings → competitions → competitors`. Matches by surname + tour + date. Returns `MatchState` or `None`. Caches ESPN event IDs after first match.

The module has no DB dependency — pure HTTP + parsing.

- [ ] **Step 1: Write the test with mocked ESPN responses**

```python
# tests/test_tennis_scores.py
import json
from feeds.tennis_scores import (
    MatchState,
    parse_scoreboard,
    find_match,
    get_sets_completed,
)


def _make_competition(
    player1: str, player2: str,
    linescores1: list | None = None,
    linescores2: list | None = None,
    status: str = "In Progress",
):
    """Build a minimal ESPN competition dict."""
    comp = {
        "competitors": [
            {
                "athlete": {"displayName": player1},
                "linescores": [{"value": s} for s in (linescores1 or [])],
            },
            {
                "athlete": {"displayName": player2},
                "linescores": [{"value": s} for s in (linescores2 or [])],
            },
        ],
        "status": {"type": {"description": status}},
    }
    return comp


def _make_scoreboard(competitions: list[dict]):
    """Wrap competitions in ESPN scoreboard structure."""
    return {
        "events": [
            {
                "groupings": [
                    {"competitions": competitions}
                ]
            }
        ]
    }


def test_parse_scoreboard_extracts_competitions():
    comps = [_make_competition("Iga Swiatek", "Aryna Sabalenka")]
    data = _make_scoreboard(comps)
    result = parse_scoreboard(data)
    assert len(result) == 1
    assert result[0]["player1"] == "Iga Swiatek"
    assert result[0]["player2"] == "Aryna Sabalenka"


def test_find_match_by_surname():
    comps = [
        _make_competition("Iga Swiatek", "Aryna Sabalenka", [6, 3], [4, 6]),
        _make_competition("Carlos Alcaraz", "Jannik Sinner"),
    ]
    data = _make_scoreboard(comps)
    parsed = parse_scoreboard(data)

    match = find_match(parsed, "Swiatek", "Sabalenka")
    assert match is not None
    assert match["player1"] == "Iga Swiatek"

    # Order shouldn't matter
    match2 = find_match(parsed, "Sabalenka", "Swiatek")
    assert match2 is not None

    # No match
    assert find_match(parsed, "Djokovic", "Nadal") is None


def test_get_sets_completed_zero():
    comp = _make_competition("A Player", "B Player")
    state = get_sets_completed(comp)
    assert state.sets_completed == 0
    assert state.match_status == "In Progress"


def test_get_sets_completed_one():
    comp = _make_competition("A Player", "B Player", [6], [4])
    state = get_sets_completed(comp)
    assert state.sets_completed == 1


def test_get_sets_completed_two():
    comp = _make_competition("A Player", "B Player", [6, 3], [4, 6])
    state = get_sets_completed(comp)
    assert state.sets_completed == 2
    assert state.score_by_set == [(6, 4), (3, 6)]


def test_get_sets_completed_final():
    comp = _make_competition(
        "A Player", "B Player", [6, 3, 6], [4, 6, 2], status="Final"
    )
    state = get_sets_completed(comp)
    assert state.sets_completed == 3
    assert state.match_status == "Final"


def test_find_match_case_insensitive():
    comps = [_make_competition("Iga SWIATEK", "aryna sabalenka")]
    parsed = parse_scoreboard(_make_scoreboard(comps))
    match = find_match(parsed, "swiatek", "SABALENKA")
    assert match is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tennis_scores.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'feeds.tennis_scores'`

- [ ] **Step 3: Implement feeds/tennis_scores.py**

```python
# feeds/tennis_scores.py
"""ESPN tennis score feed — poll ATP/WTA scoreboards for live set scores."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

LOG = logging.getLogger(__name__)

ESPN_URLS = {
    "atp": "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
    "wta": "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
}


@dataclass
class MatchState:
    sets_completed: int
    score_by_set: list[tuple[int, int]]
    match_status: str  # scheduled | live | final | postponed | suspended
    source: str = ""   # "espn_atp" | "espn_wta"


def parse_scoreboard(data: dict) -> list[dict]:
    """Extract flattened match dicts from ESPN scoreboard JSON.

    ESPN structure: events[] → groupings[] → competitions[] → competitors[].
    Returns a list of dicts with: player1, player2, competition (raw dict).
    """
    matches = []
    for event in data.get("events", []):
        for grouping in event.get("groupings", []):
            for comp in grouping.get("competitions", []):
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                p1 = competitors[0].get("athlete", {}).get("displayName", "")
                p2 = competitors[1].get("athlete", {}).get("displayName", "")
                if p1 and p2:
                    matches.append({
                        "player1": p1,
                        "player2": p2,
                        "competition": comp,
                    })
    return matches


def _surname(name: str) -> str:
    """Extract surname (last word) from a display name."""
    parts = name.strip().split()
    return parts[-1].lower() if parts else ""


def find_match(
    parsed: list[dict],
    surname1: str,
    surname2: str,
) -> Optional[dict]:
    """Find a match by both player surnames (case-insensitive, order-independent)."""
    s1 = surname1.lower()
    s2 = surname2.lower()
    for m in parsed:
        names = {_surname(m["player1"]), _surname(m["player2"])}
        if s1 in names and s2 in names:
            return m
    return None


def get_sets_completed(competition: dict) -> MatchState:
    """Extract MatchState from a single ESPN competition dict."""
    competitors = competition.get("competitors", [])
    status_desc = (
        competition.get("status", {}).get("type", {}).get("description", "")
    )

    if len(competitors) < 2:
        return MatchState(
            sets_completed=0, score_by_set=[], match_status=status_desc or "unknown"
        )

    ls1 = [s.get("value", 0) for s in competitors[0].get("linescores", [])]
    ls2 = [s.get("value", 0) for s in competitors[1].get("linescores", [])]

    # sets_completed = number of sets where BOTH players have a score
    completed = min(len(ls1), len(ls2))
    score_by_set = [(int(ls1[i]), int(ls2[i])) for i in range(completed)]

    # Map ESPN status to our status vocabulary
    status_map = {
        "In Progress": "live",
        "Final": "final",
        "Scheduled": "scheduled",
        "Postponed": "postponed",
        "Suspended": "suspended",
    }
    match_status = status_map.get(status_desc, status_desc.lower() if status_desc else "unknown")

    return MatchState(
        sets_completed=completed,
        score_by_set=score_by_set,
        match_status=match_status,
    )


class EspnOutageTracker:
    """Tracks consecutive ESPN poll failures for outage detection."""

    def __init__(self, pause_after: int = 3, cancel_after_min: int = 10):
        self.consecutive_failures: int = 0
        self.first_failure_ts: float | None = None
        self.pause_after = pause_after
        self.cancel_after_sec = cancel_after_min * 60

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.first_failure_ts = None

    def record_failure(self) -> None:
        import time
        self.consecutive_failures += 1
        if self.first_failure_ts is None:
            self.first_failure_ts = time.time()

    @property
    def should_pause_new_orders(self) -> bool:
        return self.consecutive_failures >= self.pause_after

    @property
    def should_cancel_all(self) -> bool:
        if self.first_failure_ts is None:
            return False
        import time
        return (time.time() - self.first_failure_ts) >= self.cancel_after_sec


def fetch_scoreboard(tour: str, timeout: int = 10) -> Optional[dict]:
    """Fetch live scoreboard from ESPN. Returns parsed JSON or None on error."""
    url = ESPN_URLS.get(tour)
    if not url:
        LOG.warning("Unknown tour: %s", tour)
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        LOG.warning("ESPN fetch failed for %s: %s", tour, e)
        return None


def get_match_state(
    tour: str,
    player1: str,
    player2: str,
    event_date: str | None = None,
) -> MatchState | None:
    """Fetch ESPN scoreboard and find match state for two players.

    Args:
        tour: "atp" or "wta"
        player1: surname of player 1
        player2: surname of player 2
        event_date: not used in v1 (ESPN scoreboard is current-day only)

    Returns:
        MatchState or None if match not found or ESPN unavailable.
    """
    data = fetch_scoreboard(tour)
    if data is None:
        return None

    parsed = parse_scoreboard(data)
    match = find_match(parsed, player1, player2)
    if match is None:
        return None

    state = get_sets_completed(match["competition"])
    state.source = f"espn_{tour}"
    return state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tennis_scores.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add feeds/tennis_scores.py tests/test_tennis_scores.py
git commit -m "feat: ESPN tennis score feed — MatchState from live scoreboards"
```

---

## Task 3: Polymarket Trade Tap

**Files:**
- Create: `feeds/polymarket_trade_tap.py`
- Create: `tests/test_trade_tap.py`

### Design decisions

A lightweight async WS client that subscribes to `last_trade_price` for a dynamic set of token_ids. Emits `TradeTick` dataclass events. The watchlist is updated via `watch(token_id)` / `unwatch(token_id)`. Uses the same WS protocol as `stream_low_buys.py` but with no filtering — every trade print is emitted.

For testing, we test the parsing/callback logic without live WS connections.

- [ ] **Step 1: Write the test**

```python
# tests/test_trade_tap.py
from feeds.polymarket_trade_tap import TradeTick, parse_trade_message, TradeTapState


def test_parse_valid_trade():
    msg = [
        {
            "event_type": "last_trade_price",
            "asset_id": "tok_abc",
            "market": "cond_123",
            "price": "0.45",
            "size": "100",
            "side": "SELL",
            "timestamp_ms": 1712000000000,
        }
    ]
    ticks = parse_trade_message(msg)
    assert len(ticks) == 1
    assert ticks[0].token_id == "tok_abc"
    assert ticks[0].price == 0.45
    assert ticks[0].size == 100.0
    assert ticks[0].side == "SELL"
    assert ticks[0].condition_id == "cond_123"


def test_parse_ignores_non_trade_events():
    msg = [{"event_type": "price_change", "asset_id": "tok_abc"}]
    ticks = parse_trade_message(msg)
    assert len(ticks) == 0


def test_parse_handles_missing_fields():
    msg = [{"event_type": "last_trade_price"}]
    ticks = parse_trade_message(msg)
    assert len(ticks) == 0


def test_state_watch_unwatch():
    state = TradeTapState()
    state.watch("tok_1")
    state.watch("tok_2")
    assert state.watched_tokens == {"tok_1", "tok_2"}

    state.unwatch("tok_1")
    assert state.watched_tokens == {"tok_2"}


def test_state_filters_ticks():
    state = TradeTapState()
    state.watch("tok_1")

    tick_watched = TradeTick(
        token_id="tok_1", condition_id="c1", price=0.50,
        size=100, side="SELL", timestamp_ms=0,
    )
    tick_unwatched = TradeTick(
        token_id="tok_2", condition_id="c2", price=0.50,
        size=100, side="SELL", timestamp_ms=0,
    )
    assert state.is_watched(tick_watched.token_id) is True
    assert state.is_watched(tick_unwatched.token_id) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trade_tap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'feeds.polymarket_trade_tap'`

- [ ] **Step 3: Implement feeds/polymarket_trade_tap.py**

```python
# feeds/polymarket_trade_tap.py
"""Lightweight WS trade feed for watched Polymarket tokens.

Subscribes to `last_trade_price` for a dynamic set of token_ids.
Emits every trade print — no filtering by size, price, or wallet.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

LOG = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(slots=True)
class TradeTick:
    """A single trade print from the WS stream."""
    token_id: str
    condition_id: str
    price: float
    size: float
    side: str
    timestamp_ms: int


class TradeTapState:
    """Tracks which tokens are being watched."""

    def __init__(self):
        self.watched_tokens: set[str] = set()

    def watch(self, token_id: str) -> None:
        self.watched_tokens.add(token_id)

    def unwatch(self, token_id: str) -> None:
        self.watched_tokens.discard(token_id)

    def is_watched(self, token_id: str) -> bool:
        return token_id in self.watched_tokens


def parse_trade_message(msg: list[dict]) -> list[TradeTick]:
    """Parse a WS message array into TradeTick objects.

    Only processes `last_trade_price` events. Skips malformed entries.
    """
    ticks = []
    for item in msg:
        if item.get("event_type") != "last_trade_price":
            continue
        try:
            ticks.append(TradeTick(
                token_id=str(item["asset_id"]),
                condition_id=str(item.get("market", "")),
                price=float(item["price"]),
                size=float(item["size"]),
                side=str(item.get("side", "")),
                timestamp_ms=int(item.get("timestamp_ms", 0)),
            ))
        except (KeyError, ValueError, TypeError) as e:
            LOG.debug("Skipping malformed trade message: %s", e)
    return ticks


async def run_trade_tap(
    state: TradeTapState,
    on_tick: Callable[[TradeTick], None],
    reconnect_delay: float = 5.0,
    max_reconnect_delay: float = 120.0,
) -> None:
    """Run the trade tap WS loop. Subscribes to watched tokens, emits ticks.

    Reconnects on disconnect with exponential backoff.
    Resubscribes when the watchlist changes.
    """
    import websockets

    delay = reconnect_delay
    last_watchlist: set[str] = set()

    while True:
        tokens = state.watched_tokens.copy()
        if not tokens:
            await asyncio.sleep(5)
            continue

        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=30,
                ping_timeout=60,
                close_timeout=10,
            ) as ws:
                # NOTE: Polymarket uses "assets_ids" (plural). "asset_ids" delivers zero data.
                await ws.send(json.dumps({
                    "type": "market",
                    "assets_ids": list(tokens),
                }))
                last_watchlist = tokens.copy()
                delay = reconnect_delay
                LOG.info("TradeTap subscribed to %d tokens", len(tokens))

                async for raw in ws:
                    # Check if watchlist changed — resubscribe if needed
                    current = state.watched_tokens.copy()
                    if current != last_watchlist:
                        if current:
                            await ws.send(json.dumps({
                                "type": "market",
                                "assets_ids": list(current),
                            }))
                            last_watchlist = current.copy()
                            LOG.info("TradeTap resubscribed: %d tokens", len(current))
                        else:
                            break  # No tokens to watch

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(data, dict):
                        data = [data]
                    if not isinstance(data, list):
                        continue

                    for tick in parse_trade_message(data):
                        if state.is_watched(tick.token_id):
                            on_tick(tick)

        except Exception as e:
            LOG.warning("TradeTap disconnected: %s (reconnecting in %.0fs)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_reconnect_delay)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_trade_tap.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add feeds/polymarket_trade_tap.py tests/test_trade_tap.py
git commit -m "feat: polymarket trade tap — lightweight WS feed for watched tokens"
```

---

## Task 4: Paper Limit Engine — Core Order Lifecycle

**Files:**
- Create: `paper_limit_engine.py`
- Create: `tests/test_paper_limit_engine.py`

### Design decisions

The engine manages the full lifecycle: place → reprice → fill-check → cancel → expire. It writes to `paper_limit_orders`, `paper_limit_fills`, and `paper_limit_events` tables. On strict fill, it directly inserts into `strategy_positions` — the normal `_record_plugin_entry` flow is for synchronous taker entries; maker fills are async and don't go through discover→enter.

The fill check receives `TradeTick` objects from the trade tap and evaluates them against resting bids.

- [ ] **Step 1: Write the core tests**

```python
# tests/test_paper_limit_engine.py
import sqlite3
import tempfile
import os
import json

from paper_limit_engine import (
    PaperLimitEngine,
    compute_bid_price,
)


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from db import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, path


def test_compute_bid_price_basic():
    assert compute_bid_price(0.70, dip_pct=0.30, min_bid=0.05) == 0.49


def test_compute_bid_price_rounds():
    # 0.55 * 0.70 = 0.385 → rounds to 0.39
    assert compute_bid_price(0.55, dip_pct=0.30, min_bid=0.05) == 0.39


def test_compute_bid_price_floor():
    # 0.10 * 0.70 = 0.07, but min_bid is 0.05 so no floor needed
    assert compute_bid_price(0.10, dip_pct=0.30, min_bid=0.05) == 0.07
    # 0.05 * 0.70 = 0.035 → rounds to 0.04, below floor
    assert compute_bid_price(0.05, dip_pct=0.30, min_bid=0.05) == 0.05


def test_place_order():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = engine.place_order(
            strategy_id=1,
            condition_id="cond_1",
            market_title="Swiatek vs Sabalenka",
            favorite_token_id="tok_swiatek",
            favorite_token_label="Swiatek",
            favorite_price=0.70,
            dip_pct=0.30,
            min_bid_price=0.05,
            tour="wta",
            player1="Swiatek",
            player2="Sabalenka",
        )
        assert order_id > 0

        row = conn.execute(
            "SELECT * FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["state"] == "resting"
        assert row["bid_price"] == 0.49
        assert row["favorite_price_at_entry"] == 0.70

        # Event logged
        events = conn.execute(
            "SELECT * FROM paper_limit_events WHERE order_id = ?", (order_id,)
        ).fetchall()
        assert len(events) == 1
        assert events[0]["event_type"] == "order_placed"
    finally:
        conn.close()
        os.unlink(path)


def test_place_order_duplicate_rejected():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
        )
        # Second order for same match → returns None
        order_id = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match 2",
            favorite_token_id="tok_2", favorite_token_label="P2",
            favorite_price=0.60, dip_pct=0.30, min_bid_price=0.05,
        )
        assert order_id is None
    finally:
        conn.close()
        os.unlink(path)


def test_cancel_order():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
        )
        engine.cancel_order(oid, reason="sets_completed")

        row = conn.execute(
            "SELECT state, cancel_reason FROM paper_limit_orders WHERE order_id = ?",
            (oid,),
        ).fetchone()
        assert row["state"] == "cancelled"
        assert row["cancel_reason"] == "sets_completed"
    finally:
        conn.close()
        os.unlink(path)


def test_reprice_order():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
        )
        # Favorite moved to 0.80 → new bid = 0.56 (diff = 0.07 > threshold)
        repriced = engine.reprice_order(oid, new_favorite_price=0.80, threshold=0.02)
        assert repriced is True
        row = conn.execute(
            "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?", (oid,),
        ).fetchone()
        assert row["bid_price"] == 0.56
    finally:
        conn.close()
        os.unlink(path)


def test_reprice_below_threshold_skipped():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
        )
        # Favorite moved to 0.71 → new bid = 0.497 → rounds to 0.50
        # diff from 0.49 = 0.01 < threshold 0.02 → skip
        repriced = engine.reprice_order(oid, new_favorite_price=0.71, threshold=0.02)
        assert repriced is False
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_strict():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
            min_fill_size=50,
        )
        from feeds.polymarket_trade_tap import TradeTick
        tick = TradeTick(
            token_id="tok_1", condition_id="cond_1",
            price=0.45, size=100, side="SELL", timestamp_ms=1712000000000,
        )
        result = engine.check_fill(oid, tick, position_size_usdc=25.0)
        assert result == "strict"

        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (oid,),
        ).fetchone()
        assert row["state"] == "filled"

        fills = conn.execute(
            "SELECT * FROM paper_limit_fills WHERE order_id = ?", (oid,),
        ).fetchall()
        # Both strict and loose should be logged
        labels = {f["fill_label"] for f in fills}
        assert "strict" in labels
        assert "loose" in labels
        # Slippage = bid_price - fill_price = 0.49 - 0.45 = 0.04
        strict_fill = [f for f in fills if f["fill_label"] == "strict"][0]
        assert abs(strict_fill["slippage"] - 0.04) < 0.001

        # Verify strategy_positions row was created
        pos = conn.execute(
            "SELECT * FROM strategy_positions WHERE condition_id = 'cond_1'"
        ).fetchone()
        assert pos is not None
        assert pos["state"] == "open"
        assert pos["entry_price"] == 0.45
        assert pos["current_price"] == 0.45  # Set to entry price initially
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_loose_only():
    """Trade at bid price but size below min → loose only, order stays resting."""
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
            min_fill_size=50,
        )
        from feeds.polymarket_trade_tap import TradeTick
        tick = TradeTick(
            token_id="tok_1", condition_id="cond_1",
            price=0.48, size=10, side="SELL", timestamp_ms=1712000000000,
        )
        result = engine.check_fill(oid, tick, position_size_usdc=25.0)
        assert result == "loose"

        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (oid,),
        ).fetchone()
        assert row["state"] == "resting"  # Still resting — loose doesn't fill

        fills = conn.execute(
            "SELECT * FROM paper_limit_fills WHERE order_id = ?", (oid,),
        ).fetchall()
        assert len(fills) == 1
        assert fills[0]["fill_label"] == "loose"
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_price_above_bid():
    """Trade price above bid → no fill at all."""
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        oid = engine.place_order(
            strategy_id=1, condition_id="cond_1", market_title="Match",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
            min_fill_size=50,
        )
        from feeds.polymarket_trade_tap import TradeTick
        tick = TradeTick(
            token_id="tok_1", condition_id="cond_1",
            price=0.55, size=200, side="SELL", timestamp_ms=1712000000000,
        )
        result = engine.check_fill(oid, tick, position_size_usdc=25.0)
        assert result is None
    finally:
        conn.close()
        os.unlink(path)


def test_log_skip():
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        engine.log_skip(
            strategy_id=1,
            condition_id="cond_1",
            reason="no_score_feed",
            details={"market_title": "Some Match"},
        )
        events = conn.execute(
            "SELECT * FROM paper_limit_events WHERE event_type = 'order_skipped'"
        ).fetchall()
        assert len(events) == 1
        d = json.loads(events[0]["details_json"])
        assert d["reason"] == "no_score_feed"
    finally:
        conn.close()
        os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_paper_limit_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'paper_limit_engine'`

- [ ] **Step 3: Implement paper_limit_engine.py**

```python
# paper_limit_engine.py
"""Paper limit order engine — place, reprice, fill-check, cancel, expire."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from feeds.polymarket_trade_tap import TradeTick

LOG = logging.getLogger(__name__)


def compute_bid_price(
    favorite_price: float,
    dip_pct: float = 0.30,
    min_bid: float = 0.05,
) -> float:
    """Compute bid price: favorite_price * (1 - dip_pct), tick-rounded, floored."""
    raw = favorite_price * (1 - dip_pct)
    rounded = round(raw, 2)
    return max(rounded, min_bid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PaperLimitEngine:
    """Manages paper limit order lifecycle."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _log_event(
        self,
        order_id: int | None,
        event_type: str,
        state_before: str = "",
        state_after: str = "",
        details: dict | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO paper_limit_events
               (order_id, event_type, state_before, state_after, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, event_type, state_before, state_after,
             json.dumps(details or {}), _now_iso()),
        )
        self.conn.commit()

    def place_order(
        self,
        strategy_id: int,
        condition_id: str,
        market_title: str,
        favorite_token_id: str,
        favorite_token_label: str,
        favorite_price: float,
        dip_pct: float = 0.30,
        min_bid_price: float = 0.05,
        min_fill_size: float = 50,
        event_slug: str = "",
        tour: str = "",
        player1: str = "",
        player2: str = "",
        espn_event_id: str = "",
        max_resting: int = 3,
        max_daily_fills: int = 5,
    ) -> int | None:
        """Place a new paper limit order. Returns order_id or None if duplicate or rail hit."""
        # Safety rail: max resting orders
        if self.count_resting(strategy_id) >= max_resting:
            self.log_skip(strategy_id, condition_id, "max_resting_reached")
            return None
        # Safety rail: max daily fills
        if self.count_daily_fills(strategy_id) >= max_daily_fills:
            self.log_skip(strategy_id, condition_id, "max_daily_fills_reached")
            return None

        bid = compute_bid_price(favorite_price, dip_pct, min_bid_price)
        now = _now_iso()

        try:
            cur = self.conn.execute(
                """INSERT INTO paper_limit_orders
                   (strategy_id, condition_id, market_title, event_slug,
                    favorite_token_id, favorite_token_label, favorite_price_at_entry,
                    bid_price, dip_pct, min_fill_size, state, placed_at, updated_at,
                    espn_event_id, tour, player1, player2)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'resting', ?, ?, ?, ?, ?, ?)""",
                (strategy_id, condition_id, market_title, event_slug,
                 favorite_token_id, favorite_token_label, favorite_price,
                 bid, dip_pct, min_fill_size, now, now,
                 espn_event_id, tour, player1, player2),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            LOG.info("Duplicate order for strategy=%s condition=%s", strategy_id, condition_id)
            return None

        order_id = cur.lastrowid
        self._log_event(order_id, "order_placed", "", "resting", {
            "market_title": market_title,
            "favorite_token_id": favorite_token_id,
            "favorite_token_label": favorite_token_label,
            "favorite_price": favorite_price,
            "bid_price": bid,
            "dip_pct": dip_pct,
            "tour": tour,
            "player1": player1,
            "player2": player2,
        })
        return order_id

    def cancel_order(self, order_id: int, reason: str) -> None:
        """Cancel a resting order."""
        now = _now_iso()
        self.conn.execute(
            """UPDATE paper_limit_orders
               SET state = 'cancelled', cancel_reason = ?, closed_at = ?, updated_at = ?
               WHERE order_id = ? AND state = 'resting'""",
            (reason, now, now, order_id),
        )
        self.conn.commit()
        self._log_event(order_id, "order_cancelled", "resting", "cancelled", {"reason": reason})

    def expire_order(self, order_id: int, reason: str) -> None:
        """Expire a resting order (market resolved/closed)."""
        now = _now_iso()
        self.conn.execute(
            """UPDATE paper_limit_orders
               SET state = 'expired', cancel_reason = ?, closed_at = ?, updated_at = ?
               WHERE order_id = ? AND state = 'resting'""",
            (reason, now, now, order_id),
        )
        self.conn.commit()
        self._log_event(order_id, "order_expired", "resting", "expired", {"reason": reason})

    def reprice_order(
        self,
        order_id: int,
        new_favorite_price: float,
        threshold: float = 0.02,
        dip_pct: float | None = None,
        min_bid_price: float = 0.05,
    ) -> bool:
        """Reprice a resting order if the new bid differs materially. Returns True if repriced."""
        row = self.conn.execute(
            "SELECT bid_price, dip_pct, state FROM paper_limit_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None or row["state"] != "resting":
            return False

        effective_dip = dip_pct if dip_pct is not None else row["dip_pct"]
        new_bid = compute_bid_price(new_favorite_price, effective_dip, min_bid_price)
        old_bid = row["bid_price"]
        diff = abs(new_bid - old_bid)

        # Reprice only if diff exceeds threshold — use whichever is smaller
        # (absolute or 5% relative) per spec §1 Refresh/Reprice
        relative_threshold = old_bid * 0.05
        effective_threshold = min(threshold, relative_threshold)
        if diff < effective_threshold:
            return False

        now = _now_iso()
        self.conn.execute(
            """UPDATE paper_limit_orders SET bid_price = ?, updated_at = ?
               WHERE order_id = ? AND state = 'resting'""",
            (new_bid, now, order_id),
        )
        self.conn.commit()
        self._log_event(order_id, "order_repriced", "resting", "resting", {
            "old_bid": old_bid,
            "new_bid": new_bid,
            "favorite_price_now": new_favorite_price,
        })
        return True

    def check_fill(self, order_id: int, tick: TradeTick, position_size_usdc: float = 25.0) -> str | None:
        """Check if a trade tick fills a resting order.

        On strict fill, creates a strategy_positions row with the fill price.

        Returns:
            "strict" if strict fill triggered (order transitions to filled)
            "loose" if loose fill triggered (order stays resting, logged)
            None if no fill
        """
        row = self.conn.execute(
            """SELECT * FROM paper_limit_orders WHERE order_id = ?""",
            (order_id,),
        ).fetchone()
        if row is None or row["state"] != "resting":
            return None

        if tick.token_id != row["favorite_token_id"]:
            return None

        bid = row["bid_price"]
        if tick.price > bid:
            return None

        now = _now_iso()
        slippage = round(bid - tick.price, 6)

        # Check strict first
        is_strict = tick.size >= row["min_fill_size"]

        if is_strict:
            # Log both strict and loose fills
            for label in ("strict", "loose"):
                self.conn.execute(
                    """INSERT INTO paper_limit_fills
                       (order_id, fill_label, trade_price, trade_size, trade_side,
                        trade_token_id, trade_tx_hash, trade_ts, bid_price, slippage,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)""",
                    (order_id, label, tick.price, tick.size, tick.side,
                     tick.token_id, str(tick.timestamp_ms), bid, slippage, now),
                )
            # Transition to filled
            self.conn.execute(
                """UPDATE paper_limit_orders
                   SET state = 'filled', closed_at = ?, updated_at = ?
                   WHERE order_id = ?""",
                (now, now, order_id),
            )
            self.conn.commit()
            self._log_event(order_id, "fill_strict", "resting", "filled", {
                "trade_price": tick.price,
                "trade_size": tick.size,
                "trade_side": tick.side,
                "bid_price": bid,
                "slippage": slippage,
            })
            # Bridge: create strategy_positions row
            if position_size_usdc > 0:
                self._create_position_from_fill(
                    order_id=order_id,
                    strategy_id=row["strategy_id"],
                    condition_id=row["condition_id"],
                    outcome=row["favorite_token_label"],
                    token_id=row["favorite_token_id"],
                    entry_price=tick.price,
                    size_usdc=position_size_usdc,
                )
            return "strict"
        else:
            # Loose fill only — order stays resting
            # Check if loose already logged for this order (once per order for strict,
            # but loose can log multiple sensitivity observations)
            self.conn.execute(
                """INSERT INTO paper_limit_fills
                   (order_id, fill_label, trade_price, trade_size, trade_side,
                    trade_token_id, trade_tx_hash, trade_ts, bid_price, slippage,
                    created_at)
                   VALUES (?, 'loose', ?, ?, ?, ?, '', ?, ?, ?, ?)""",
                (order_id, tick.price, tick.size, tick.side,
                 tick.token_id, str(tick.timestamp_ms), bid, slippage, now),
            )
            self.conn.commit()
            self._log_event(order_id, "fill_loose", "resting", "resting", {
                "trade_price": tick.price,
                "trade_size": tick.size,
                "trade_side": tick.side,
                "bid_price": bid,
                "slippage": slippage,
            })
            return "loose"

    def _create_position_from_fill(
        self,
        order_id: int,
        strategy_id: int,
        condition_id: str,
        outcome: str,
        token_id: str,
        entry_price: float,
        size_usdc: float,
    ) -> int | None:
        """Create a strategy_positions row from a strict paper fill."""
        import time
        now_ts = int(time.time())
        shares = size_usdc / entry_price if entry_price > 0 else 0

        # Use synthetic wallet to avoid collision with other empty-wallet
        # paper strategies on the same market. The unique index is on
        # (wallet, condition_id, outcome) WHERE state IN (...).
        synthetic_wallet = f"paper:stink_bid:{strategy_id}"

        try:
            cur = self.conn.execute("""
                INSERT INTO strategy_positions
                (wallet, condition_id, outcome, strategy_id, state,
                 token_id, entry_price, avg_entry_price, entry_shares,
                 filled_entry_shares, realized_pnl, opened_ts, closed_ts,
                 last_reconciled_ts, version, updated_ts, instance_tag,
                 size_usdc, current_price)
                VALUES (?, ?, ?, ?, 'open',
                        ?, ?, ?, ?,
                        ?, 0, ?, 0,
                        ?, 1, ?, 'stink-bid-paper',
                        ?, ?)
            """, (synthetic_wallet, condition_id, outcome, str(strategy_id),
                  token_id, entry_price, entry_price, shares,
                  shares, now_ts, now_ts, now_ts, size_usdc, entry_price))
            self.conn.commit()
            position_id = cur.lastrowid

            self._log_event(order_id, "position_opened", "filled", "filled", {
                "position_id": position_id,
                "entry_price": entry_price,
                "size_usdc": size_usdc,
                "shares": round(shares, 4),
            })
            return position_id
        except sqlite3.IntegrityError as e:
            LOG.warning("Position already exists for %s: %s", condition_id, e)
            return None

    def count_resting(self, strategy_id: int) -> int:
        """Count resting orders for a strategy."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE state = 'resting' AND strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        return row["n"] if row else 0

    def count_daily_fills(self, strategy_id: int) -> int:
        """Count fills today for a strategy."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            """SELECT COUNT(*) AS n FROM paper_limit_orders
               WHERE state = 'filled' AND strategy_id = ?
               AND closed_at LIKE ?""",
            (strategy_id, f"{today}%"),
        ).fetchone()
        return row["n"] if row else 0

    def get_resting_orders(self, strategy_id: int | None = None) -> list[dict]:
        """Get all resting orders, optionally filtered by strategy_id."""
        if strategy_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM paper_limit_orders WHERE state = 'resting' AND strategy_id = ?",
                (strategy_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM paper_limit_orders WHERE state = 'resting'"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_order_for_match(self, strategy_id: int, condition_id: str) -> bool:
        """Check if any order (any state) exists for this match."""
        row = self.conn.execute(
            "SELECT 1 FROM paper_limit_orders WHERE strategy_id = ? AND condition_id = ?",
            (strategy_id, condition_id),
        ).fetchone()
        return row is not None

    def log_skip(
        self,
        strategy_id: int,
        condition_id: str,
        reason: str,
        details: dict | None = None,
    ) -> None:
        """Log an order_skipped event."""
        d = {"reason": reason, "strategy_id": strategy_id, "condition_id": condition_id}
        if details:
            d.update(details)
        self._log_event(None, "order_skipped", "", "", d)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_paper_limit_engine.py -v`
Expected: 10 passed

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -x -q 2>&1 | tail -5`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add paper_limit_engine.py tests/test_paper_limit_engine.py
git commit -m "feat: paper limit engine — order lifecycle, dual-label fill detection"
```

---

## Task 5: Strategy Plugin — stink_bid_v1

**Files:**
- Create: `strategies/stink_bid_v1.py`
- Modify: `strategies/registry.py` (register plugin)
- Create: `tests/test_stink_bid_strategy.py`

### Design decisions

Follows the standard `StrategyPlugin` interface: `discover()`, `enter()`, `manage()`, `exit()`. But for this strategy, **resting order placement is handled by `stink_bid_runner.py`, not by `enter()`**. The plugin's job is narrower:

- provide shared tennis-market classification helpers used by the runner
- manage already-open `strategy_positions` created by strict fills
- exit only on resolution / stale-close conditions

The `strategy_positions` bridge on strict fill lives in `paper_limit_engine._create_position_from_fill()`, not in the plugin. This keeps the async maker-fill path separate from the normal synchronous discover→enter path used by taker strategies.

- [ ] **Step 1: Write the core tests**

```python
# tests/test_stink_bid_strategy.py
import sqlite3
import tempfile
import os
import json
from unittest.mock import patch, MagicMock

from strategies.stink_bid_v1 import StinkBidV1Strategy
from strategies.base import StrategySignal


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from db import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, path


def _make_strategy_row(strategy_id=99, params=None):
    """Mimics a real strategies table row dict."""
    return {
        "id": strategy_id,
        "strategy_type": "stink_bid_v1",
        "label": "Stink Bid Paper Test",
        "parameters": json.dumps(params or {}),
        "status": "active",
        "instance_tag": "stink-bid-paper",
    }


def test_plugin_metadata():
    plugin = StinkBidV1Strategy()
    assert plugin.plugin_id == "stink_bid_v1"
    assert "paper" in plugin.supported_execution_modes
    assert "real" not in plugin.supported_execution_modes


def test_discover_returns_empty_when_no_tennis_markets():
    """No tennis markets in market_metadata → empty signals."""
    conn, path = _make_db()
    try:
        plugin = StinkBidV1Strategy()
        strategy = _make_strategy_row()
        signals = plugin.discover(conn, strategy)
        assert signals == []
    finally:
        conn.close()
        os.unlink(path)


def test_qualify_market_basic():
    """A tennis match with favorite > threshold qualifies."""
    plugin = StinkBidV1Strategy()
    market = {
        "condition_id": "cond_tennis_1",
        "title": "Swiatek vs Sabalenka - WTA Finals",
        "outcomes": ["Swiatek", "Sabalenka"],
        "outcome_prices": [0.70, 0.30],
        "volume_24h": 50000,
        "end_date_iso": "2026-04-10T00:00:00Z",
        "is_open": True,
    }
    params = {"min_favorite_price": 0.55, "min_market_volume": 10000}
    result = plugin._qualify_market(market, params)
    assert result is not None
    assert result["favorite_label"] == "Swiatek"
    assert result["favorite_price"] == 0.70


def test_qualify_market_rejects_coin_flip():
    """Favorite below threshold → rejected."""
    plugin = StinkBidV1Strategy()
    market = {
        "condition_id": "cond_tennis_2",
        "title": "Player A vs Player B - ATP",
        "outcomes": ["Player A", "Player B"],
        "outcome_prices": [0.52, 0.48],
        "volume_24h": 50000,
        "end_date_iso": "2026-04-10T00:00:00Z",
        "is_open": True,
    }
    params = {"min_favorite_price": 0.55, "min_market_volume": 10000}
    result = plugin._qualify_market(market, params)
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stink_bid_strategy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.stink_bid_v1'`

- [ ] **Step 3: Implement strategies/stink_bid_v1.py**

```python
# strategies/stink_bid_v1.py
"""Stink Bid V1 — paper limit order strategy for tennis match-winner markets."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Optional

from strategies.base import (
    StrategyPlugin,
    StrategySignal,
    StrategyEntryDecision,
    StrategyManageState,
    StrategyExitDecision,
)

LOG = logging.getLogger(__name__)

# Patterns to identify tennis match-winner markets
_TENNIS_PATTERNS = [
    re.compile(r"\b(wta|atp|grand\s*slam|wimbledon|roland\s*garros|"
               r"australian\s*open|us\s*open|french\s*open)\b", re.I),
    re.compile(r"\bvs\.?\b.*\b(win|beat|defeat)\b", re.I),
]

_TOUR_PATTERN = re.compile(r"\b(wta)\b", re.I)


def _extract_tour(title: str) -> str:
    """Extract tour from market title. Default to 'atp' if WTA not found."""
    if _TOUR_PATTERN.search(title):
        return "wta"
    return "atp"


def _extract_player_surnames(title: str) -> tuple[str, str] | None:
    """Try to extract two player surnames from a 'X vs Y' title."""
    # Match "Player1 vs Player2" or "Player1 vs. Player2"
    m = re.search(r"([A-Z][a-z'-]+(?:\s[A-Z][a-z'-]+)*)\s+vs\.?\s+([A-Z][a-z'-]+(?:\s[A-Z][a-z'-]+)*)", title)
    if not m:
        return None
    name1 = m.group(1).strip().split()[-1]  # surname
    name2 = m.group(2).strip().split()[-1]
    return (name1, name2)


def _get_params(strategy: dict) -> dict:
    """Extract parameters with defaults from the real strategies row shape.

    The repo's strategies table stores JSON in `parameters`, not `parameters_json`.
    Accept both keys so the helper also works in isolated tests.
    """
    raw = strategy.get("parameters")
    if raw is None:
        raw = strategy.get("parameters_json", "{}")
    params = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return {
        "min_favorite_price": params.get("min_favorite_price", 0.55),
        "min_market_volume": params.get("min_market_volume", 10000),
        "min_bid_price": params.get("min_bid_price", 0.05),
        "dip_pct": params.get("dip_pct", 0.30),
        "min_fill_size": params.get("min_fill_size", 50),
        "reprice_threshold": params.get("reprice_threshold", 0.02),
        "max_resting_orders": params.get("max_resting_orders", 3),
        "max_daily_fills": params.get("max_daily_fills", 5),
        "max_position_size_usdc": params.get("max_position_size_usdc", 25),
    }


class StinkBidV1Strategy(StrategyPlugin):
    plugin_id = "stink_bid_v1"
    display_name = "Stink Bid V1 — Tennis Panic Dip"
    description = "Paper limit bid 30% below tennis match favorite, hold to resolution"
    strategy_family = "value"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket",)
    default_loop_seconds = 900  # 15 min refresh

    def _is_tennis_market(self, title: str) -> bool:
        """Check if a market title looks like a tennis match-winner market."""
        return any(p.search(title) for p in _TENNIS_PATTERNS)

    def _qualify_market(self, market: dict, params: dict) -> dict | None:
        """Check if a market qualifies for stink bid. Returns enriched dict or None."""
        if not market.get("is_open"):
            return None
        title = market.get("title", "")
        if not self._is_tennis_market(title):
            return None

        volume = market.get("volume_24h", 0)
        if volume < params["min_market_volume"]:
            return None

        prices = market.get("outcome_prices", [])
        outcomes = market.get("outcomes", [])
        if len(prices) < 2 or len(outcomes) < 2:
            return None

        # Find favorite: highest-priced outcome
        fav_idx = 0 if float(prices[0]) >= float(prices[1]) else 1
        fav_price = float(prices[fav_idx])
        fav_label = outcomes[fav_idx]

        if fav_price < params["min_favorite_price"]:
            return None

        return {
            "condition_id": market["condition_id"],
            "title": title,
            "favorite_label": fav_label,
            "favorite_price": fav_price,
            "favorite_idx": fav_idx,
            "volume": volume,
        }

    def discover(self, conn: sqlite3.Connection, strategy: dict) -> list[StrategySignal]:
        """Find tennis candidate markets for observability/reporting.

        The live resting-order placement path runs in `stink_bid_runner.py`.
        This discover() method is intentionally lightweight and returns
        candidate tennis markets so the plugin still participates cleanly in
        the strategy framework.
        """
        # Query market_metadata for active tennis markets
        rows = conn.execute("""
            SELECT condition_id, title, slug, category, volume_24h,
                   is_open, end_date_iso
            FROM market_metadata
            WHERE is_open = 1
        """).fetchall()

        params = _get_params(strategy)
        signals = []

        for row in rows:
            market = dict(row)
            title = market.get("title", "")
            if self._is_tennis_market(title):
                signals.append(StrategySignal(
                    wallet="",  # No wallet for this strategy
                    condition_id=market["condition_id"],
                    outcome="",  # Determined during enter
                    title=title,
                    current_price=0,  # Fetched during enter
                    position_value=0,
                    analyzed_ts=int(time.time()),
                    metadata={"category": market.get("category", "")},
                ))

        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: dict,
        signal: StrategySignal,
        open_positions: list,
    ) -> StrategyEntryDecision:
        """No-op entry path.

        Resting bids are placed asynchronously by `stink_bid_runner.py`.
        The standard strategy runner should never attempt a synchronous taker
        entry for this plugin.
        """
        return StrategyEntryDecision(
            should_enter=False,
            reason="stink_bid orders are placed by stink_bid_runner, not enter()",
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: dict,
        position: dict,
    ) -> StrategyManageState:
        """Manage open positions — check resolution, update price."""
        current_price = position.get("current_price", 0)
        return StrategyManageState(
            current_price=current_price,
            signal_open=True,
            metadata={},
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: dict,
        position: dict,
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        """Exit on resolution only — hold to expiration."""
        current_price = position.get("current_price", 0)
        # Resolution check
        if current_price >= 0.97:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_win",
                exit_price=current_price,
                status="closed",
            )
        if current_price <= 0.03:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_loss",
                exit_price=current_price,
                status="closed",
            )
        return StrategyExitDecision(should_exit=False, reason="holding")
```

- [ ] **Step 4: Register in strategies/registry.py**

Add to the `_PLUGINS` dict in `strategies/registry.py`:

```python
from strategies.stink_bid_v1 import StinkBidV1Strategy

# In the _PLUGINS dict:
"stink_bid_v1": StinkBidV1Strategy(),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stink_bid_strategy.py -v`
Expected: 4 passed

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -x -q 2>&1 | tail -5`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add strategies/stink_bid_v1.py strategies/registry.py tests/test_stink_bid_strategy.py
git commit -m "feat: stink_bid_v1 strategy plugin — tennis market discovery + paper limit lifecycle"
```

---

## Task 6: Runtime Wiring — Trade Tap → Fill Checker → Position Bridge

**Files:**
- Create: `stink_bid_runner.py`
- Create: `tests/test_stink_bid_runner.py`

### Design decisions

This is the runtime entrypoint that connects the trade tap to the fill checker. It runs as an async loop: the trade tap receives WS trade events, and for each tick, iterates over all resting paper bids and calls `check_fill()`. It also manages the ESPN outage tracker and the strategy's refresh cycle.

This runs as a separate process (systemd service), not inside `strategy_runner.py`. The strategy runner handles the discover/manage/exit lifecycle on its normal 15-min cycle. This runner handles the real-time fill detection + ESPN polling.

- [ ] **Step 1: Write the test**

```python
# tests/test_stink_bid_runner.py
import sqlite3
import tempfile
import os

from paper_limit_engine import PaperLimitEngine
from feeds.polymarket_trade_tap import TradeTick
from feeds.tennis_scores import EspnOutageTracker


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from db import init_db
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, path


def test_tick_dispatches_to_all_resting_orders():
    """A single tick is checked against all resting orders."""
    conn, path = _make_db()
    try:
        engine = PaperLimitEngine(conn)
        # Two resting orders for different matches, same token won't match both
        engine.place_order(
            strategy_id=1, condition_id="c1", market_title="M1",
            favorite_token_id="tok_1", favorite_token_label="P1",
            favorite_price=0.70, dip_pct=0.30, min_bid_price=0.05,
        )
        engine.place_order(
            strategy_id=1, condition_id="c2", market_title="M2",
            favorite_token_id="tok_2", favorite_token_label="P2",
            favorite_price=0.60, dip_pct=0.30, min_bid_price=0.05,
        )

        # Tick for tok_1 at fill price
        tick = TradeTick(
            token_id="tok_1", condition_id="c1",
            price=0.40, size=100, side="SELL", timestamp_ms=0,
        )

        resting = engine.get_resting_orders(strategy_id=1)
        results = []
        for order in resting:
            r = engine.check_fill(order["order_id"], tick, position_size_usdc=25.0)
            if r:
                results.append((order["condition_id"], r))

        # Only c1 should fill (tok_1 matches), not c2
        assert len(results) == 1
        assert results[0] == ("c1", "strict")
    finally:
        conn.close()
        os.unlink(path)


def test_espn_outage_tracker_lifecycle():
    tracker = EspnOutageTracker(pause_after=3, cancel_after_min=1)

    assert tracker.should_pause_new_orders is False
    assert tracker.should_cancel_all is False

    tracker.record_failure()
    tracker.record_failure()
    assert tracker.should_pause_new_orders is False  # Only 2

    tracker.record_failure()
    assert tracker.should_pause_new_orders is True  # 3 now

    # Reset on success
    tracker.record_success()
    assert tracker.should_pause_new_orders is False
    assert tracker.should_cancel_all is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_stink_bid_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'stink_bid_runner'`

- [ ] **Step 3: Implement stink_bid_runner.py**

```python
# stink_bid_runner.py
"""Stink Bid runtime — trade tap fill detection + ESPN polling.

Runs as a separate async process. Connects trade tap to fill checker.
Strategy discovery/manage/exit runs in strategy_runner.py on its 15-min cycle.
This runner handles real-time fill detection and ESPN score polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

from feeds.polymarket_trade_tap import TradeTapState, TradeTick, run_trade_tap
from feeds.tennis_scores import EspnOutageTracker, get_match_state
from paper_limit_engine import PaperLimitEngine, compute_bid_price
from strategies.stink_bid_v1 import (
    StinkBidV1Strategy,
    _extract_player_surnames,
    _extract_tour,
)

LOG = logging.getLogger(__name__)


class StinkBidRunner:
    """Orchestrates the full stink bid lifecycle:
    - Discovers tennis markets, qualifies, places paper bids
    - Polls ESPN for set scores, cancels when sets_completed >= 2
    - Reprices bids on each refresh cycle
    - Manages trade tap → fill detection for resting orders
    - Handles ESPN outage → pause/cancel
    """

    def __init__(self, db_path: str, strategy_id: int, params: dict | None = None):
        self.db_path = db_path
        self.strategy_id = strategy_id
        self.params = params or {}
        self.plugin = StinkBidV1Strategy()
        self.tap_state = TradeTapState()
        self.outage_tracker = EspnOutageTracker(
            pause_after=3,
            cancel_after_min=self.params.get("espn_outage_cancel_min", 10),
        )
        self._position_size = self.params.get("max_position_size_usdc", 25.0)
        self._dip_pct = self.params.get("dip_pct", 0.30)
        self._min_favorite = self.params.get("min_favorite_price", 0.55)
        self._min_bid = self.params.get("min_bid_price", 0.05)
        self._min_fill_size = self.params.get("min_fill_size", 50)
        self._max_resting = self.params.get("max_resting_orders", 3)
        self._max_daily_fills = self.params.get("max_daily_fills", 5)
        self._reprice_threshold = self.params.get("reprice_threshold", 0.02)
        self._refresh_sec = self.params.get("refresh_interval_sec", 900)
        self._espn_poll_sec = self.params.get("espn_poll_interval_sec", 60)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _is_tennis_match_title(self, title: str) -> bool:
        """Reuse the plugin classifier so runner + plugin stay aligned."""
        return self.plugin._is_tennis_market(title)

    def _on_tick(self, tick: TradeTick) -> None:
        """Called for every trade event on watched tokens."""
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self.strategy_id)
            for order in resting:
                engine.check_fill(
                    order["order_id"], tick,
                    position_size_usdc=self._position_size,
                )
        finally:
            conn.close()

    def sync_watchlist(self) -> None:
        """Update trade tap watchlist from current resting orders."""
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self.strategy_id)
            current_tokens = {o["favorite_token_id"] for o in resting}
            for tid in current_tokens - self.tap_state.watched_tokens:
                self.tap_state.watch(tid)
            for tid in self.tap_state.watched_tokens - current_tokens:
                self.tap_state.unwatch(tid)
        finally:
            conn.close()

    def discover_and_place(self) -> None:
        """Discover qualifying tennis markets, place new paper bids.

        This is the ORDER PLACEMENT path — the only code that calls
        paper_limit_engine.place_order().
        """
        from polymarket_api import fetch_market_by_condition
        import requests

        if self.outage_tracker.should_pause_new_orders:
            LOG.info("ESPN outage — pausing new orders")
            return

        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)

            # Find active tennis match-winner markets
            rows = conn.execute("""
                SELECT condition_id, title, slug, event_slug, category,
                       volume_24h, is_open, end_date_iso
                FROM market_metadata WHERE is_open = 1
            """).fetchall()

            session = requests.Session()
            for row in rows:
                title = row["title"] or ""
                condition_id = row["condition_id"]

                # Tennis match-winner filter
                if not self._is_tennis_match_title(title):
                    continue
                if (row["volume_24h"] or 0) < self.params.get("min_market_volume", 10000):
                    continue

                # Already have an order for this match?
                if engine.has_order_for_match(self.strategy_id, condition_id):
                    continue

                # Resolve token IDs via existing Gamma API helper
                gamma = fetch_market_by_condition(session, condition_id)
                if not gamma:
                    engine.log_skip(self.strategy_id, condition_id, "gamma_lookup_failed")
                    continue

                import json as _json
                try:
                    outcomes = _json.loads(gamma.get("outcomes", "[]"))
                    token_ids = _json.loads(gamma.get("clobTokenIds", "[]"))
                    prices_raw = _json.loads(gamma.get("outcomePrices", "[]"))
                    prices = [float(p) for p in prices_raw]
                except (ValueError, TypeError):
                    engine.log_skip(self.strategy_id, condition_id, "parse_error")
                    continue

                if len(outcomes) < 2 or len(token_ids) < 2 or len(prices) < 2:
                    continue

                # Determine favorite
                fav_idx = 0 if prices[0] >= prices[1] else 1
                fav_price = prices[fav_idx]
                fav_label = outcomes[fav_idx]
                fav_token = token_ids[fav_idx]

                if fav_price < self._min_favorite:
                    engine.log_skip(self.strategy_id, condition_id, "no_favorite_above_threshold")
                    continue

                # Extract player surnames for ESPN
                names = _extract_player_surnames(title)
                if not names:
                    engine.log_skip(self.strategy_id, condition_id, "no_player_names")
                    continue
                tour = _extract_tour(title)

                # ESPN coverage check
                match_state = get_match_state(tour, names[0], names[1])
                if match_state is None:
                    engine.log_skip(self.strategy_id, condition_id, "no_score_feed")
                    continue
                if match_state.sets_completed >= 2:
                    engine.log_skip(self.strategy_id, condition_id, "past_two_sets")
                    continue

                # Place the order
                order_id = engine.place_order(
                    strategy_id=self.strategy_id,
                    condition_id=condition_id,
                    market_title=title,
                    favorite_token_id=fav_token,
                    favorite_token_label=fav_label,
                    favorite_price=fav_price,
                    dip_pct=self._dip_pct,
                    min_bid_price=self._min_bid,
                    min_fill_size=self._min_fill_size,
                    tour=tour,
                    player1=names[0],
                    player2=names[1],
                    max_resting=self._max_resting,
                    max_daily_fills=self._max_daily_fills,
                )
                if order_id:
                    LOG.info("Placed stink bid #%d on %s (%s) at bid=%.2f",
                             order_id, fav_label, condition_id[:16], 
                             compute_bid_price(fav_price, self._dip_pct, self._min_bid))
        finally:
            conn.close()

    def poll_espn_and_gate(self) -> None:
        """Poll ESPN scores and cancel bids when sets_completed >= 2.

        Also tracks ESPN outage state.
        """
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self.strategy_id)

            if not resting:
                return

            # Try ESPN poll (any order's tour works — poll both if mixed)
            tours_to_check = {o.get("tour", "atp") for o in resting}
            espn_ok = False

            for tour in tours_to_check:
                from feeds.tennis_scores import fetch_scoreboard, parse_scoreboard, find_match, get_sets_completed
                data = fetch_scoreboard(tour)
                if data is not None:
                    espn_ok = True
                    self.outage_tracker.record_success()
                    parsed = parse_scoreboard(data)

                    for order in resting:
                        if order.get("tour") != tour:
                            continue
                        match = find_match(parsed, order["player1"], order["player2"])
                        if match is None:
                            continue
                        state = get_sets_completed(match["competition"])
                        if state.sets_completed >= 2:
                            engine.cancel_order(order["order_id"], "sets_completed")
                            LOG.info("Cancelled bid #%d — %d sets completed",
                                     order["order_id"], state.sets_completed)
                else:
                    self.outage_tracker.record_failure()

            # Outage handling
            if self.outage_tracker.should_cancel_all:
                for order in resting:
                    if order["state"] == "resting":
                        engine.cancel_order(order["order_id"], "espn_outage")
                LOG.warning("ESPN sustained outage — cancelled all resting bids")
        finally:
            conn.close()

    def reprice_all(self) -> None:
        """Reprice resting bids if favorite price has moved materially."""
        import requests
        from polymarket_api import fetch_market_by_condition

        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self.strategy_id)
            session = requests.Session()

            for order in resting:
                # Skip if favorite flipped
                gamma = fetch_market_by_condition(session, order["condition_id"])
                if not gamma:
                    continue
                import json as _json
                try:
                    prices = [float(p) for p in _json.loads(gamma.get("outcomePrices", "[]"))]
                except (ValueError, TypeError):
                    continue
                if len(prices) < 2:
                    continue

                # Current favorite price (at original favorite's index)
                fav_label = order["favorite_token_label"]
                outcomes = _json.loads(gamma.get("outcomes", "[]"))
                try:
                    fav_idx = outcomes.index(fav_label)
                except ValueError:
                    continue
                current_fav_price = prices[fav_idx]

                # Flip guard: stop repricing if favorite dropped below 0.50
                if current_fav_price < 0.50:
                    continue

                engine.reprice_order(
                    order["order_id"],
                    new_favorite_price=current_fav_price,
                    threshold=self._reprice_threshold,
                    min_bid_price=self._min_bid,
                )
        finally:
            conn.close()

    async def run(self) -> None:
        """Main async loop:
        1. Trade tap runs continuously for fill detection
        2. Every 15 min: discover new markets, place bids, reprice existing
        3. Every 60s: poll ESPN, gate on sets_completed
        4. Every 30s: sync watchlist
        """
        LOG.info("StinkBidRunner starting for strategy_id=%s", self.strategy_id)

        tap_task = asyncio.create_task(
            run_trade_tap(self.tap_state, self._on_tick)
        )

        espn_counter = 0
        refresh_counter = 0

        try:
            while True:
                # Every 30s: sync watchlist
                self.sync_watchlist()

                # Every 60s: poll ESPN and gate
                espn_counter += 30
                if espn_counter >= self._espn_poll_sec:
                    espn_counter = 0
                    self.poll_espn_and_gate()

                # Every 15 min: discover, place, reprice
                refresh_counter += 30
                if refresh_counter >= self._refresh_sec:
                    refresh_counter = 0
                    self.discover_and_place()
                    self.reprice_all()

                await asyncio.sleep(30)
        except asyncio.CancelledError:
            tap_task.cancel()
            raise


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    def _load_strategy_params(db_path: str, strategy_id: int) -> dict:
        """Load params from the real `strategies` table.

        Repo schema:
        - primary key: `id`
        - JSON config column: `parameters`
        """
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT parameters, status FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            if row is None:
                raise SystemExit(f"strategy {strategy_id} not found")
            if str(row["status"] or "active") != "active":
                raise SystemExit(f"strategy {strategy_id} is not active")
            raw = row["parameters"] or "{}"
            return json.loads(raw) if isinstance(raw, str) else {}
        finally:
            conn.close()

    parser = argparse.ArgumentParser(description="Stink Bid fill detection runner")
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--strategy-id", type=int, required=True)
    args = parser.parse_args()

    params = _load_strategy_params(args.db, args.strategy_id)
    runner = StinkBidRunner(args.db, args.strategy_id, params=params)
    asyncio.run(runner.run())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_stink_bid_runner.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add stink_bid_runner.py tests/test_stink_bid_runner.py
git commit -m "feat: stink bid runner — trade tap → fill checker runtime loop"
```

---

## Task 7: Token ID Resolution (integrated into runner)

### Design decisions

Token resolution uses the existing `polymarket_api.fetch_market_by_condition(session, condition_id)` helper, which correctly queries `condition_ids` (plural) via the Gamma API. This is already called in `StinkBidRunner.discover_and_place()` — the runner fetches the Gamma market, parses `clobTokenIds`, and maps the favorite's outcome index to the correct token_id before calling `place_order()`.

**No separate task needed.** Token resolution is integrated into Task 6's `discover_and_place()` method. The existing `fetch_market_by_condition()` in `polymarket_api.py:546` is the correct helper — it uses `condition_ids` (plural) parameter which actually filters correctly, unlike `conditionId` (singular) which returns unfiltered results.

- [ ] **Step 1: Verify integration test**

Add to `tests/test_stink_bid_runner.py`:

```python
def test_token_resolution_uses_condition_ids_plural():
    """Token resolution must use condition_ids (plural) Gamma API param."""
    from polymarket_api import fetch_market_by_condition
    import inspect
    source = inspect.getsource(fetch_market_by_condition)
    assert "condition_ids" in source, "Must use condition_ids plural param"
    assert "conditionId" not in source or "condition_ids" in source
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_stink_bid_runner.py
git commit -m "test: verify token resolution uses condition_ids plural"
```

---

## Task 8: Evaluation Queries (deferred to post-data-collection)

**Files:**
- Create: `scripts/stink_bid_report.py`

### Design decisions

Evaluation runs after data accumulates (50+ qualified matches per spec §7 exit criteria). This task creates the evaluation script shell with the 8 metrics from the spec. It queries the `paper_limit_orders`, `paper_limit_fills`, and `paper_limit_events` tables.

**Explicitly deferred until data exists.** The script is created now so the metrics are defined and testable, but meaningful results require live data.

- [ ] **Step 1: Create evaluation script**

```python
# scripts/stink_bid_report.py
"""Stink Bid evaluation report — run after data collection."""
from __future__ import annotations

import json
import sqlite3
import sys


def report(db_path: str, strategy_id: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. Fill rate
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()["n"]
    strict = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_limit_orders WHERE strategy_id = ? AND state = 'filled'",
        (strategy_id,),
    ).fetchone()["n"]
    loose_only = conn.execute("""
        SELECT COUNT(DISTINCT plo.order_id) AS n
        FROM paper_limit_orders plo
        JOIN paper_limit_fills plf ON plo.order_id = plf.order_id
        WHERE plo.strategy_id = ? AND plo.state != 'filled'
        AND plf.fill_label = 'loose'
    """, (strategy_id,)).fetchone()["n"]

    print(f"=== STINK BID REPORT (strategy_id={strategy_id}) ===")
    print(f"Total orders: {total}")
    print(f"Strict fills: {strict} ({100*strict/max(total,1):.1f}%)")
    print(f"Loose-only (sensitivity gap): {loose_only}")

    # 2. Opportunity rate
    skipped = conn.execute("""
        SELECT COUNT(*) AS n FROM paper_limit_events
        WHERE event_type = 'order_skipped'
        AND json_extract(details_json, '$.reason') NOT IN
            ('max_resting_reached', 'max_daily_fills_reached')
    """).fetchone()["n"]
    qualified = total + skipped
    print(f"Qualified matches: {qualified}")
    print(f"Opportunity rate: orders that ever saw a trade at bid zone — requires fill event analysis")

    # 3-8: Win rate, PnL, fill quality, sensitivity gap, time-in-market, dip frequency
    # These require strategy_positions join — implemented when data exists
    print("\nDetailed metrics require accumulated position data.")
    print("Run this script after 50+ qualified matches.")

    conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    sid = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    report(db, sid)
```

- [ ] **Step 2: Commit**

```bash
git add scripts/stink_bid_report.py
git commit -m "feat: stink bid evaluation report shell — metrics defined, awaiting data"
```

---

## Potential Challenges and Mitigations

1. **ESPN name matching.** Polymarket titles may use different spellings, accents, or name formats than ESPN. Mitigation: surname-only matching, case-insensitive. If that's insufficient, add a manual alias table later.

2. **WS connection stability.** The trade tap runs a persistent WS connection. If it drops, resting bids miss trade events. Mitigation: reconnect with exponential backoff, log gaps so fill rate analysis accounts for downtime.

3. **Token ID resolution.** Polymarket markets have `condition_id` but the trade tap needs `token_id` (asset_id). The strategy must resolve this from market metadata (`tokens` array in Gamma API response or `market_metadata` table). The `clobTokenIds` field in Gamma API maps outcomes to token IDs.

4. **strategy_positions unique constraint.** The existing `uq_strategy_positions_active_owner` index is on `(wallet, condition_id, outcome) WHERE state IN (...)`. Stink bid uses a synthetic wallet `paper:stink_bid:{strategy_id}` to avoid collision with other empty-wallet paper strategies. The `instance_tag` is NOT part of the unique constraint, so `wallet` is the correct differentiator.

6. **Risk evaluation on fill.** Each strict fill should ideally go through `evaluate_entry_risk()` from `risk/policy.py` to enforce daily exposure and total exposure limits. In v1, the `paper_limit_engine`'s own rails (`max_resting`, `max_daily_fills`, `max_position_size_usdc`) provide basic guardrails. Full `evaluate_entry_risk()` integration is a v1.1 enhancement — it requires building the `RiskContext` from current portfolio state at fill time, which adds DB queries on the hot path. The v1 rails are sufficient for a paper test with $25/position and 5 fills/day caps.

5. **Concurrent access.** The trade tap runs async (fills happen anytime) while the strategy runner cycles every 15 min. The `paper_limit_orders` state transitions must be atomic. Mitigation: SQLite's single-writer lock + WAL mode handles this; state checks use `WHERE state = 'resting'` so a cancelled order can't be filled.
