"""Tests for scripts/fast_ooda.py — deterministic fast-path event matcher."""

import json
import sqlite3
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root is on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from scripts.fast_ooda import (
    extract_keywords,
    score_match,
    find_matches,
    merge_micro_boosts,
    fetch_recent_news,
    fetch_open_positions,
    run,
    HAPPENED_POSITIVE,
    HAPPENED_NEGATIVE,
    MAX_BOOST,
    MIN_BOOST,
    MICRO_BOOST_EXPIRY_HOURS,
)


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    def test_basic_extraction(self):
        """Extracts words >= 4 chars, lowercased."""
        kw = extract_keywords("Iran ceasefire talks collapse in Geneva")
        assert "iran" in kw
        assert "ceasefire" in kw
        assert "talks" in kw
        assert "collapse" in kw
        assert "geneva" in kw
        # "in" is too short (2 chars)
        assert "in" not in kw

    def test_stopwords_removed(self):
        """Common stopwords are excluded."""
        kw = extract_keywords("This will be the end of an era before 2026")
        assert "will" not in kw
        assert "before" not in kw
        assert "2026" not in kw

    def test_empty_input(self):
        kw = extract_keywords("")
        assert kw == set()

    def test_short_words_excluded(self):
        """Words under 4 chars are excluded."""
        kw = extract_keywords("US UK EU war oil gas")
        # All words are <= 3 chars, so nothing passes min_length=4
        assert len(kw) == 0

    def test_duplicate_words(self):
        """Same word appearing twice only appears once in the set."""
        kw = extract_keywords("Iran Iran Iran ceasefire ceasefire")
        assert "iran" in kw
        assert "ceasefire" in kw
        assert len(kw) == 2


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoreMatch:
    def test_positive_sentiment_yes_position(self):
        """Positive event words + YES position = positive boost."""
        boost = score_match("ceasefire deal confirmed between nations", "YES", 3)
        assert boost > 0

    def test_positive_sentiment_no_position(self):
        """Positive event words + NO position = NEGATIVE boost (inverted)."""
        boost = score_match("ceasefire deal confirmed between nations", "NO", 3)
        assert boost < 0

    def test_negative_sentiment_yes_position(self):
        """Negative event words + YES position = negative boost."""
        boost = score_match("ceasefire talks collapsed and failed completely", "YES", 3)
        assert boost < 0

    def test_negative_sentiment_no_position(self):
        """Negative event words + NO position = POSITIVE boost (inverted)."""
        boost = score_match("ceasefire talks collapsed and failed completely", "NO", 3)
        assert boost > 0

    def test_neutral_headline_no_boost(self):
        """Headlines with no sentiment keywords produce zero boost."""
        boost = score_match("weather update for the greater area", "YES", 3)
        assert boost == 0

    def test_micro_boost_capped_positive(self):
        """Boosts are capped at MAX_BOOST."""
        # Load up with many positive words
        headline = " ".join(HAPPENED_POSITIVE)
        boost = score_match(headline, "YES", 5)
        assert boost <= MAX_BOOST

    def test_micro_boost_capped_negative(self):
        """Boosts are capped at MIN_BOOST."""
        headline = " ".join(HAPPENED_NEGATIVE)
        boost = score_match(headline, "YES", 5)
        assert boost >= MIN_BOOST

    def test_mixed_sentiment(self):
        """Mixed positive + negative words produce net score."""
        # "confirmed" (+1 pos) and "rejected" (+1 neg) cancel out = 0 net
        # Note: "deal" is also in HAPPENED_POSITIVE, so avoid it in this test
        boost = score_match("plan confirmed but motion rejected", "YES", 3)
        # Should be 0 since pos and neg cancel
        assert boost == 0

    def test_outcome_case_insensitive(self):
        """Outcome comparison is case-insensitive."""
        boost_lower = score_match("deal confirmed", "no", 2)
        boost_upper = score_match("deal confirmed", "NO", 2)
        assert boost_lower == boost_upper

    def test_none_outcome_defaults_to_yes(self):
        """None outcome defaults to YES behavior."""
        boost_none = score_match("deal confirmed", None, 2)
        boost_yes = score_match("deal confirmed", "YES", 2)
        assert boost_none == boost_yes


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

class TestFindMatches:
    def test_keyword_overlap_match(self):
        """Headlines with 2+ keyword overlap match positions."""
        news = [{"title": "Iran ceasefire talks progress in Geneva",
                 "source": "bbc", "description": ""}]
        positions = [{
            "condition_id": "0xABC",
            "market_title": "US-Iran ceasefire agreement by April 15",
            "outcome": "YES",
            "entry_price": 0.35,
        }]
        matches = find_matches(news, positions)
        assert len(matches) >= 1
        m = matches[0]
        assert m["condition_id"] == "0xABC"
        assert "iran" in m["overlap_keywords"] or "ceasefire" in m["overlap_keywords"]

    def test_no_overlap_no_match(self):
        """No keyword overlap = no match."""
        news = [{"title": "Apple releases new iPhone model",
                 "source": "tech", "description": ""}]
        positions = [{
            "condition_id": "0xDEF",
            "market_title": "US-Iran ceasefire agreement by April 15",
            "outcome": "YES",
            "entry_price": 0.35,
        }]
        matches = find_matches(news, positions)
        assert len(matches) == 0

    def test_neutral_headline_filtered(self):
        """Matches with zero boost are filtered out."""
        news = [{"title": "Iran ceasefire talks continue without changes",
                 "source": "bbc", "description": ""}]
        positions = [{
            "condition_id": "0xABC",
            "market_title": "US-Iran ceasefire agreement by April 15",
            "outcome": "YES",
            "entry_price": 0.35,
        }]
        matches = find_matches(news, positions)
        # "continue" is not in either sentiment list, so boost should be 0
        # and the match should be filtered out
        for m in matches:
            assert m["boost"] != 0

    def test_multiple_positions_matched(self):
        """One headline can match multiple positions."""
        news = [{"title": "Iran strikes confirmed by military officials",
                 "source": "bbc", "description": ""}]
        positions = [
            {
                "condition_id": "0x1",
                "market_title": "US-Iran military strikes before April",
                "outcome": "YES",
                "entry_price": 0.35,
            },
            {
                "condition_id": "0x2",
                "market_title": "Iran military conflict escalation 2026",
                "outcome": "NO",
                "entry_price": 0.60,
            },
        ]
        matches = find_matches(news, positions)
        cids = [m["condition_id"] for m in matches]
        # Should match both positions (keywords "iran", "military", "strikes" overlap)
        assert "0x1" in cids or "0x2" in cids

    def test_empty_news(self):
        """No news = no matches."""
        matches = find_matches([], [{"condition_id": "0x1", "market_title": "Test market",
                                     "outcome": "YES", "entry_price": 0.5}])
        assert matches == []

    def test_empty_positions(self):
        """No positions = no matches."""
        matches = find_matches(
            [{"title": "Big news event", "source": "bbc", "description": ""}],
            [],
        )
        assert matches == []

    def test_description_included_in_matching(self):
        """News description text is also used for keyword extraction."""
        news = [{
            "title": "Breaking update",
            "source": "bbc",
            "description": "Iran ceasefire deal confirmed by officials",
        }]
        positions = [{
            "condition_id": "0xABC",
            "market_title": "US-Iran ceasefire agreement by April 15",
            "outcome": "YES",
            "entry_price": 0.35,
        }]
        matches = find_matches(news, positions)
        # Should match via description keywords
        assert len(matches) >= 1


# ---------------------------------------------------------------------------
# Boost merging
# ---------------------------------------------------------------------------

class TestMergeBoosts:
    def test_preserves_analyst_boosts(self):
        """Fast OODA preserves analyst boosts and skips legacy namespaced duplicates."""
        existing = {
            "0xabc": {
                "boost": 20,
                "reason": "Analyst: strong geo context",
                "direction": "YES",
                "confidence": "high",
                "source": "world_analyst",
                "expires_hours": 24,
            }
        }
        matches = [{
            "condition_id": "0xABC",
            "market_title": "Test market",
            "outcome": "YES",
            "news_title": "Breaking: test event confirmed",
            "overlap_keywords": ["test", "market"],
            "overlap_count": 2,
            "boost": 10,
        }]
        merged = merge_micro_boosts(existing, matches)
        # Original analyst boost should still be there
        assert merged["0xabc"]["source"] == "world_analyst"
        assert merged["0xabc"]["boost"] == 20
        # Fast OODA no longer writes a legacy namespaced duplicate key here
        assert "0xabc__fast_ooda" not in merged

    def test_overwrites_own_boosts(self):
        """Fast OODA overwrites its own previous boosts."""
        existing = {
            "0xabc": {
                "boost": 5,
                "reason": "[fast_ooda] old headline",
                "source": "fast_ooda",
                "expires_hours": 2,
            }
        }
        matches = [{
            "condition_id": "0xABC",
            "market_title": "Test market",
            "outcome": "YES",
            "news_title": "New headline about test market",
            "overlap_keywords": ["test", "market"],
            "overlap_count": 2,
            "boost": 15,
        }]
        merged = merge_micro_boosts(existing, matches)
        assert merged["0xabc"]["boost"] == 15
        assert merged["0xabc"]["source"] == "fast_ooda"

    def test_empty_matches_no_change(self):
        """Empty matches list doesn't modify existing boosts."""
        existing = {"0xabc": {"boost": 20, "source": "world_analyst"}}
        merged = merge_micro_boosts(existing, [])
        assert merged == existing

    def test_new_condition_id_added(self):
        """New condition_id not in existing boosts gets added."""
        existing = {}
        matches = [{
            "condition_id": "0xNEW",
            "market_title": "New market",
            "outcome": "NO",
            "news_title": "Event related to new market",
            "overlap_keywords": ["event", "market"],
            "overlap_count": 2,
            "boost": -10,
        }]
        merged = merge_micro_boosts(existing, matches)
        assert "0xnew" in merged
        assert merged["0xnew"]["boost"] == -10
        assert merged["0xnew"]["source"] == "fast_ooda"
        assert merged["0xnew"]["expires_hours"] == MICRO_BOOST_EXPIRY_HOURS

    def test_strongest_boost_per_condition(self):
        """If multiple matches for same condition_id, strongest wins."""
        matches = [
            {
                "condition_id": "0xABC",
                "market_title": "Test",
                "outcome": "YES",
                "news_title": "Weak signal",
                "overlap_keywords": ["test"],
                "overlap_count": 2,
                "boost": 5,
            },
            {
                "condition_id": "0xABC",
                "market_title": "Test",
                "outcome": "YES",
                "news_title": "Strong signal",
                "overlap_keywords": ["test"],
                "overlap_count": 3,
                "boost": 15,
            },
        ]
        merged = merge_micro_boosts({}, matches)
        assert merged["0xabc"]["boost"] == 15


# ---------------------------------------------------------------------------
# DB reader tests (with in-memory SQLite)
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db():
    """Create an in-memory SQLite DB with news_items and paper_trades tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            url TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            topic_tags TEXT,
            sentiment TEXT,
            processed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            market_title TEXT,
            outcome TEXT,
            entry_price REAL,
            status TEXT DEFAULT 'open',
            wallet TEXT,
            duplicate_of INTEGER
        );
        CREATE TABLE strategy_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            outcome TEXT,
            entry_price REAL,
            state TEXT DEFAULT 'open',
            strategy_id TEXT
        );
        CREATE TABLE market_metadata (
            condition_id TEXT PRIMARY KEY,
            title TEXT
        );
    """)
    return conn


class TestFetchRecentNews:
    def test_fetches_recent_items(self, mem_db):
        """Items fetched within the window are returned."""
        mem_db.execute(
            "INSERT INTO news_items (source, title, description, fetched_at) VALUES (?, ?, ?, datetime('now'))",
            ("bbc", "Iran ceasefire confirmed", "Details about the deal"),
        )
        mem_db.commit()
        # Use a large window to definitely catch it
        news = fetch_recent_news(mem_db, window_minutes=60)
        assert len(news) == 1
        assert news[0]["title"] == "Iran ceasefire confirmed"

    def test_old_items_excluded(self, mem_db):
        """Items older than the window are excluded."""
        mem_db.execute(
            "INSERT INTO news_items (source, title, fetched_at) VALUES (?, ?, datetime('now', '-120 minutes'))",
            ("bbc", "Old news"),
        )
        mem_db.commit()
        news = fetch_recent_news(mem_db, window_minutes=30)
        assert len(news) == 0


class TestFetchOpenPositions:
    def test_fetches_open_paper_trades(self, mem_db):
        """Open paper trades are returned."""
        mem_db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("0x1", "Test Market", "YES", 0.35, "open", "0xWALLET"),
        )
        mem_db.commit()
        positions = fetch_open_positions(mem_db)
        assert len(positions) == 1
        assert positions[0]["condition_id"] == "0x1"
        assert positions[0]["source"] == "paper_trades"

    def test_closed_trades_excluded(self, mem_db):
        """Closed paper trades are not returned."""
        mem_db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("0x1", "Test Market", "YES", 0.35, "resolution_win", "0xWALLET"),
        )
        mem_db.commit()
        positions = fetch_open_positions(mem_db)
        assert len(positions) == 0

    def test_duplicate_trades_excluded(self, mem_db):
        """Duplicate paper trades are excluded."""
        mem_db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet, duplicate_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("0x1", "Test Market", "YES", 0.35, "open", "0xWALLET", 99),
        )
        mem_db.commit()
        positions = fetch_open_positions(mem_db)
        assert len(positions) == 0

    def test_fetches_strategy_positions(self, mem_db):
        """Open strategy positions are returned."""
        mem_db.execute(
            "INSERT INTO strategy_positions (condition_id, outcome, entry_price, state, strategy_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0x2", "NO", 0.60, "open", "7"),
        )
        mem_db.execute(
            "INSERT INTO market_metadata (condition_id, title) VALUES (?, ?)",
            ("0x2", "Strategy Test Market"),
        )
        mem_db.commit()
        positions = fetch_open_positions(mem_db)
        assert len(positions) == 1
        assert positions[0]["condition_id"] == "0x2"
        assert positions[0]["market_title"] == "Strategy Test Market"
        assert positions[0]["source"] == "strategy_positions"

    def test_deduplication_across_sources(self, mem_db):
        """Same condition_id in both paper_trades and strategy_positions is deduplicated."""
        mem_db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("0xSAME", "Shared Market", "YES", 0.35, "open", "0xWALLET"),
        )
        mem_db.execute(
            "INSERT INTO strategy_positions (condition_id, outcome, entry_price, state, strategy_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("0xSAME", "YES", 0.40, "open", "7"),
        )
        mem_db.execute(
            "INSERT INTO market_metadata (condition_id, title) VALUES (?, ?)",
            ("0xSAME", "Shared Market"),
        )
        mem_db.commit()
        positions = fetch_open_positions(mem_db)
        assert len(positions) == 1


# ---------------------------------------------------------------------------
# Integration: run() with mocked Supabase
# ---------------------------------------------------------------------------

class TestRunIntegration:
    @patch("scripts.fast_ooda.write_run_summary", return_value=True)
    @patch("scripts.fast_ooda.write_boosts", return_value=True)
    @patch("scripts.fast_ooda.read_current_boosts", return_value={})
    @patch("scripts.fast_ooda._get_db_conn")
    def test_end_to_end_with_match(self, mock_conn, mock_read, mock_write, mock_summary):
        """Full run with a matching news-position pair."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY, source TEXT, title TEXT, description TEXT,
                url TEXT, published_at TEXT, fetched_at TEXT DEFAULT (datetime('now')),
                topic_tags TEXT, sentiment TEXT, processed INTEGER DEFAULT 0
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY, condition_id TEXT, market_title TEXT,
                outcome TEXT, entry_price REAL, status TEXT, wallet TEXT, duplicate_of INTEGER
            );
            CREATE TABLE strategy_positions (
                id INTEGER PRIMARY KEY, condition_id TEXT, outcome TEXT,
                entry_price REAL, state TEXT, strategy_id TEXT
            );
            CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY, title TEXT);
        """)
        db.execute(
            "INSERT INTO news_items (source, title, description, fetched_at) VALUES (?, ?, ?, datetime('now'))",
            ("bbc", "Iran strikes confirmed by military command", "Major escalation in the region"),
        )
        db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("0xIRAN", "Iran military strikes before April 2026", "YES", 0.35, "open", "0xWALLET"),
        )
        db.commit()
        mock_conn.return_value = db

        result = run(quiet=True, dry_run=False, window_minutes=60)

        assert result["matches_found"] >= 1
        assert result["boosts_written"] >= 1
        mock_write.assert_called_once()
        mock_summary.assert_called_once()

    @patch("scripts.fast_ooda._get_db_conn")
    def test_dry_run_no_writes(self, mock_conn):
        """Dry run mode doesn't write to Supabase."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY, source TEXT, title TEXT, description TEXT,
                url TEXT, published_at TEXT, fetched_at TEXT DEFAULT (datetime('now')),
                topic_tags TEXT, sentiment TEXT, processed INTEGER DEFAULT 0
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY, condition_id TEXT, market_title TEXT,
                outcome TEXT, entry_price REAL, status TEXT, wallet TEXT, duplicate_of INTEGER
            );
            CREATE TABLE strategy_positions (
                id INTEGER PRIMARY KEY, condition_id TEXT, outcome TEXT,
                entry_price REAL, state TEXT, strategy_id TEXT
            );
            CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY, title TEXT);
        """)
        db.execute(
            "INSERT INTO news_items (source, title, description, fetched_at) VALUES (?, ?, ?, datetime('now'))",
            ("bbc", "Iran strikes confirmed", ""),
        )
        db.execute(
            "INSERT INTO paper_trades (condition_id, market_title, outcome, entry_price, status, wallet) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("0xIRAN", "Iran military strikes confirmed", "YES", 0.35, "open", "0xW"),
        )
        db.commit()
        mock_conn.return_value = db

        with patch("scripts.fast_ooda.write_boosts") as mock_wb, \
             patch("scripts.fast_ooda.write_run_summary") as mock_ws:
            result = run(quiet=True, dry_run=True, window_minutes=60)
            mock_wb.assert_not_called()
            mock_ws.assert_not_called()

    @patch("scripts.fast_ooda.write_run_summary", return_value=True)
    @patch("scripts.fast_ooda._get_db_conn")
    def test_no_news_no_writes(self, mock_conn, mock_summary):
        """No recent news produces no matches and no boost writes."""
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY, source TEXT, title TEXT, description TEXT,
                url TEXT, published_at TEXT, fetched_at TEXT DEFAULT (datetime('now')),
                topic_tags TEXT, sentiment TEXT, processed INTEGER DEFAULT 0
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY, condition_id TEXT, market_title TEXT,
                outcome TEXT, entry_price REAL, status TEXT, wallet TEXT, duplicate_of INTEGER
            );
            CREATE TABLE strategy_positions (
                id INTEGER PRIMARY KEY, condition_id TEXT, outcome TEXT,
                entry_price REAL, state TEXT, strategy_id TEXT
            );
            CREATE TABLE market_metadata (condition_id TEXT PRIMARY KEY, title TEXT);
        """)
        db.commit()
        mock_conn.return_value = db

        result = run(quiet=True, dry_run=False, window_minutes=60)
        assert result["matches_found"] == 0
        assert result["boosts_written"] == 0
