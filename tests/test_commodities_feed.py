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
        ensure_commodity_snapshots_table(conn)

    def test_insert_and_retrieve(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        row_id = insert_commodity_snapshot(
            conn, instance_tag="test", symbol="BZ=F", price_usd=72.50,
            market_ts="2026-03-11T12:00:00Z", provider="yfinance",
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
        row_id = insert_commodity_snapshot(conn, **kwargs)
        self.assertIsNone(row_id)
        rows = recent_commodity_snapshots(conn, symbol="CL=F", hours=24)
        self.assertEqual(len(rows), 1)

    def test_recent_snapshots_filter_by_hours(self):
        conn = _in_memory_conn()
        ensure_commodity_snapshots_table(conn)
        conn.execute(
            """INSERT INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, market_ts, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-48 hours'))""",
            ("test", "commodities", "yfinance", "BZ=F", 70.0, "2026-03-09T12:00:00Z"),
        )
        conn.commit()
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


import unittest.mock


class TestCommoditiesFeedAdapterAttributes(unittest.TestCase):
    """Verify adapter class-level attributes."""

    def test_feed_id(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        self.assertEqual(CommoditiesFeedAdapter.feed_id, "commodities")

    def test_display_name(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        self.assertEqual(CommoditiesFeedAdapter.display_name, "Commodity Prices")

    def test_default_poll_seconds(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        self.assertEqual(CommoditiesFeedAdapter.default_poll_seconds, 1800)

    def test_mode(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        self.assertEqual(CommoditiesFeedAdapter.mode, "poll")


class TestCommoditiesFeedAdapterConfig(unittest.TestCase):
    """Verify config handling."""

    def test_validate_config_always_true(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        adapter = CommoditiesFeedAdapter()
        self.assertTrue(adapter.validate_config())

    def test_default_symbols(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        adapter = CommoditiesFeedAdapter()
        symbols = adapter._get_symbols()
        self.assertIn("BZ=F", symbols)
        self.assertIn("CL=F", symbols)

    @unittest.mock.patch.dict(os.environ, {"COMMODITY_SYMBOLS": "GC=F,NG=F"})
    def test_symbols_from_env(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        adapter = CommoditiesFeedAdapter()
        symbols = adapter._get_symbols()
        self.assertEqual(symbols, ["GC=F", "NG=F"])

    def test_warmup_creates_table(self):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("commodity_snapshots", tables)


class TestCommoditiesFeedAdapterTick(unittest.TestCase):
    """Verify tick() fetches and stores data."""

    @unittest.mock.patch("sniper.feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_stores_snapshots(self, mock_fetch):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
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

    @unittest.mock.patch("sniper.feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_skips_failed_symbols(self, mock_fetch):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
        mock_fetch.return_value = []  # all symbols failed
        conn = _in_memory_conn()
        adapter = CommoditiesFeedAdapter()
        adapter.warmup(conn)
        adapter.tick(conn)
        rows = recent_commodity_snapshots(conn, hours=24)
        self.assertEqual(len(rows), 0)

    @unittest.mock.patch("sniper.feeds.commodities.CommoditiesFeedAdapter._fetch_prices")
    def test_tick_deduplicates(self, mock_fetch):
        from sniper.feeds.commodities import CommoditiesFeedAdapter
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
        from sniper.feeds.registry import get_feed_adapter
        adapter = get_feed_adapter("commodities")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "commodities")

    def test_registry_list_includes_commodities(self):
        from sniper.feeds.registry import list_feed_adapters
        feed_ids = [a.feed_id for a in list_feed_adapters()]
        self.assertIn("commodities", feed_ids)
