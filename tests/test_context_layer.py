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

    def test_domain_filter_applies(self):
        from db import find_affected_markets
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil", "Will oil exceed $80?")
        _seed_market(conn, "cond_sports", "Will the Knicks beat the Celtics?")
        results = find_affected_markets(conn, ["will"], domain="Sports")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition_id"], "cond_sports")
        self.assertEqual(results[0]["domain"], "Sports")


class TestCommodityPriceHistory(unittest.TestCase):

    def test_returns_price_series(self):
        from db import commodity_price_history
        from datetime import datetime, timezone, timedelta
        conn = _in_memory_conn()
        # Use relative timestamps so this test does not expire over time
        now = datetime.now(timezone.utc)
        ts1 = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts2 = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        insert_commodity_snapshot(conn, instance_tag="t", symbol="BZ=F",
                                 price_usd=70.0, market_ts=ts1)
        insert_commodity_snapshot(conn, instance_tag="t", symbol="BZ=F",
                                 price_usd=72.0, market_ts=ts2)
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


class TestMarketPriceSeries(unittest.TestCase):

    def test_returns_series_from_position_signals(self):
        from db import market_price_series
        conn = _in_memory_conn()
        now = int(time.time())
        conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, entry_price, current_price,
                position_value, is_open, position_insider_score,
                position_speculator_score, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("w1", "cond1", "YES", 0.3, 0.5, 100.0, 1, 70, 20, now - 3600),
        )
        conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, entry_price, current_price,
                position_value, is_open, position_insider_score,
                position_speculator_score, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("w2", "cond1", "YES", 0.3, 0.6, 200.0, 1, 80, 10, now),
        )
        conn.commit()
        series = market_price_series(conn, "cond1", hours=72)
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(series[0]["price"], 0.5)
        self.assertAlmostEqual(series[1]["price"], 0.6)

    def test_empty_for_unknown_condition(self):
        from db import market_price_series
        conn = _in_memory_conn()
        series = market_price_series(conn, "nonexistent", hours=72)
        self.assertEqual(len(series), 0)

    def test_deduplicates_same_timestamp(self):
        from db import market_price_series
        conn = _in_memory_conn()
        now = int(time.time())
        # Two signals at exact same analyzed_ts
        conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, entry_price, current_price,
                position_value, is_open, position_insider_score,
                position_speculator_score, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("w1", "cond1", "YES", 0.3, 0.5, 100.0, 1, 70, 20, now),
        )
        conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, entry_price, current_price,
                position_value, is_open, position_insider_score,
                position_speculator_score, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("w2", "cond1", "YES", 0.3, 0.5, 200.0, 1, 80, 10, now),
        )
        conn.commit()
        series = market_price_series(conn, "cond1", hours=72)
        self.assertEqual(len(series), 1)  # deduplicated


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


class TestContextPackageImports(unittest.TestCase):
    """Verify context package re-exports work."""

    def test_import_from_context(self):
        from sniper.context import find_affected_markets, commodity_price_history
        self.assertTrue(callable(find_affected_markets))
        self.assertTrue(callable(commodity_price_history))

    def test_import_all_exports(self):
        from sniper.context import (
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
