"""Tests for the news_intel_v1 strategy plugin."""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest.mock import patch

from sniper.strategies.base import StrategyManageState, StrategySignal
from sniper.strategies.news_intel_v1 import NewsIntelV1Strategy


def _valid_condition_id(n: int = 1) -> str:
    return f"0x{n:064x}"


def _make_conn(tmp_db: str = ":memory:") -> sqlite3.Connection:
    """Return an in-memory SQLite connection with trade_ideas + paper_trades tables."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_ideas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            market_title TEXT NOT NULL,
            outcome      TEXT NOT NULL,
            direction    TEXT NOT NULL,
            current_price REAL,
            target_price  REAL,
            confidence   TEXT NOT NULL DEFAULT 'low',
            reasoning    TEXT NOT NULL,
            news_trigger TEXT,
            news_source  TEXT,
            status       TEXT NOT NULL DEFAULT 'open',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at   TEXT,
            acted_at     TEXT,
            alerted_at   TEXT,
            notes        TEXT
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet        TEXT,
            condition_id  TEXT,
            outcome       TEXT,
            status        TEXT DEFAULT 'open',
            duplicate_of  TEXT
        );

        CREATE TABLE IF NOT EXISTS strategy_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id   INTEGER,
            condition_id  TEXT,
            outcome       TEXT,
            status        TEXT DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS market_metadata (
            condition_id    TEXT PRIMARY KEY,
            title           TEXT,
            slug            TEXT,
            event_slug      TEXT,
            end_date_iso    TEXT,
            category        TEXT,
            category_source TEXT,
            volume_24h      REAL,
            is_open         INTEGER DEFAULT 1,
            last_synced_ts  INTEGER,
            inserted_at     TEXT,
            updated_at      TEXT
        );
    """)
    conn.commit()
    return conn


def _insert_idea(
    conn,
    condition_id: str = _valid_condition_id(1),
    title: str = "Will X happen?",
    outcome: str = "Yes",
    direction: str = "BUY_YES",
    price: float = 0.45,
    confidence: str = "high",
    reasoning: str = "Strong news signal",
    source: str = "reuters",
    status: str = "open",
    expires_at: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO trade_ideas
           (condition_id, market_title, outcome, direction, current_price,
            confidence, reasoning, news_source, status, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (condition_id, title, outcome, direction, price,
         confidence, reasoning, source, status, expires_at),
    )
    conn.commit()
    return cur.lastrowid


def _make_signal(
    condition_id: str = _valid_condition_id(1),
    outcome: str = "Yes",
    price: float = 0.45,
    idea_id: int = 1,
) -> StrategySignal:
    return StrategySignal(
        wallet=f"news_{condition_id[:8]}",
        condition_id=condition_id,
        outcome=outcome,
        title="Will X happen?",
        current_price=price,
        position_value=0.0,
        analyzed_ts=int(time.time()),
        metadata={
            "idea_id": idea_id,
            "confidence": "HIGH",
            "reasoning": "Strong news signal",
            "direction": "BUY_YES",
            "news_source": "reuters",
        },
    )


# =============================================================================
# Plugin Attributes
# =============================================================================


class TestNewsIntelV1Attributes(unittest.TestCase):

    def test_plugin_id(self):
        p = NewsIntelV1Strategy()
        self.assertEqual(p.plugin_id, "news_intel_v1")

    def test_display_name(self):
        p = NewsIntelV1Strategy()
        self.assertEqual(p.display_name, "News Intel v1")

    def test_strategy_family(self):
        p = NewsIntelV1Strategy()
        self.assertEqual(p.strategy_family, "news_intel")

    def test_default_loop_seconds(self):
        p = NewsIntelV1Strategy()
        self.assertEqual(p.default_loop_seconds, 1800)


# =============================================================================
# discover()
# =============================================================================


class TestNewsIntelV1Discover(unittest.TestCase):

    def test_discover_returns_high_confidence_ideas(self):
        conn = _make_conn()
        condition_id = _valid_condition_id(11)
        _insert_idea(conn, confidence="high", condition_id=condition_id)

        plugin = NewsIntelV1Strategy()
        with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.45):
            signals = plugin.discover(conn, {"parameters": {}})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].condition_id, condition_id)
        self.assertEqual(signals[0].metadata["confidence"], "HIGH")

    def test_discover_skips_medium_confidence(self):
        conn = _make_conn()
        _insert_idea(conn, confidence="medium", condition_id=_valid_condition_id(12))

        plugin = NewsIntelV1Strategy()
        with patch.object(plugin, "_send_medium_alert") as mock_alert:
            with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.45):
                signals = plugin.discover(conn, {"parameters": {}})

        self.assertEqual(len(signals), 0)
        mock_alert.assert_called_once()

    def test_discover_skips_expired_ideas(self):
        conn = _make_conn()
        _insert_idea(
            conn,
            confidence="high",
            condition_id=_valid_condition_id(13),
            expires_at="2020-01-01 00:00:00",
        )

        plugin = NewsIntelV1Strategy()
        with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.45):
            signals = plugin.discover(conn, {"parameters": {}})

        self.assertEqual(len(signals), 0)

    def test_discover_deduplicates_by_condition_id(self):
        conn = _make_conn()
        # Two ideas with the same condition_id but different directions
        conn.execute(
            """INSERT INTO trade_ideas
               (condition_id, market_title, outcome, direction, current_price,
                confidence, reasoning, news_source, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_valid_condition_id(14), "Market A", "Yes", "BUY_YES", 0.40,
             "high", "reason1", "src1", "open"),
        )
        conn.execute(
            """INSERT INTO trade_ideas
               (condition_id, market_title, outcome, direction, current_price,
                confidence, reasoning, news_source, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_valid_condition_id(14), "Market A", "No", "BUY_NO", 0.60,
             "high", "reason2", "src2", "open"),
        )
        conn.commit()

        plugin = NewsIntelV1Strategy()
        with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.40):
            signals = plugin.discover(conn, {"parameters": {}})

        # Only one signal despite two rows with same condition_id
        self.assertEqual(len(signals), 1)

    def test_discover_skips_closed_status(self):
        conn = _make_conn()
        _insert_idea(conn, confidence="high", status="acted")

        plugin = NewsIntelV1Strategy()
        signals = plugin.discover(conn, {"parameters": {}})

        self.assertEqual(len(signals), 0)


# =============================================================================
# enter()
# =============================================================================


class TestNewsIntelV1Enter(unittest.TestCase):

    def test_enter_approves_valid_signal(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        signal = _make_signal(price=0.45)

        decision = plugin.enter(conn, {"parameters": {}}, signal, [])

        self.assertTrue(decision.should_enter)
        self.assertGreater(decision.size_usdc, 0)
        self.assertAlmostEqual(decision.entry_price, 0.45)

    def test_enter_respects_max_concurrent(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        signal = _make_signal()

        # 10 open positions (default max_concurrent_positions = 10)
        positions = [
            {"condition_id": f"cond_{i}", "outcome": "Yes"} for i in range(10)
        ]

        decision = plugin.enter(conn, {"parameters": {}}, signal, positions)

        self.assertFalse(decision.should_enter)
        self.assertIn("Max concurrent", decision.reason)

    def test_enter_rejects_duplicate_position(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        signal = _make_signal(condition_id="cond_001", outcome="Yes")

        positions = [{"condition_id": "cond_001", "outcome": "Yes"}]

        decision = plugin.enter(conn, {"parameters": {}}, signal, positions)

        self.assertFalse(decision.should_enter)
        self.assertIn("Duplicate", decision.reason)

    def test_enter_rejects_cross_dedup_with_paper_trades(self):
        conn = _make_conn()
        # Insert an open paper_trade for same condition_id + outcome
        conn.execute(
            """INSERT INTO paper_trades (wallet, condition_id, outcome, status, duplicate_of)
               VALUES (?, ?, ?, 'open', NULL)""",
            ("some_wallet", "cond_001", "Yes"),
        )
        conn.commit()

        plugin = NewsIntelV1Strategy()
        signal = _make_signal(condition_id="cond_001", outcome="Yes")

        decision = plugin.enter(conn, {"parameters": {}}, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("paper_trades", decision.reason)

    def test_enter_rejects_low_price(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        signal = _make_signal(price=0.005)

        decision = plugin.enter(conn, {"parameters": {}}, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("Price", decision.reason)

    def test_enter_rejects_high_price(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        signal = _make_signal(price=0.995)

        decision = plugin.enter(conn, {"parameters": {}}, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("Price", decision.reason)


# =============================================================================
# manage()
# =============================================================================


class TestNewsIntelV1Manage(unittest.TestCase):

    @patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.55)
    def test_manage_returns_live_price(self, mock_fetch):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.45}

        state = plugin.manage(conn, {"parameters": {}}, position)

        self.assertAlmostEqual(state.current_price, 0.55)
        self.assertTrue(state.signal_open)
        self.assertTrue(state.metadata["live_price_available"])

    @patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=None)
    def test_manage_falls_back_to_entry_price(self, mock_fetch):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.45}

        state = plugin.manage(conn, {"parameters": {}}, position)

        self.assertAlmostEqual(state.current_price, 0.45)
        self.assertFalse(state.metadata["live_price_available"])


# =============================================================================
# exit()
# =============================================================================


class TestNewsIntelV1Exit(unittest.TestCase):

    def test_exit_on_market_resolved_yes(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.98, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_resolved_yes")

    def test_exit_on_market_resolved_no(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.02, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_resolved_no")

    def test_exit_on_stop_loss_yes_side(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        # Entry at 0.40, 25% stop-loss triggers below 0.30
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.40, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.29, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")

    def test_exit_on_stop_loss_no_side(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        # NO side: entry_price is stored as the NO price.
        # Entry: NO price = 0.60 (YES was 0.40 at entry).
        # Current: manage() returns YES price = 0.60 => NO current = 1 - 0.60 = 0.40.
        # 25% stop-loss: 0.40 < 0.60 * (1 - 0.25) = 0.45 => triggers.
        position = {"condition_id": "cond_001", "outcome": "No", "entry_price": 0.60, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.60, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stop_loss")

    def test_exit_on_staleness(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        # Opened 100 days ago (default max_days_hold = 90)
        position = {
            "condition_id": "cond_001",
            "outcome": "Yes",
            "entry_price": 0.45,
            "opened_ts": int(time.time()) - 86400 * 100,
        }
        state = StrategyManageState(current_price=0.50, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stale_position")

    def test_hold_when_healthy(self):
        conn = _make_conn()
        plugin = NewsIntelV1Strategy()
        position = {"condition_id": "cond_001", "outcome": "Yes", "entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.50, signal_open=True)

        decision = plugin.exit(conn, {"parameters": {}}, position, state)

        self.assertFalse(decision.should_exit)
        self.assertEqual(decision.reason, "hold")


# =============================================================================
# Registry
# =============================================================================


class TestNewsIntelRegistry(unittest.TestCase):

    def test_news_intel_in_registry(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("news_intel_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "news_intel_v1")

    def test_list_plugins_includes_news_intel(self):
        from sniper.strategies.registry import list_strategy_plugins
        plugins = list_strategy_plugins()
        ids = [p.plugin_id for p in plugins]
        self.assertIn("news_intel_v1", ids)


if __name__ == "__main__":
    unittest.main()
