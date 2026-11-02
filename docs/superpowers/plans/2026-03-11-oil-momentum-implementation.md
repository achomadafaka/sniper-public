# Oil Momentum Strategy & World Context Feeds — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `oil_momentum_v1` strategy plugin with news and commodities feed adapters, context query layer, and separate-instance rollout config.

**Architecture:** Two new feed adapters (news, commodities) write to normalized snapshot tables. A context query layer in `context/` re-exports read-only DB helpers. The `oil_momentum_v1` strategy uses commodity momentum to discover mispriced Polymarket markets. All raw SQL lives in `db.py`; context module is a thin composition layer.

**Tech Stack:** yfinance (commodity prices), feedparser (RSS), NewsAPI (optional), SQLite, existing strategy plugin framework.

**Spec:** `docs/superpowers/specs/2026-03-11-oil-momentum-and-world-context-design.md`

---

## Chunk 1: Commodities Feed (DB + Adapter + Tests)

Build the commodity price data pipeline first — it's the primary signal for the strategy.

### Task 1.1: Commodity Snapshots DB Helpers

**Files:**
- Modify: `db.py` (append after weather snapshot helpers, ~line 10269)
- Test: `tests/test_commodities_feed.py`

- [ ] **Step 1: Write failing tests for DB helpers**

```python
# tests/test_commodities_feed.py
"""Tests for the commodities feed adapter and DB helpers."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch, MagicMock

from db import (
    ensure_commodity_snapshots_table,
    init_db,
    insert_commodity_snapshot,
    recent_commodity_snapshots,
)


def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


class TestCommoditySnapshotsTable(unittest.TestCase):
    """Verify commodity_snapshots table creation and helpers."""

    def test_ensure_table_creates_table(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("commodity_snapshots", tables)

    def test_ensure_table_idempotent(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        ensure_commodity_snapshots_table(conn)  # no error

    def test_insert_and_retrieve(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        row_id = insert_commodity_snapshot(
            conn,
            instance_tag="test",
            symbol="BZ=F",
            price_usd=72.50,
            market_ts="2026-03-11T12:00:00Z",
            provider="yfinance",
        )
        self.assertGreater(row_id, 0)
        rows = recent_commodity_snapshots(conn, symbol="BZ=F", hours=24)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["price_usd"], 72.50)

    def test_unique_constraint(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        kwargs = dict(
            instance_tag="test", symbol="CL=F", price_usd=70.0,
            market_ts="2026-03-11T12:00:00Z", provider="yfinance",
        )
        insert_commodity_snapshot(conn, **kwargs)
        # Second insert with same (symbol, market_ts, provider) should be ignored
        row_id = insert_commodity_snapshot(conn, **kwargs)
        self.assertIsNone(row_id)
        rows = recent_commodity_snapshots(conn, symbol="CL=F", hours=24)
        self.assertEqual(len(rows), 1)

    def test_recent_snapshots_filter_by_hours(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        # Insert old snapshot manually
        conn.execute(
            """INSERT INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, market_ts, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-48 hours'))""",
            ("test", "commodities", "yfinance", "BZ=F", 70.0, "2026-03-09T12:00:00Z"),
        )
        conn.commit()
        # Recent one
        insert_commodity_snapshot(
            conn, instance_tag="test", symbol="BZ=F", price_usd=72.0,
            market_ts="2026-03-11T12:00:00Z", provider="yfinance",
        )
        rows = recent_commodity_snapshots(conn, symbol="BZ=F", hours=24)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["price_usd"], 72.0)

    def test_recent_snapshots_all_symbols(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        insert_commodity_snapshot(
            conn, instance_tag="test", symbol="BZ=F", price_usd=72.0,
            market_ts="2026-03-11T12:00:00Z", provider="yfinance",
        )
        insert_commodity_snapshot(
            conn, instance_tag="test", symbol="CL=F", price_usd=68.0,
            market_ts="2026-03-11T12:00:01Z", provider="yfinance",
        )
        rows = recent_commodity_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 2)

    def test_insert_with_volume_and_payload(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        row_id = insert_commodity_snapshot(
            conn, instance_tag="test", symbol="GC=F", price_usd=2050.0,
            market_ts="2026-03-11T12:00:00Z", provider="yfinance",
            volume=125000.0, payload_json='{"info": "gold"}',
        )
        self.assertGreater(row_id, 0)
        rows = recent_commodity_snapshots(conn, symbol="GC=F", hours=24)
        self.assertAlmostEqual(rows[0]["volume"], 125000.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_commodities_feed.py -v -x 2>&1 | head -20`
Expected: FAIL — `ImportError: cannot import name 'ensure_commodity_snapshots_table' from 'db'`

- [ ] **Step 3: Implement DB helpers in db.py**

Append after `recent_weather_snapshots()` (after line ~10269):

```python
# ---------------------------------------------------------------------------
# Commodity snapshots
# ---------------------------------------------------------------------------


def ensure_commodity_snapshots_table(conn) -> None:
    """Create the ``commodity_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commodity_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag  TEXT,
            feed_id       TEXT NOT NULL DEFAULT 'commodities',
            provider      TEXT NOT NULL DEFAULT 'yfinance',
            symbol        TEXT NOT NULL,
            price_usd     REAL NOT NULL,
            volume        REAL,
            market_ts     TEXT NOT NULL,
            payload_json  TEXT NOT NULL DEFAULT '{}',
            captured_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_commodity_snapshots_uniq
        ON commodity_snapshots (symbol, market_ts, provider)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_commodity_snapshots_sym_ts
        ON commodity_snapshots (symbol, captured_at DESC)
    """)
    conn.commit()


def insert_commodity_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    symbol: str,
    price_usd: float,
    market_ts: str,
    provider: str = "yfinance",
    volume: float | None = None,
    payload_json: str = "{}",
) -> int | None:
    """Insert a commodity snapshot row.  Returns row id, or None if duplicate."""
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, volume, market_ts, payload_json)
               VALUES (?, 'commodities', ?, ?, ?, ?, ?, ?)""",
            (instance_tag, provider, symbol, price_usd, volume, market_ts, payload_json),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception("insert_commodity_snapshot failed")
        return None


def recent_commodity_snapshots(
    conn, symbol: str | None = None, hours: int = 24
) -> list[dict]:
    """Return recent commodity snapshots, newest first."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if symbol:
        rows = conn.execute(
            """SELECT * FROM commodity_snapshots
               WHERE symbol = ? AND captured_at >= ?
               ORDER BY captured_at DESC""",
            (symbol, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM commodity_snapshots
               WHERE captured_at >= ?
               ORDER BY captured_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_commodities_feed.py::TestCommoditySnapshotsTable -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_commodities_feed.py
git commit -m "feat(commodities): add commodity_snapshots DB helpers"
```

---

### Task 1.2: Commodities Feed Adapter

**Files:**
- Create: `feeds/commodities.py`
- Modify: `feeds/registry.py`
- Test: `tests/test_commodities_feed.py` (append)

- [ ] **Step 1: Write failing tests for the adapter**

Append to `tests/test_commodities_feed.py`:

```python
from feeds.commodities import CommoditiesFeedAdapter


class TestCommoditiesFeedAdapterAttributes(unittest.TestCase):
    """Verify adapter class-level attributes."""

    def test_feed_id(self):
        self.assertEqual(CommoditiesFeedAdapter.feed_id, "commodities")

    def test_display_name(self):
        self.assertEqual(CommoditiesFeedAdapter.display_name, "Commodity Prices")

    def test_default_poll_seconds(self):
        self.assertEqual(CommoditiesFeedAdapter.default_poll_seconds, 1800)

    def test_mode(self):
        self.assertEqual(CommoditiesFeedAdapter.mode, "poll")


class TestCommoditiesFeedAdapterConfig(unittest.TestCase):
    """Verify config handling."""

    def test_validate_config_always_true(self):
        adapter = CommoditiesFeedAdapter()
        self.assertTrue(adapter.validate_config())

    def test_default_symbols(self):
        adapter = CommoditiesFeedAdapter()
        symbols = adapter._get_symbols()
        self.assertIn("BZ=F", symbols)
        self.assertIn("CL=F", symbols)

    @unittest.mock.patch.dict(os.environ, {"COMMODITY_SYMBOLS": "GC=F,NG=F"})
    def test_symbols_from_env(self):
        adapter = CommoditiesFeedAdapter()
        symbols = adapter._get_symbols()
        self.assertEqual(symbols, ["GC=F", "NG=F"])

    def test_warmup_creates_table(self):
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("commodity_snapshots", tables)


class TestCommoditiesFeedAdapterTick(unittest.TestCase):
    """Verify tick() fetches and stores data."""

    @unittest.mock.patch("feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_stores_snapshots(self, mock_fetch):
        mock_fetch.return_value = [
            {"symbol": "BZ=F", "price_usd": 72.50, "market_ts": "2026-03-11T12:00:00Z",
             "volume": 50000, "payload": {"regularMarketPrice": 72.50}},
        ]
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        adapter.tick(conn)
        rows = recent_commodity_snapshots(conn, symbol="BZ=F", hours=24)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["price_usd"], 72.50)

    @unittest.mock.patch("feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_skips_failed_symbols(self, mock_fetch):
        mock_fetch.return_value = []  # all symbols failed
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        adapter.tick(conn)
        rows = recent_commodity_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 0)

    @unittest.mock.patch("feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_deduplicates(self, mock_fetch):
        mock_fetch.return_value = [
            {"symbol": "BZ=F", "price_usd": 72.50, "market_ts": "2026-03-11T12:00:00Z",
             "volume": None, "payload": {}},
        ]
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        adapter.tick(conn)
        adapter.tick(conn)  # duplicate
        rows = recent_commodity_snapshots(conn, symbol="BZ=F", hours=24)
        self.assertEqual(len(rows), 1)


class TestCommoditiesFeedRegistry(unittest.TestCase):
    """Verify adapter is registered."""

    def test_registry_lookup(self):
        from feeds.registry import get_feed_adapter
        adapter = get_feed_adapter("commodities")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "commodities")

    def test_registry_list_includes_commodities(self):
        from feeds.registry import list_feed_adapters
        feed_ids = [a.feed_id for a in list_feed_adapters()]
        self.assertIn("commodities", feed_ids)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_commodities_feed.py -v -x 2>&1 | head -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'feeds.commodities'`

- [ ] **Step 3: Implement the adapter**

Create `feeds/commodities.py`:

```python
"""Commodity price feed adapter.

Polls commodity prices (oil, gold, gas) on a 30-minute cadence and stores
snapshots in the ``commodity_snapshots`` table.  Primary provider is yfinance
(no API key needed).  Per-symbol failure is non-fatal.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import FeedAdapter

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("BZ=F", "CL=F", "GC=F", "NG=F")


class CommoditiesFeedAdapter(FeedAdapter):
    """yfinance-backed commodity price feed."""

    feed_id = "commodities"
    display_name = "Commodity Prices"
    default_poll_seconds = 1800  # 30 minutes
    mode = "poll"

    def validate_config(self) -> bool:
        """Always True — yfinance needs no API key."""
        return True

    def warmup(self, conn) -> None:
        from db import ensure_commodity_snapshots_table
        ensure_commodity_snapshots_table(conn)

    def tick(self, conn) -> None:
        from db import insert_commodity_snapshot

        instance_tag = os.environ.get("INSTANCE_TAG", os.environ.get("SNIPER_INSTANCE_TAG", "default"))
        symbols = self._get_symbols()
        results = self._fetch_prices(symbols)

        stored = 0
        for item in results:
            try:
                row_id = insert_commodity_snapshot(
                    conn,
                    instance_tag=instance_tag,
                    symbol=item["symbol"],
                    price_usd=item["price_usd"],
                    market_ts=item["market_ts"],
                    provider="yfinance",
                    volume=item.get("volume"),
                    payload_json=json.dumps(item.get("payload", {})),
                )
                if row_id is not None:
                    stored += 1
            except Exception:
                logger.exception("commodity tick: failed to store %s", item.get("symbol"))

        if stored:
            logger.info("commodity snapshots stored: %d/%d symbols", stored, len(symbols))
        elif results:
            logger.debug("commodity tick: all snapshots were duplicates")
        else:
            logger.warning("commodity tick: no prices fetched for %d symbols", len(symbols))

    def _get_symbols(self) -> List[str]:
        env_symbols = os.environ.get("COMMODITY_SYMBOLS", "").strip()
        if env_symbols:
            return [s.strip().upper() for s in env_symbols.split(",") if s.strip()]
        return list(DEFAULT_SYMBOLS)

    @staticmethod
    def _fetch_prices(symbols: List[str]) -> List[Dict[str, Any]]:
        """Fetch current prices via yfinance.  Returns list of dicts.

        Per-symbol failure is non-fatal: log and skip.
        """
        results: List[Dict[str, Any]] = []
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance not installed — cannot fetch commodity prices")
            return results

        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                info = ticker.fast_info
                price = float(getattr(info, "last_price", 0) or 0)
                if price <= 0:
                    logger.warning("commodity: no price for %s", symbol)
                    continue
                market_ts = datetime.now(timezone.utc).isoformat()
                results.append({
                    "symbol": symbol,
                    "price_usd": price,
                    "market_ts": market_ts,
                    "volume": float(getattr(info, "last_volume", 0) or 0) or None,
                    "payload": {
                        "last_price": price,
                        "currency": getattr(info, "currency", "USD"),
                    },
                })
            except Exception:
                logger.exception("commodity: failed to fetch %s", symbol)

        return results
```

- [ ] **Step 4: Register in feeds/registry.py**

Add import and entry:

```python
from .commodities import CommoditiesFeedAdapter

# In _ADAPTER_CLASSES dict, add:
CommoditiesFeedAdapter.feed_id: CommoditiesFeedAdapter,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_commodities_feed.py -v`
Expected: all passed (~19 tests)

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `python3 -m pytest tests/ -q`
Expected: 1105+ passed

- [ ] **Step 7: Commit**

```bash
git add feeds/commodities.py feeds/registry.py tests/test_commodities_feed.py
git commit -m "feat(commodities): add commodity price feed adapter with yfinance provider"
```

---

## Chunk 2: News Feed (DB + Adapter + Tests)

### Task 2.1: News Snapshots DB Helpers

**Files:**
- Modify: `db.py` (append after commodity snapshot helpers)
- Test: `tests/test_news_feed.py`

- [ ] **Step 1: Write failing tests for DB helpers**

```python
# tests/test_news_feed.py
"""Tests for the news feed adapter and DB helpers."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch, MagicMock

from db import (
    ensure_news_snapshots_table,
    init_db,
    insert_news_snapshot,
    recent_news_snapshots,
    _classify_news_domain,
)


def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


class TestNewsSnapshotsTable(unittest.TestCase):
    """Verify news_snapshots table creation and helpers."""

    def test_ensure_table_creates_table(self):
        conn = _in_memory_conn()
        ensure_news_snapshots_table(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("news_snapshots", tables)

    def test_ensure_table_idempotent(self):
        conn = _in_memory_conn()
        ensure_news_snapshots_table(conn)
        ensure_news_snapshots_table(conn)

    def test_insert_and_retrieve(self):
        conn = _in_memory_conn()
        ensure_news_snapshots_table(conn)
        row_id = insert_news_snapshot(
            conn,
            instance_tag="test",
            source_name="rss_reuters",
            canonical_url="https://reuters.com/article/oil-prices-surge",
            article_hash="abc123",
            title="Oil prices surge amid Iran tensions",
            description="Brent crude jumps 5%",
            published_at="2026-03-11T10:00:00Z",
            domain="Oil",
            sentiment_keywords='["oil", "surge", "iran"]',
        )
        self.assertGreater(row_id, 0)
        rows = recent_news_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Oil prices surge amid Iran tensions")

    def test_unique_article_hash(self):
        conn = _in_memory_conn()
        ensure_news_snapshots_table(conn)
        kwargs = dict(
            instance_tag="test", source_name="rss_reuters",
            canonical_url="https://reuters.com/article/oil",
            article_hash="hash1", title="Title", description="Desc",
            published_at="2026-03-11T10:00:00Z",
        )
        insert_news_snapshot(conn, **kwargs)
        row_id = insert_news_snapshot(conn, **kwargs)
        self.assertIsNone(row_id)
        rows = recent_news_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 1)

    def test_recent_filter_by_domain(self):
        conn = _in_memory_conn()
        ensure_news_snapshots_table(conn)
        insert_news_snapshot(
            conn, instance_tag="test", source_name="rss",
            canonical_url="https://example.com/oil", article_hash="h1",
            title="Oil news", description="", published_at="2026-03-11T10:00:00Z",
            domain="Oil",
        )
        insert_news_snapshot(
            conn, instance_tag="test", source_name="rss",
            canonical_url="https://example.com/crypto", article_hash="h2",
            title="Crypto news", description="", published_at="2026-03-11T10:00:00Z",
            domain="Crypto",
        )
        rows = recent_news_snapshots(conn, domain="Oil", hours=24)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "Oil")


class TestClassifyNewsDomain(unittest.TestCase):
    """Verify news-specific domain classifier."""

    def test_oil_domain(self):
        self.assertEqual(_classify_news_domain("Oil prices surge to 5-year high"), "Oil")
        self.assertEqual(_classify_news_domain("Brent crude tops $80"), "Oil")
        self.assertEqual(_classify_news_domain("OPEC cuts production"), "Oil")

    def test_geopolitics_domain(self):
        self.assertEqual(_classify_news_domain("Iran sanctions tightened by US"), "Geopolitics")
        self.assertEqual(_classify_news_domain("NATO deploys troops to eastern border"), "Geopolitics")

    def test_finance_domain(self):
        self.assertEqual(_classify_news_domain("Fed raises interest rate by 25bps"), "Finance")
        self.assertEqual(_classify_news_domain("Inflation hits 4% in January"), "Finance")

    def test_crypto_domain(self):
        self.assertEqual(_classify_news_domain("Bitcoin surges past $100k"), "Crypto")

    def test_general_fallback(self):
        self.assertEqual(_classify_news_domain("Local weather forecast for Tuesday"), "General")
        self.assertEqual(_classify_news_domain("New recipe for banana bread"), "General")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_news_feed.py -v -x 2>&1 | head -20`
Expected: FAIL — `ImportError: cannot import name 'ensure_news_snapshots_table' from 'db'`

- [ ] **Step 3: Implement DB helpers in db.py**

Append after `recent_commodity_snapshots()`:

```python
# ---------------------------------------------------------------------------
# News snapshots
# ---------------------------------------------------------------------------


def _classify_news_domain(title: str) -> str:
    """Classify a news article title into a domain category.

    Separate from ``_classify_title_domain()`` which is tuned for Polymarket
    market titles.  This function uses keyword patterns suited to news headlines.
    """
    lower = title.lower()
    # Check most specific first
    oil_kw = ("oil", "crude", "brent", "wti", "petroleum", "opec",
              "refinery", "pipeline", "energy price", "barrel")
    if any(kw in lower for kw in oil_kw):
        return "Oil"
    geo_kw = ("iran", "sanctions", "war ", " war", "military", "conflict",
              "nato", "ceasefire", "invasion", "troops", "missile", "airstrike")
    if any(kw in lower for kw in geo_kw):
        return "Geopolitics"
    fin_kw = ("fed ", "interest rate", "inflation", "gdp", "stock market",
              "treasury", "central bank", "federal reserve")
    if any(kw in lower for kw in fin_kw):
        return "Finance"
    crypto_kw = ("bitcoin", "ethereum", "crypto", "blockchain")
    if any(kw in lower for kw in crypto_kw):
        return "Crypto"
    return "General"


def ensure_news_snapshots_table(conn) -> None:
    """Create the ``news_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag       TEXT,
            feed_id            TEXT NOT NULL DEFAULT 'news',
            source_name        TEXT NOT NULL,
            canonical_url      TEXT,
            article_hash       TEXT NOT NULL UNIQUE,
            title              TEXT NOT NULL,
            description        TEXT,
            published_at       TEXT,
            domain             TEXT,
            sentiment_keywords TEXT,
            payload_json       TEXT NOT NULL DEFAULT '{}',
            captured_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_snapshots_domain_pub
        ON news_snapshots (domain, published_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_snapshots_captured
        ON news_snapshots (captured_at DESC)
    """)
    conn.commit()


def insert_news_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    source_name: str,
    canonical_url: str | None = None,
    article_hash: str,
    title: str,
    description: str = "",
    published_at: str = "",
    domain: str | None = None,
    sentiment_keywords: str = "[]",
    payload_json: str = "{}",
) -> int | None:
    """Insert a news snapshot row.  Returns row id, or None if duplicate."""
    if domain is None:
        domain = _classify_news_domain(title)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news_snapshots
               (instance_tag, feed_id, source_name, canonical_url, article_hash,
                title, description, published_at, domain, sentiment_keywords, payload_json)
               VALUES (?, 'news', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_tag, source_name, canonical_url, article_hash,
             title, description, published_at, domain, sentiment_keywords, payload_json),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception("insert_news_snapshot failed")
        return None


def recent_news_snapshots(
    conn, domain: str | None = None, hours: int = 24
) -> list[dict]:
    """Return recent news snapshots, newest first."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if domain:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE domain = ? AND captured_at >= ?
               ORDER BY published_at DESC""",
            (domain, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE captured_at >= ?
               ORDER BY published_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_news_feed.py -v`
Expected: all passed (~12 tests)

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_news_feed.py
git commit -m "feat(news): add news_snapshots DB helpers and domain classifier"
```

---

### Task 2.2: News Feed Adapter

**Files:**
- Create: `feeds/news.py`
- Modify: `feeds/registry.py`
- Test: `tests/test_news_feed.py` (append)

- [ ] **Step 1: Write failing tests for the adapter**

Append to `tests/test_news_feed.py`:

```python
from feeds.news import NewsFeedAdapter, _canonicalize_url, _compute_article_hash, _extract_sentiment_keywords


class TestUrlCanonicalization(unittest.TestCase):

    def test_strips_utm_params(self):
        url = "https://reuters.com/article?utm_source=twitter&utm_medium=social&id=123"
        canon = _canonicalize_url(url)
        self.assertNotIn("utm_source", canon)
        self.assertNotIn("utm_medium", canon)
        self.assertIn("id=123", canon)

    def test_strips_ref_param(self):
        url = "https://example.com/article?ref=homepage&topic=oil"
        canon = _canonicalize_url(url)
        self.assertNotIn("ref=", canon)
        self.assertIn("topic=oil", canon)

    def test_handles_no_params(self):
        url = "https://example.com/article/oil-prices"
        self.assertEqual(_canonicalize_url(url), url)

    def test_handles_empty_url(self):
        self.assertEqual(_canonicalize_url(""), "")
        self.assertEqual(_canonicalize_url(None), "")


class TestArticleHash(unittest.TestCase):

    def test_url_based_hash(self):
        h1 = _compute_article_hash("https://reuters.com/oil", "Title1", "2026-03-11")
        h2 = _compute_article_hash("https://reuters.com/oil", "Title2", "2026-03-11")
        self.assertEqual(h1, h2)  # same URL = same hash

    def test_fallback_title_hash(self):
        h1 = _compute_article_hash("", "Oil prices surge", "2026-03-11T10:00:00Z")
        h2 = _compute_article_hash("", "Oil prices surge", "2026-03-11T10:00:00Z")
        self.assertEqual(h1, h2)

    def test_different_urls_different_hashes(self):
        h1 = _compute_article_hash("https://reuters.com/a", "T", "2026")
        h2 = _compute_article_hash("https://reuters.com/b", "T", "2026")
        self.assertNotEqual(h1, h2)

    def test_none_url_falls_back(self):
        h = _compute_article_hash(None, "Title", "2026-03-11")
        self.assertTrue(len(h) > 0)


class TestSentimentKeywords(unittest.TestCase):

    def test_extracts_oil_keywords(self):
        kws = _extract_sentiment_keywords("Oil prices surge amid Iran sanctions and OPEC cuts")
        self.assertIn("oil", kws)
        self.assertIn("sanctions", kws)
        self.assertIn("opec", kws)

    def test_returns_empty_for_generic(self):
        kws = _extract_sentiment_keywords("Local sports team wins game")
        self.assertEqual(kws, [])


class TestNewsFeedAdapterAttributes(unittest.TestCase):

    def test_feed_id(self):
        self.assertEqual(NewsFeedAdapter.feed_id, "news")

    def test_mode(self):
        self.assertEqual(NewsFeedAdapter.mode, "poll")

    def test_default_poll_seconds(self):
        self.assertEqual(NewsFeedAdapter.default_poll_seconds, 900)

    def test_validate_config_always_true(self):
        adapter = NewsFeedAdapter()
        self.assertTrue(adapter.validate_config())

    def test_warmup_creates_table(self):
        conn = _in_memory_conn()
        adapter = NewsFeedAdapter()
        adapter.warmup(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("news_snapshots", tables)


class TestNewsFeedAdapterTick(unittest.TestCase):

    @patch("feeds.news.NewsFeedAdapter._fetch_rss")
    @patch("feeds.news.NewsFeedAdapter._fetch_newsapi")
    def test_tick_rss_only(self, mock_newsapi, mock_rss):
        mock_rss.return_value = [
            {"title": "Oil prices rise", "url": "https://reuters.com/oil-rise",
             "description": "Brent up 3%", "published_at": "2026-03-11T10:00:00Z",
             "source_name": "rss_reuters"},
        ]
        mock_newsapi.return_value = []
        conn = _in_memory_conn()
        adapter = NewsFeedAdapter()
        adapter.warmup(conn)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEWS_API_KEY", None)
            adapter.tick(conn)
        rows = recent_news_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 1)

    @patch("feeds.news.NewsFeedAdapter._fetch_rss")
    @patch("feeds.news.NewsFeedAdapter._fetch_newsapi")
    def test_tick_deduplicates(self, mock_newsapi, mock_rss):
        article = {
            "title": "Oil prices rise", "url": "https://reuters.com/oil-rise",
            "description": "Brent up 3%", "published_at": "2026-03-11T10:00:00Z",
            "source_name": "rss_reuters",
        }
        mock_rss.return_value = [article]
        mock_newsapi.return_value = [article]  # same article from both sources
        conn = _in_memory_conn()
        adapter = NewsFeedAdapter()
        adapter.warmup(conn)
        adapter.tick(conn)
        rows = recent_news_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 1)


class TestNewsFeedRegistry(unittest.TestCase):

    def test_registry_lookup(self):
        from feeds.registry import get_feed_adapter
        adapter = get_feed_adapter("news")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "news")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_news_feed.py::TestNewsFeedAdapterAttributes -v -x 2>&1 | head -10`
Expected: FAIL — `ModuleNotFoundError: No module named 'feeds.news'`

- [ ] **Step 3: Implement the adapter**

Create `feeds/news.py`:

```python
"""News feed adapter (NewsAPI + RSS).

Polls RSS feeds on a 15-minute cadence and optionally enriches with NewsAPI.
Stores normalized article snapshots in the ``news_snapshots`` table.
RSS-only mode works without any API key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from .base import FeedAdapter

logger = logging.getLogger(__name__)

# Tracking params to strip from URLs for canonicalization
_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid",
})

# Default RSS feeds (verified working as of 2026-03-11)
DEFAULT_RSS_FEEDS = (
    "https://feeds.reuters.com/reuters/worldNews",
    "https://rss.app/feeds/v1.1/tsWbSnBk1ym9aGdv.xml",  # AP Top Headlines
    "https://www.aljazeera.com/xml/rss/all.xml",
)

# Sentiment keywords to extract from news titles/descriptions
SENTIMENT_KEYWORDS = (
    "oil", "crude", "brent", "wti", "petroleum", "opec", "barrel",
    "sanctions", "war", "military", "ceasefire", "invasion", "troops",
    "missile", "airstrike", "escalation", "conflict", "embargo",
    "inflation", "recession", "rally", "crash", "surge", "plunge",
    "bitcoin", "crypto",
)


def _canonicalize_url(url: str | None) -> str:
    """Strip tracking parameters from a URL for dedup."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        cleaned = {k: v for k, v in params.items() if k.lower() not in _STRIP_PARAMS}
        new_query = urlencode(cleaned, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url or ""


def _compute_article_hash(canonical_url: str | None, title: str, published_at: str) -> str:
    """Compute a dedup hash for an article.

    Primary: hash of canonical URL.
    Fallback: hash of title + published_at (for articles without URLs).
    """
    if canonical_url:
        source = canonical_url
    else:
        source = f"{title}|{published_at}"
    return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]


def _extract_sentiment_keywords(text: str) -> List[str]:
    """Extract known sentiment keywords from text."""
    lower = text.lower()
    return [kw for kw in SENTIMENT_KEYWORDS if kw in lower]


class NewsFeedAdapter(FeedAdapter):
    """NewsAPI + RSS feed adapter."""

    feed_id = "news"
    display_name = "News (NewsAPI + RSS)"
    default_poll_seconds = 900  # 15 minutes
    mode = "poll"

    def validate_config(self) -> bool:
        """Always True — RSS works without API key."""
        return True

    def warmup(self, conn) -> None:
        from db import ensure_news_snapshots_table
        ensure_news_snapshots_table(conn)

    def tick(self, conn) -> None:
        from db import insert_news_snapshot, _classify_news_domain

        instance_tag = os.environ.get("INSTANCE_TAG", os.environ.get("SNIPER_INSTANCE_TAG", "default"))

        # Collect articles from all sources
        articles: List[Dict[str, Any]] = []
        articles.extend(self._fetch_rss())
        if os.environ.get("NEWS_API_KEY", "").strip():
            articles.extend(self._fetch_newsapi())

        stored = 0
        for article in articles:
            title = str(article.get("title") or "")
            if not title:
                continue
            canonical_url = _canonicalize_url(article.get("url"))
            published_at = str(article.get("published_at") or "")
            article_hash = _compute_article_hash(canonical_url, title, published_at)
            domain = _classify_news_domain(title)
            sentiment_kws = _extract_sentiment_keywords(
                f"{title} {article.get('description', '')}"
            )

            row_id = insert_news_snapshot(
                conn,
                instance_tag=instance_tag,
                source_name=str(article.get("source_name") or "unknown"),
                canonical_url=canonical_url,
                article_hash=article_hash,
                title=title,
                description=str(article.get("description") or ""),
                published_at=published_at,
                domain=domain,
                sentiment_keywords=json.dumps(sentiment_kws),
                payload_json=json.dumps(article.get("payload", {})),
            )
            if row_id is not None:
                stored += 1

        if stored:
            logger.info("news snapshots stored: %d new articles", stored)
        else:
            logger.debug("news tick: no new articles")

    # -- internal helpers -------------------------------------------------------

    def _get_rss_urls(self) -> List[str]:
        env_feeds = os.environ.get("NEWS_RSS_FEEDS", "").strip()
        if env_feeds:
            return [u.strip() for u in env_feeds.split(",") if u.strip()]
        return list(DEFAULT_RSS_FEEDS)

    def _fetch_rss(self) -> List[Dict[str, Any]]:
        """Parse RSS feeds and return normalized article dicts."""
        articles: List[Dict[str, Any]] = []
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser not installed — cannot fetch RSS feeds")
            return articles

        for feed_url in self._get_rss_urls():
            try:
                feed = feedparser.parse(feed_url)
                source_name = f"rss_{feed.feed.get('title', 'unknown')[:20]}".lower().replace(" ", "_")
                for entry in feed.entries[:20]:  # cap per feed
                    articles.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "description": entry.get("summary", entry.get("description", "")),
                        "published_at": entry.get("published", ""),
                        "source_name": source_name,
                        "payload": {"feed_url": feed_url},
                    })
            except Exception:
                logger.exception("news: failed to parse RSS feed %s", feed_url)

        return articles

    def _fetch_newsapi(self) -> List[Dict[str, Any]]:
        """Fetch top headlines from NewsAPI (if key is set)."""
        import urllib.request
        import urllib.error

        api_key = os.environ.get("NEWS_API_KEY", "").strip()
        if not api_key:
            return []

        articles: List[Dict[str, Any]] = []
        url = (
            f"https://newsapi.org/v2/top-headlines"
            f"?category=business&language=en&pageSize=20&apiKey={api_key}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sniper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data.get("articles", []):
                    articles.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "description": item.get("description", ""),
                        "published_at": item.get("publishedAt", ""),
                        "source_name": "newsapi",
                        "payload": {"source": item.get("source", {})},
                    })
        except Exception:
            logger.exception("news: failed to fetch from NewsAPI")

        return articles
```

- [ ] **Step 4: Register in feeds/registry.py**

Add import and entry:

```python
from .news import NewsFeedAdapter

# In _ADAPTER_CLASSES dict, add:
NewsFeedAdapter.feed_id: NewsFeedAdapter,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_news_feed.py -v`
Expected: all passed (~25 tests)

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 1105+ passed

- [ ] **Step 7: Commit**

```bash
git add feeds/news.py feeds/registry.py tests/test_news_feed.py
git commit -m "feat(news): add news feed adapter with RSS and optional NewsAPI"
```

---

## Chunk 3: Context Query Layer (DB Helpers + Context Package + Tests)

### Task 3.1: Context DB Helpers

**Files:**
- Modify: `db.py` (append context query helpers)
- Test: `tests/test_context_layer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_context_layer.py
"""Tests for the context query layer."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest

from db import (
    init_db,
    ensure_commodity_snapshots_table,
    ensure_news_snapshots_table,
    insert_commodity_snapshot,
    insert_news_snapshot,
)


def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_commodity_snapshots_table(conn)
    ensure_news_snapshots_table(conn)
    return conn


def _seed_market(conn, condition_id, title, is_open=1, volume_24h=1000.0, end_date="2026-04-01T00:00:00Z"):
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, end_date_iso, category,
            category_source, volume_24h, is_open, last_synced_ts)
           VALUES (?, ?, '', '', ?, '', '', ?, ?, ?)""",
        (condition_id, title, end_date, volume_24h, is_open, int(time.time())),
    )
    conn.commit()


class TestFindAffectedMarkets(unittest.TestCase):

    def test_finds_matching_markets(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will oil prices exceed $80 per barrel?")
        _seed_market(conn, "cond2", "Will Bitcoin reach $200k?")
        results = find_affected_markets(conn, ["oil", "barrel"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition_id"], "cond1")
        self.assertIn("oil", results[0]["matched_keywords"])

    def test_skips_closed_markets(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will oil exceed $80?", is_open=0)
        results = find_affected_markets(conn, ["oil"])
        self.assertEqual(len(results), 0)

    def test_case_insensitive(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will OIL PRICES exceed $80?")
        results = find_affected_markets(conn, ["oil"])
        self.assertEqual(len(results), 1)

    def test_no_matches(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will the next US president be a Democrat?")
        results = find_affected_markets(conn, ["oil", "crude"])
        self.assertEqual(len(results), 0)

    def test_min_volume_filter(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will oil exceed $80?", volume_24h=50.0)
        results = find_affected_markets(conn, ["oil"], min_volume_24h=100.0)
        self.assertEqual(len(results), 0)


class TestCommodityPriceHistory(unittest.TestCase):

    def test_returns_price_series(self):
        from db import commodity_price_history
        conn = _in_memory_conn()
        insert_commodity_snapshot(conn, instance_tag="t", symbol="BZ=F",
                                 price_usd=70.0, market_ts="2026-03-11T08:00:00Z")
        insert_commodity_snapshot(conn, instance_tag="t", symbol="BZ=F",
                                 price_usd=72.0, market_ts="2026-03-11T12:00:00Z")
        series = commodity_price_history(conn, "BZ=F", hours=168)
        self.assertEqual(len(series), 2)
        # Ordered by market_ts ASC
        self.assertAlmostEqual(series[0]["price_usd"], 70.0)
        self.assertAlmostEqual(series[1]["price_usd"], 72.0)

    def test_empty_for_unknown_symbol(self):
        from db import commodity_price_history
        conn = _in_memory_conn()
        series = commodity_price_history(conn, "FAKE=F", hours=168)
        self.assertEqual(len(series), 0)


class TestRecentNewsForMarket(unittest.TestCase):

    def test_finds_related_news(self):
        from db import recent_news_for_market
        conn = _in_memory_conn()
        _seed_market(conn, "cond1", "Will oil prices exceed $80?")
        insert_news_snapshot(conn, instance_tag="t", source_name="rss",
                            canonical_url="https://example.com/oil",
                            article_hash="h1", title="Oil prices surge",
                            domain="Oil", published_at="2026-03-11T10:00:00Z")
        results = recent_news_for_market(conn, "cond1", hours=24)
        self.assertEqual(len(results), 1)

    def test_no_news_table_returns_empty(self):
        """Graceful degradation when news_snapshots doesn't exist."""
        from db import recent_news_for_market
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        # Don't create news_snapshots table
        _seed_market(conn, "cond1", "Will oil exceed $80?")
        results = recent_news_for_market(conn, "cond1", hours=24)
        self.assertEqual(len(results), 0)


class TestInsiderConvergenceCheck(unittest.TestCase):

    def test_returns_counts(self):
        from db import insider_convergence_check
        conn = _in_memory_conn()
        # Seed a position signal with insider score
        conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, entry_price, current_price,
                position_value, is_open, position_insider_score,
                position_speculator_score, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("wallet1", "cond1", "YES", 0.5, 0.6, 100.0, 1, 70, 30, int(time.time())),
        )
        conn.commit()
        result = insider_convergence_check(conn, "cond1", min_insider_score=55)
        self.assertEqual(result["insider_count"], 1)
        self.assertAlmostEqual(result["avg_insider_score"], 70.0)

    def test_empty_when_no_insiders(self):
        from db import insider_convergence_check
        conn = _in_memory_conn()
        result = insider_convergence_check(conn, "nonexistent")
        self.assertEqual(result["insider_count"], 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_context_layer.py -v -x 2>&1 | head -20`
Expected: FAIL — `ImportError: cannot import name 'find_affected_markets' from 'db'`

- [ ] **Step 3: Implement context DB helpers in db.py**

Append after `recent_news_snapshots()`:

```python
# ---------------------------------------------------------------------------
# Context query helpers (read-only)
# ---------------------------------------------------------------------------


def find_affected_markets(
    conn, keywords: list[str], domain: str | None = None, min_volume_24h: float = 0.0
) -> list[dict]:
    """Find open markets whose titles match any of the given keywords.

    Fetches up to 500 open markets ordered by volume, then filters Python-side.
    Same approach as weather_v1.discover().
    """
    rows = conn.execute(
        """SELECT condition_id, title, end_date_iso, category, volume_24h
           FROM market_metadata
           WHERE is_open = 1
           ORDER BY volume_24h DESC
           LIMIT 500"""
    ).fetchall()

    results = []
    for row in rows:
        title = str(row["title"] or "")
        vol = float(row["volume_24h"] or 0)
        if vol < min_volume_24h:
            continue
        lower_title = title.lower()
        matched = [kw for kw in keywords if kw.lower() in lower_title]
        if not matched:
            continue
        results.append({
            "condition_id": str(row["condition_id"] or ""),
            "title": title,
            "category": str(row["category"] or ""),
            "end_date_iso": str(row["end_date_iso"] or ""),
            "volume_24h": vol,
            "matched_keywords": matched,
        })
    return results


def commodity_price_history(
    conn, symbol: str, hours: int = 168
) -> list[dict]:
    """Return commodity price time series, oldest first.

    Filters by ``market_ts`` (provider timestamp), NOT ``captured_at``
    (our clock).  This ensures the time window reflects actual market
    data coverage, not when we happened to poll.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()
    rows = conn.execute(
        """SELECT price_usd, market_ts, provider, captured_at
           FROM commodity_snapshots
           WHERE symbol = ? AND market_ts >= ?
           ORDER BY market_ts ASC""",
        (symbol, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def market_price_series(
    conn, condition_id: str, hours: int = 72
) -> list[dict]:
    """Assemble whatever price history is available for a market.

    Sources: signal_history, position_signals.
    Returns list of {price, timestamp, source} ordered by timestamp ASC.
    May return empty list for markets the system hasn't tracked.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = int((_dt.now(_tz.utc) - _td(hours=hours)).timestamp())
    prices: list[dict] = []

    # Source 1: signal_history (most granular)
    try:
        rows = conn.execute(
            """SELECT current_price, analyzed_ts
               FROM signal_history
               WHERE condition_id = ? AND analyzed_ts >= ?
               ORDER BY analyzed_ts ASC""",
            (condition_id, cutoff),
        ).fetchall()
        for r in rows:
            p = float(r["current_price"] or 0)
            if p > 0:
                prices.append({"price": p, "timestamp": int(r["analyzed_ts"]), "source": "signal_history"})
    except Exception:
        pass  # table may not exist

    # Source 2: position_signals (current state)
    try:
        rows = conn.execute(
            """SELECT current_price, analyzed_ts
               FROM position_signals
               WHERE condition_id = ? AND analyzed_ts >= ?
               ORDER BY analyzed_ts ASC""",
            (condition_id, cutoff),
        ).fetchall()
        for r in rows:
            p = float(r["current_price"] or 0)
            if p > 0:
                prices.append({"price": p, "timestamp": int(r["analyzed_ts"]), "source": "position_signals"})
    except Exception:
        pass

    # Deduplicate by timestamp and sort
    seen = set()
    unique = []
    for p in sorted(prices, key=lambda x: x["timestamp"]):
        if p["timestamp"] not in seen:
            seen.add(p["timestamp"])
            unique.append(p)

    return unique


def recent_news_for_market(
    conn, condition_id: str, hours: int = 24
) -> list[dict]:
    """Find recent news articles related to a market.

    Matches news domain/keywords against the market's title.
    Returns empty list if news_snapshots table doesn't exist.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # Get market title
    row = conn.execute(
        "SELECT title FROM market_metadata WHERE condition_id = ? LIMIT 1",
        (condition_id,),
    ).fetchone()
    if row is None:
        return []

    market_title = str(row["title"] or "").lower()
    if not market_title:
        return []

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE captured_at >= ?
               ORDER BY published_at DESC
               LIMIT 100""",
            (cutoff,),
        ).fetchall()
    except Exception:
        # news_snapshots table may not exist
        return []

    # Python-side relevance filter
    results = []
    for r in rows:
        news_title = str(r["title"] or "").lower()
        # Check for keyword overlap between market title and news title
        market_words = set(market_title.split())
        news_words = set(news_title.split())
        overlap = market_words & news_words - {"will", "the", "a", "an", "to", "in", "of", "by", "be", "is", "it", "at", "on", "for", "and", "or"}
        if len(overlap) >= 2:
            results.append(dict(r))
    return results


def insider_convergence_check(
    conn, condition_id: str, min_insider_score: int = 55
) -> dict:
    """Check for high-insider-score wallets on a market.

    Returns dict with insider_count, total_insider_notional, avg_insider_score.
    Annotation only — not an entry gate.
    """
    try:
        rows = conn.execute(
            """SELECT position_insider_score, position_value
               FROM position_signals
               WHERE condition_id = ? AND is_open = 1
                 AND position_insider_score >= ?""",
            (condition_id, min_insider_score),
        ).fetchall()
    except Exception:
        rows = []

    if not rows:
        return {"insider_count": 0, "total_insider_notional": 0.0, "avg_insider_score": 0.0}

    scores = [float(r["position_insider_score"] or 0) for r in rows]
    notionals = [float(r["position_value"] or 0) for r in rows]
    return {
        "insider_count": len(rows),
        "total_insider_notional": sum(notionals),
        "avg_insider_score": sum(scores) / len(scores) if scores else 0.0,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_context_layer.py -v`
Expected: all passed (~12 tests)

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_context_layer.py
git commit -m "feat(context): add context query helpers for market/commodity/news/insider data"
```

---

### Task 3.2: Context Package

**Files:**
- Create: `context/__init__.py`
- Create: `context/market_context.py`

- [ ] **Step 1: Create context/market_context.py**

```python
"""Context query layer — thin composition over db.py helpers.

All raw SQL lives in ``db.py``.  This module re-exports and composes
those helpers to give strategy plugins a clean import path::

    from context import find_affected_markets, commodity_price_history
"""
from __future__ import annotations

from db import (
    commodity_price_history,
    find_affected_markets,
    insider_convergence_check,
    market_price_series,
    recent_news_for_market,
)

__all__ = [
    "commodity_price_history",
    "find_affected_markets",
    "insider_convergence_check",
    "market_price_series",
    "recent_news_for_market",
]
```

- [ ] **Step 2: Create context/__init__.py**

```python
"""Context query layer package."""

from .market_context import (
    commodity_price_history,
    find_affected_markets,
    insider_convergence_check,
    market_price_series,
    recent_news_for_market,
)

__all__ = [
    "commodity_price_history",
    "find_affected_markets",
    "insider_convergence_check",
    "market_price_series",
    "recent_news_for_market",
]
```

- [ ] **Step 3: Add import test to test_context_layer.py**

Append:

```python
class TestContextPackageImports(unittest.TestCase):
    """Verify context package re-exports work."""

    def test_import_from_context(self):
        from context import find_affected_markets, commodity_price_history
        self.assertTrue(callable(find_affected_markets))
        self.assertTrue(callable(commodity_price_history))

    def test_import_all_exports(self):
        from context import (
            commodity_price_history,
            find_affected_markets,
            insider_convergence_check,
            market_price_series,
            recent_news_for_market,
        )
        for fn in (commodity_price_history, find_affected_markets,
                   insider_convergence_check, market_price_series,
                   recent_news_for_market):
            self.assertTrue(callable(fn))
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_context_layer.py -v`
Expected: all passed (~14 tests)

- [ ] **Step 5: Commit**

```bash
git add context/__init__.py context/market_context.py tests/test_context_layer.py
git commit -m "feat(context): add context query layer package"
```

---

## Chunk 4: Shared Live Price Helper + Oil Momentum V1 Strategy

### Task 4.0: Extract Shared Live Price Helper

**Why:** The runner's `_enrich_signal_with_live_price()` in `strategy_runner.py:513` hardcodes a CLOB price fetch. The oil momentum strategy needs the same live price in `manage()`. Instead of duplicating, extract the core fetch into a shared helper so both paths use the same code.

**Files:**
- Modify: `polymarket_api.py` (add `fetch_live_clob_price()`)
- Modify: `strategy_runner.py` (reuse shared helper in `_enrich_signal_with_live_price`)

- [ ] **Step 1: Add shared helper to polymarket_api.py**

Append to `polymarket_api.py`:

```python
def fetch_live_clob_price(condition_id: str, outcome: str) -> float | None:
    """Fetch live CLOB price for a specific outcome from Polymarket.

    Returns the clamped price in [0.0, 1.0] or None on failure.
    Shared by strategy_runner enrichment and strategy manage() methods.
    """
    import requests
    try:
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            for tok in r.json().get("tokens", []):
                if str(tok.get("outcome", "")).lower() == outcome.lower():
                    price = max(0.0, min(1.0, float(tok["price"])))
                    if price > 0:
                        return price
    except Exception:
        pass
    return None
```

- [ ] **Step 2: Update strategy_runner.py to use shared helper**

In `_enrich_signal_with_live_price()` at line ~513, replace the inline requests call with:

```python
from polymarket_api import fetch_live_clob_price
live = fetch_live_clob_price(signal.condition_id, signal.outcome)
if live is not None and live > 0:
    if cached and cached > 0:
        gap = abs(live - cached) / cached * 100
        if gap > 5:
            logger.info(...)
    signal.current_price = live
return signal
```

- [ ] **Step 3: Run full test suite to verify no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: 1105+ passed

- [ ] **Step 4: Commit**

```bash
git add polymarket_api.py strategy_runner.py
git commit -m "refactor: extract shared fetch_live_clob_price helper"
```

---

### Task 4.1: Strategy Plugin Implementation

**Files:**
- Create: `strategies/oil_momentum_v1.py`
- Modify: `strategies/registry.py`
- Test: `tests/test_oil_momentum.py`

- [ ] **Step 1: Write failing tests for discover/direction logic**

```python
# tests/test_oil_momentum.py
"""Tests for the oil_momentum_v1 strategy plugin."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from db import (
    init_db,
    ensure_commodity_snapshots_table,
    ensure_news_snapshots_table,
    insert_commodity_snapshot,
)
from strategies.oil_momentum_v1 import (
    OilMomentumV1Strategy,
    _parse_direction,
    _compute_momentum,
)
from strategies.base import StrategySignal


def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_commodity_snapshots_table(conn)
    ensure_news_snapshots_table(conn)
    return conn


def _seed_market(conn, condition_id, title, is_open=1, volume_24h=1000.0,
                 end_date="2026-04-01T00:00:00Z"):
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, end_date_iso, category,
            category_source, volume_24h, is_open, last_synced_ts)
           VALUES (?, ?, '', '', ?, '', '', ?, ?, ?)""",
        (condition_id, title, end_date, volume_24h, is_open, int(time.time())),
    )
    conn.commit()


def _seed_commodity_history(conn, symbol="BZ=F", prices=None):
    """Seed commodity snapshots with timestamps spread across 24 hours."""
    if prices is None:
        prices = [70.0, 72.5]  # ~3.5% increase
    now = int(time.time())
    for i, price in enumerate(prices):
        hours_ago = len(prices) - 1 - i
        ts = now - hours_ago * 3600
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        conn.execute(
            """INSERT INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, market_ts,
                captured_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', ? || ' hours'))""",
            ("test", "commodities", "yfinance", symbol, price, iso,
             str(-hours_ago)),
        )
    conn.commit()


def _default_strategy():
    return {
        "id": 1, "strategy_type": "oil_momentum_v1", "parameters": json.dumps({}),
        "is_active": 1,
    }


# =============================================================================
# Direction parsing
# =============================================================================


class TestParseDirection(unittest.TestCase):

    def test_oil_up_keywords(self):
        self.assertEqual(_parse_direction("Will oil prices exceed $80?"), "oil_up")
        self.assertEqual(_parse_direction("Will crude rise above $75?"), "oil_up")
        self.assertEqual(_parse_direction("Will Brent be higher than $80?"), "oil_up")
        self.assertEqual(_parse_direction("Oil prices over $90 by June?"), "oil_up")

    def test_oil_down_keywords(self):
        self.assertEqual(_parse_direction("Will oil fall below $60?"), "oil_down")
        self.assertEqual(_parse_direction("Will crude prices drop under $65?"), "oil_down")
        self.assertEqual(_parse_direction("Will oil decline to new lows?"), "oil_down")

    def test_ambiguous_returns_none(self):
        self.assertIsNone(_parse_direction("What will happen to oil prices?"))
        self.assertIsNone(_parse_direction("Oil market outlook 2026"))

    def test_case_insensitive(self):
        self.assertEqual(_parse_direction("Will OIL EXCEED $80?"), "oil_up")


# =============================================================================
# Momentum computation
# =============================================================================


class TestComputeMomentum(unittest.TestCase):

    def test_positive_momentum(self):
        series = [
            {"price_usd": 70.0, "market_ts": "2026-03-10T00:00:00Z"},
            {"price_usd": 72.5, "market_ts": "2026-03-11T00:00:00Z"},
        ]
        pct = _compute_momentum(series, window_hours=24)
        self.assertAlmostEqual(pct, 3.571, places=2)

    def test_negative_momentum(self):
        series = [
            {"price_usd": 72.5, "market_ts": "2026-03-10T00:00:00Z"},
            {"price_usd": 70.0, "market_ts": "2026-03-11T00:00:00Z"},
        ]
        pct = _compute_momentum(series, window_hours=24)
        self.assertAlmostEqual(pct, -3.448, places=2)

    def test_empty_series(self):
        pct = _compute_momentum([], window_hours=24)
        self.assertAlmostEqual(pct, 0.0)

    def test_single_point(self):
        series = [{"price_usd": 70.0, "market_ts": "2026-03-11T12:00:00Z"}]
        pct = _compute_momentum(series, window_hours=24)
        self.assertAlmostEqual(pct, 0.0)

    def test_insufficient_time_coverage(self):
        """Series spanning only 30 min should return 0 for 24h window."""
        series = [
            {"price_usd": 70.0, "market_ts": "2026-03-11T11:30:00Z"},
            {"price_usd": 73.0, "market_ts": "2026-03-11T12:00:00Z"},
        ]
        pct = _compute_momentum(series, window_hours=24)
        self.assertAlmostEqual(pct, 0.0)  # insufficient coverage


# =============================================================================
# Discover
# =============================================================================


class TestDiscover(unittest.TestCase):

    def test_discovers_matching_market(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil prices exceed $80 per barrel?")
        _seed_commodity_history(conn, "BZ=F", [70.0, 72.5])  # +3.5%
        plugin = OilMomentumV1Strategy()
        signals = plugin.discover(conn, _default_strategy())
        self.assertGreaterEqual(len(signals), 1)
        self.assertEqual(signals[0].wallet, "oil_momentum_bot")
        self.assertEqual(signals[0].outcome, "YES")
        self.assertAlmostEqual(signals[0].current_price, 0.0)

    def test_skips_when_no_momentum(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil prices exceed $80?")
        _seed_commodity_history(conn, "BZ=F", [70.0, 70.5])  # +0.7%, below threshold
        plugin = OilMomentumV1Strategy()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)

    def test_skips_ambiguous_direction(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "What will happen to oil prices?")
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])  # +4.3%
        plugin = OilMomentumV1Strategy()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)

    def test_discovers_oil_down_market(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil prices fall below $60?")
        _seed_commodity_history(conn, "BZ=F", [73.0, 70.0])  # -4.1% (oil dropping)
        plugin = OilMomentumV1Strategy()
        signals = plugin.discover(conn, _default_strategy())
        self.assertGreaterEqual(len(signals), 1)
        self.assertEqual(signals[0].outcome, "YES")

    def test_skips_opposing_momentum(self):
        conn = _in_memory_conn()
        # Market thesis = oil up, but commodity momentum is DOWN
        _seed_market(conn, "cond_oil", "Will oil prices exceed $80?")
        _seed_commodity_history(conn, "BZ=F", [73.0, 70.0])  # -4.1%
        plugin = OilMomentumV1Strategy()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)


# =============================================================================
# Enter
# =============================================================================


class TestEnter(unittest.TestCase):

    def _make_signal(self, condition_id="cond_oil", price=0.45, momentum=4.0):
        return StrategySignal(
            wallet="oil_momentum_bot", condition_id=condition_id,
            outcome="YES", title="Will oil exceed $80?",
            current_price=price, position_value=0.0,
            analyzed_ts=int(time.time()),
            metadata={"momentum_pct": momentum, "commodity_symbol": "BZ=F",
                       "momentum_window_hours": 24, "direction": "oil_up"},
        )

    def test_approves_entry(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])
        plugin = OilMomentumV1Strategy()
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(), [])
        self.assertTrue(decision.should_enter)
        self.assertAlmostEqual(decision.size_usdc, 10.0)

    def test_rejects_duplicate(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])
        plugin = OilMomentumV1Strategy()
        existing = [{"condition_id": "cond_oil", "outcome": "YES"}]
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(), existing)
        self.assertFalse(decision.should_enter)
        self.assertIn("already open", decision.reason)

    def test_rejects_max_concurrent(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])
        plugin = OilMomentumV1Strategy()
        existing = [{"condition_id": f"other_{i}", "outcome": "YES"} for i in range(5)]
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(), existing)
        self.assertFalse(decision.should_enter)

    def test_rejects_high_entry_price(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])
        plugin = OilMomentumV1Strategy()
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(price=0.85), [])
        self.assertFalse(decision.should_enter)
        self.assertIn("entry price", decision.reason)

    def test_rejects_stale_momentum(self):
        conn = _in_memory_conn()
        # No commodity data = momentum check fails
        plugin = OilMomentumV1Strategy()
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(), [])
        self.assertFalse(decision.should_enter)
        self.assertIn("momentum", decision.reason.lower())

    def test_metadata_includes_insider_convergence(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", [70.0, 73.0])
        plugin = OilMomentumV1Strategy()
        decision = plugin.enter(conn, _default_strategy(), self._make_signal(), [])
        self.assertTrue(decision.should_enter)
        self.assertIn("insider_convergence", decision.metadata)


# =============================================================================
# Manage
# =============================================================================


class TestManage(unittest.TestCase):

    def test_market_open(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil exceed $80?", is_open=1)
        plugin = OilMomentumV1Strategy()
        position = {"condition_id": "cond_oil", "outcome": "YES", "avg_entry_price": 0.45}
        with patch("strategies.oil_momentum_v1.fetch_live_clob_price", return_value=0.55):
            state = plugin.manage(conn, _default_strategy(), position)
        self.assertTrue(state.signal_open)
        self.assertAlmostEqual(state.current_price, 0.55)

    def test_market_closed(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil exceed $80?", is_open=0)
        plugin = OilMomentumV1Strategy()
        position = {"condition_id": "cond_oil", "outcome": "YES", "avg_entry_price": 0.45}
        with patch("strategies.oil_momentum_v1.fetch_live_clob_price", return_value=0.55):
            state = plugin.manage(conn, _default_strategy(), position)
        self.assertFalse(state.signal_open)

    def test_fallback_to_entry_price(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil exceed $80?", is_open=1)
        plugin = OilMomentumV1Strategy()
        position = {"condition_id": "cond_oil", "outcome": "YES", "avg_entry_price": 0.45}
        with patch("strategies.oil_momentum_v1.fetch_live_clob_price", return_value=None):
            state = plugin.manage(conn, _default_strategy(), position)
        self.assertAlmostEqual(state.current_price, 0.45)


# =============================================================================
# Exit
# =============================================================================


class TestExit(unittest.TestCase):

    def _make_position(self, entry_price=0.45, opened_ts=None):
        return {
            "condition_id": "cond_oil", "outcome": "YES",
            "avg_entry_price": entry_price,
            "opened_ts": opened_ts or int(time.time()),
        }

    def test_market_resolved(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        state = StrategyManageState(current_price=0.50, signal_open=False)
        conn = _in_memory_conn()
        decision = plugin.exit(conn, _default_strategy(), self._make_position(), state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_closed")

    def test_early_close_win(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        state = StrategyManageState(current_price=0.98, signal_open=True)
        conn = _in_memory_conn()
        decision = plugin.exit(conn, _default_strategy(), self._make_position(), state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "early_close_win")

    def test_early_close_loss(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        state = StrategyManageState(current_price=0.02, signal_open=True)
        conn = _in_memory_conn()
        decision = plugin.exit(conn, _default_strategy(), self._make_position(), state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "early_close_loss")

    def test_take_profit(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        # Entered at 0.40, now at 0.55 = 37.5% ROI > 30% threshold
        state = StrategyManageState(current_price=0.55, signal_open=True)
        conn = _in_memory_conn()
        decision = plugin.exit(conn, _default_strategy(), self._make_position(entry_price=0.40), state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "take_profit")

    def test_stale_exit(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        state = StrategyManageState(current_price=0.50, signal_open=True)
        conn = _in_memory_conn()
        old_ts = int(time.time()) - 15 * 86400  # 15 days ago
        decision = plugin.exit(conn, _default_strategy(), self._make_position(opened_ts=old_ts), state)
        self.assertTrue(decision.should_exit)
        self.assertIn("stale", decision.reason)

    def test_hold(self):
        from strategies.base import StrategyManageState
        plugin = OilMomentumV1Strategy()
        state = StrategyManageState(current_price=0.50, signal_open=True)
        conn = _in_memory_conn()
        decision = plugin.exit(conn, _default_strategy(), self._make_position(), state)
        self.assertFalse(decision.should_exit)
        self.assertEqual(decision.reason, "hold")


# =============================================================================
# Registry
# =============================================================================


class TestOilMomentumRegistry(unittest.TestCase):

    def test_registry_lookup(self):
        from strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("oil_momentum_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "oil_momentum_v1")

    def test_plugin_attributes(self):
        plugin = OilMomentumV1Strategy()
        self.assertEqual(plugin.strategy_family, "commodity_momentum")
        self.assertEqual(plugin.required_feeds, ("polymarket", "commodities"))
        self.assertEqual(plugin.supported_execution_modes, ("paper",))
        self.assertEqual(plugin.default_loop_seconds, 300)

    def test_replay_adapter_none(self):
        plugin = OilMomentumV1Strategy()
        self.assertIsNone(plugin.replay_adapter())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_oil_momentum.py -v -x 2>&1 | head -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.oil_momentum_v1'`

- [ ] **Step 3: Implement the strategy plugin**

Create `strategies/oil_momentum_v1.py`:

```python
"""Oil Momentum V1 strategy plugin.

Detects significant oil/commodity price movements and trades related
Polymarket prediction markets before they reprice.  Paper-only in v1.

Thesis: when commodity prices move significantly but related Polymarket
markets haven't repriced yet, there's a mispricing window.

Direction logic is deterministic: market title encodes an oil-up or oil-down
thesis, commodity momentum confirms direction, and the bot bets YES on
aligned markets only.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

logger = logging.getLogger(__name__)

# Direction-determining keywords in market titles
_UP_KEYWORDS = ("above", "exceed", "over", "higher", "rise", "rises", "top", "surpass")
_DOWN_KEYWORDS = ("below", "under", "fall", "falls", "drop", "drops", "decline", "lower")

# Default oil/energy keywords for market discovery
_DEFAULT_OIL_KEYWORDS = (
    "oil", "crude", "brent", "wti", "petroleum", "energy price", "barrel",
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(params, dict):
        return {}
    return params


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _parse_direction(title: str) -> str | None:
    """Determine if a market title encodes an oil-up or oil-down thesis.

    Returns "oil_up", "oil_down", or None if ambiguous.
    """
    lower = title.lower()
    has_up = any(kw in lower for kw in _UP_KEYWORDS)
    has_down = any(kw in lower for kw in _DOWN_KEYWORDS)
    if has_up and not has_down:
        return "oil_up"
    if has_down and not has_up:
        return "oil_down"
    return None


def _compute_momentum(series: List[Dict[str, Any]], window_hours: int = 24) -> float:
    """Compute price momentum (%) from a commodity price series.

    Uses oldest and newest points in the series.  Requires that the series
    actually spans at least 50% of the requested window to avoid treating
    short bursts or backfills as real momentum signals.

    Returns 0.0 if insufficient data or insufficient time coverage.
    """
    if len(series) < 2:
        return 0.0
    oldest_price = _to_float(series[0].get("price_usd"))
    newest_price = _to_float(series[-1].get("price_usd"))
    if oldest_price <= 0:
        return 0.0

    # Validate time coverage: series must span >= 50% of window
    try:
        from datetime import datetime, timezone
        oldest_ts = datetime.fromisoformat(str(series[0]["market_ts"]).replace("Z", "+00:00"))
        newest_ts = datetime.fromisoformat(str(series[-1]["market_ts"]).replace("Z", "+00:00"))
        span_hours = (newest_ts - oldest_ts).total_seconds() / 3600
        if span_hours < window_hours * 0.5:
            return 0.0  # not enough time coverage
    except Exception:
        return 0.0  # can't parse timestamps, don't trust the data

    return (newest_price - oldest_price) / oldest_price * 100


class OilMomentumV1Strategy(StrategyPlugin):
    """Commodity momentum strategy — paper only, oil-price markets."""

    plugin_id = "oil_momentum_v1"
    display_name = "Oil Momentum V1"
    description = (
        "Detects significant oil price movements and trades related "
        "Polymarket markets before they reprice."
    )

    strategy_family = "commodity_momentum"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket", "commodities")
    default_loop_seconds = 300

    def discover(
        self, conn: sqlite3.Connection, strategy: Dict[str, Any]
    ) -> List[StrategySignal]:
        params = _parse_params(strategy)
        threshold = _to_float(params.get("momentum_threshold_pct"), 3.0)
        window_hours = int(params.get("momentum_window_hours", 24))
        oil_keywords_str = str(params.get("oil_keywords", ""))
        if oil_keywords_str:
            oil_keywords = [kw.strip() for kw in oil_keywords_str.split(",") if kw.strip()]
        else:
            oil_keywords = list(_DEFAULT_OIL_KEYWORDS)
        symbols_str = str(params.get("commodity_symbols", "BZ=F,CL=F"))
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]

        from context import commodity_price_history, find_affected_markets

        # Check momentum for each symbol
        momentum_by_symbol: Dict[str, float] = {}
        for symbol in symbols:
            series = commodity_price_history(conn, symbol, hours=window_hours)
            pct = _compute_momentum(series, window_hours)
            if abs(pct) >= threshold:
                momentum_by_symbol[symbol] = pct

        if not momentum_by_symbol:
            return []

        # Find matching markets
        markets = find_affected_markets(conn, oil_keywords)
        if not markets:
            return []

        signals: List[StrategySignal] = []
        # Use the symbol with strongest momentum
        best_symbol = max(momentum_by_symbol, key=lambda s: abs(momentum_by_symbol[s]))
        best_momentum = momentum_by_symbol[best_symbol]
        commodity_direction = "oil_up" if best_momentum > 0 else "oil_down"

        for market in markets:
            title = str(market.get("title") or "")
            condition_id = str(market.get("condition_id") or "")

            # Determine market direction
            market_direction = _parse_direction(title)
            if market_direction is None:
                continue  # ambiguous, skip

            # Only bet when commodity momentum MATCHES market thesis direction
            if market_direction != commodity_direction:
                continue

            signals.append(
                StrategySignal(
                    wallet="oil_momentum_bot",
                    condition_id=condition_id,
                    outcome="YES",
                    title=title,
                    current_price=0.0,  # Runner enriches via _enrich_signal_with_live_price
                    position_value=0.0,
                    analyzed_ts=_now_ts(),
                    metadata={
                        "commodity_symbol": best_symbol,
                        "momentum_pct": round(best_momentum, 3),
                        "momentum_window_hours": window_hours,
                        "direction": commodity_direction,
                        "matched_keywords": market.get("matched_keywords", []),
                        "end_date_iso": str(market.get("end_date_iso") or ""),
                    },
                )
            )

        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        max_entry_price = _to_float(params.get("max_entry_price"), 0.70)
        max_concurrent = int(params.get("max_concurrent_positions", 5))
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc"), 50.0)
        bet_size = _to_float(params.get("bet_size_usdc"), 10.0)
        threshold = _to_float(params.get("momentum_threshold_pct"), 3.0)
        window_hours = int(params.get("momentum_window_hours", 24))
        max_days = int(params.get("max_days_to_resolution", 60))

        # Gate 1: duplicate check
        if any(
            p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this market/outcome")

        # Gate 2: max concurrent
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                False, f"max concurrent positions reached ({max_concurrent})"
            )

        # Gate 3: max entry price
        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(
                False,
                f"entry price {signal.current_price:.3f} > {max_entry_price:.2f}",
            )

        # Gate 4: market exposure
        current_exposure = sum(
            _to_float(p.get("entry_shares"), 0) * _to_float(p.get("avg_entry_price"), 0)
            for p in open_positions
            if str(p.get("condition_id") or "").lower() == signal.condition_id.lower()
        )
        if current_exposure >= max_exposure:
            return StrategyEntryDecision(
                False,
                f"market exposure ${current_exposure:.0f} >= ${max_exposure:.0f} cap",
            )

        # Gate 5: momentum freshness
        commodity_symbol = str(signal.metadata.get("commodity_symbol") or "BZ=F")
        from context import commodity_price_history
        series = commodity_price_history(conn, commodity_symbol, hours=window_hours)
        current_momentum = _compute_momentum(series, window_hours)
        if abs(current_momentum) < threshold:
            return StrategyEntryDecision(
                False,
                f"momentum {current_momentum:.1f}% below threshold {threshold:.1f}%",
            )

        # Gate 6: resolution date
        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                if days_left < 0:
                    return StrategyEntryDecision(False, "market already past end date")
                if days_left > max_days:
                    return StrategyEntryDecision(
                        False,
                        f"market resolves in {days_left}d > {max_days}d",
                    )
            except Exception:
                pass

        # Clamp size
        headroom = max_exposure - current_exposure
        size_usdc = min(bet_size, headroom)
        size_usdc = max(1.0, size_usdc)

        # Insider convergence annotation
        from context import insider_convergence_check
        convergence = insider_convergence_check(conn, signal.condition_id)

        return StrategyEntryDecision(
            True,
            f"oil momentum: {commodity_symbol} {current_momentum:+.1f}%",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata={
                "commodity_symbol": commodity_symbol,
                "momentum_pct": round(current_momentum, 3),
                "momentum_window_hours": window_hours,
                "direction": str(signal.metadata.get("direction") or ""),
                "insider_convergence": convergence,
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        condition_id = str(position.get("condition_id") or "")
        outcome = str(position.get("outcome") or "YES")

        # Check market status
        row = conn.execute(
            "SELECT is_open FROM market_metadata WHERE condition_id = ? LIMIT 1",
            (condition_id,),
        ).fetchone()
        market_open = True
        if row is not None:
            market_open = bool(int(row["is_open"] or 0))

        # Fetch live price via shared helper (same path as runner enrichment)
        entry_price = _to_float(position.get("avg_entry_price"), 0.5)
        try:
            from polymarket_api import fetch_live_clob_price
            live_price = fetch_live_clob_price(condition_id, outcome)
        except Exception:
            live_price = None
        current_price = live_price if live_price is not None else entry_price

        return StrategyManageState(
            current_price=current_price,
            signal_open=market_open,
            metadata={},
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days", 14))
        take_profit_pct = _to_float(params.get("take_profit_pct"), 0.30)
        win_threshold = 0.97
        loss_threshold = 0.03

        current_price = _to_float(
            state.current_price, _to_float(position.get("avg_entry_price"))
        )
        entry_price = _to_float(position.get("avg_entry_price"), 0.5)
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts else 0

        # 1. Market resolved
        if not state.signal_open:
            return StrategyExitDecision(
                True, "market_closed", exit_price=current_price, status="closed"
            )

        # 2. Near-certain win
        if current_price >= win_threshold:
            return StrategyExitDecision(
                True, "early_close_win", exit_price=current_price, status="closed"
            )

        # 3. Near-certain loss
        if current_price <= loss_threshold:
            return StrategyExitDecision(
                True, "early_close_loss", exit_price=current_price, status="closed"
            )

        # 4. Take profit (ROI-based)
        if entry_price > 0:
            roi = (current_price - entry_price) / entry_price
            if roi >= take_profit_pct:
                return StrategyExitDecision(
                    True, "take_profit", exit_price=current_price, status="closed"
                )

        # 5. Stale
        if age_days >= stale_days:
            return StrategyExitDecision(
                True, f"stale_{age_days}d", exit_price=current_price, status="closed"
            )

        # 6. Hold
        return StrategyExitDecision(
            False, "hold", exit_price=current_price, status="open"
        )

    def report(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        positions: List[Dict[str, Any]],
    ) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})

    def replay_adapter(self):
        """Oil momentum replay deferred — return None."""
        return None
```

- [ ] **Step 4: Register in strategies/registry.py**

Add import and entry:

```python
from .oil_momentum_v1 import OilMomentumV1Strategy

# In _PLUGINS dict, add:
OilMomentumV1Strategy.plugin_id: OilMomentumV1Strategy(),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_oil_momentum.py -v`
Expected: all passed (~30+ tests)

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 1105+ passed

- [ ] **Step 7: Commit**

```bash
git add strategies/oil_momentum_v1.py strategies/registry.py tests/test_oil_momentum.py
git commit -m "feat(oil-momentum): add oil_momentum_v1 strategy plugin"
```

---

## Chunk 5: Rollout Config & Verification

### Task 5.1: Update .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add commodity and news env vars and oil momentum instance profile**

Append to `.env.example` after the weather instance block:

```
# --- Commodities Feed ---
# COMMODITY_SYMBOLS=BZ=F,CL=F,GC=F,NG=F  # Comma-separated yfinance symbols

# --- News Feed ---
# NEWS_RSS_FEEDS=                          # Comma-separated RSS URLs (defaults built in)

# --- Example: Oil Momentum Paper Instance ---
# IMPORTANT: Must use a SEPARATE DB file from insider-copy instance.
# Copy this block to .env on the oil-momentum VPS/instance:
#
#   DB_PATH=oil_momentum.db
#   INSTANCE_TAG=oil-momentum-paper
#   INSTANCE_DISPLAY_NAME=Oil Momentum Paper
#   INSTANCE_ROLE=paper
#   ENABLED_STRATEGIES=oil_momentum_v1
#   ENABLED_FEEDS=polymarket,news,commodities
#   PAPER_ONLY=1
#   INSTANCE_BANKROLL_USDC=500
#   INSTANCE_MAX_TOTAL_EXPOSURE_USDC=500
#   INSTANCE_MAX_MARKET_EXPOSURE_USDC=50
#   INSTANCE_MAX_CONCURRENT_POSITIONS=5
#   INSTANCE_DAILY_LOSS_LIMIT_USDC=25
#   STRATEGY_LOOP_SECONDS=300
#   COMMODITY_SYMBOLS=BZ=F,CL=F,GC=F,NG=F
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: add commodity/news feed and oil momentum instance config to .env.example"
```

---

### Task 5.2: Install Dependencies

**Files:** None (runtime dependency)

- [ ] **Step 1: Install yfinance and feedparser and update requirements.txt**

```bash
pip install yfinance feedparser
```

Add to `requirements.txt`:
```
yfinance
feedparser
```

- [ ] **Step 2: Verify imports work**

```bash
python3 -c "import yfinance; print('yfinance', yfinance.__version__)"
python3 -c "import feedparser; print('feedparser', feedparser.__version__)"
```

---

### Task 5.3: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 1105+ passed (all existing + ~80 new tests)

- [ ] **Step 2: Verify all registries**

```bash
python3 -c "
from feeds.registry import list_feed_adapters
from strategies.registry import list_strategy_plugins
print('Feeds:', [a.feed_id for a in list_feed_adapters()])
print('Strategies:', [p.plugin_id for p in list_strategy_plugins()])
"
```

Expected output:
```
Feeds: ['polymarket', 'weather', 'commodities', 'news']
Strategies: ['insider_copy_v1', 'fade_v1', 'weather_v1', 'oil_momentum_v1']
```

- [ ] **Step 3: Verify context imports**

```bash
python3 -c "
from context import (
    find_affected_markets, commodity_price_history,
    market_price_series, recent_news_for_market,
    insider_convergence_check,
)
print('All context imports OK')
"
```

- [ ] **Step 4: Smoke test commodity feed (optional, requires internet)**

```bash
python3 -c "
from feeds.commodities import CommoditiesFeedAdapter
adapter = CommoditiesFeedAdapter()
results = adapter._fetch_prices(['BZ=F'])
for r in results:
    print(f'{r[\"symbol\"]}: \${r[\"price_usd\"]:.2f}')
"
```

- [ ] **Step 5: Commit any remaining changes**

Ensure working tree is clean. If all tests pass and registries are correct, the implementation is complete.
