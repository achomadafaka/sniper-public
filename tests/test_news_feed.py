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


from sniper.feeds.news import NewsFeedAdapter, _canonicalize_url, _compute_article_hash, _extract_sentiment_keywords


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

    @patch("sniper.feeds.news.NewsFeedAdapter._fetch_rss")
    @patch("sniper.feeds.news.NewsFeedAdapter._fetch_newsapi")
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

    @patch("sniper.feeds.news.NewsFeedAdapter._fetch_rss")
    @patch("sniper.feeds.news.NewsFeedAdapter._fetch_newsapi")
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
        from sniper.feeds.registry import get_feed_adapter
        adapter = get_feed_adapter("news")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "news")
