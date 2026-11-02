"""Tests for MCP tool helpers and supporting db.py additions."""
import sqlite3
import time
import unittest
from unittest.mock import patch

from db import (
    CORE_SCHEMA_VERSION,
    get_connection,
    get_readonly_connection,
    init_db,
    insert_strategy,
    update_strategy_status,
    insert_mcp_audit_log,
)
from scripts.ops.health_check import get_health_summary


class TestReadonlyConnection(unittest.TestCase):
    def setUp(self):
        import tempfile, os

        self.fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.fd)
        conn = get_connection(self.db_path)
        init_db(conn)
        conn.execute(
            "INSERT INTO market_metadata (condition_id, title) VALUES ('c1', 'Test')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        import os

        os.unlink(self.db_path)

    def test_readonly_connection_can_read(self):
        ro = get_readonly_connection(self.db_path)
        rows = ro.execute(
            "SELECT title FROM market_metadata WHERE condition_id='c1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Test")
        ro.close()

    def test_readonly_connection_cannot_write(self):
        ro = get_readonly_connection(self.db_path)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO market_metadata (condition_id, title) VALUES ('c2', 'Bad')"
            )
        ro.close()


class TestHealthSummary(unittest.TestCase):
    def setUp(self):
        import tempfile, os

        self.fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.fd)
        conn = get_connection(self.db_path)
        init_db(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import os

        os.unlink(self.db_path)

    def test_health_summary_returns_expected_keys(self):
        summary = get_health_summary(self.db_path)
        self.assertIn("db_size_mb", summary)
        self.assertIn("table_counts", summary)
        self.assertIn("checks", summary)
        self.assertIsInstance(summary["table_counts"], dict)
        self.assertIsInstance(summary["checks"], list)

    def test_health_summary_on_empty_db(self):
        summary = get_health_summary(self.db_path)
        # market_metadata is created by init_db; paper_trades is not (created by paper_trader)
        self.assertEqual(summary["table_counts"].get("market_metadata", 0), 0)
        # paper_trades returns -1 (table missing) which is valid
        self.assertIn("paper_summary", summary)

    def test_health_summary_orphan_check_ignores_outcome_case(self):
        conn = get_connection(self.db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                pnl_usdc REAL,
                roi_percent REAL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                duplicate_of INTEGER
            );
            CREATE TABLE IF NOT EXISTS position_signals (
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO paper_trades (wallet, condition_id, outcome, status) VALUES ('0xabc', 'c1', 'YES', 'open')"
        )
        conn.execute(
            "INSERT INTO position_signals (wallet, condition_id, outcome) VALUES ('0xabc', 'c1', 'yes')"
        )
        conn.commit()
        conn.close()

        summary = get_health_summary(self.db_path)
        checks = {c["name"]: c for c in summary["checks"]}
        orphan_check = checks["orphaned_paper_trades"]
        self.assertTrue(orphan_check["passed"])
        self.assertEqual(orphan_check["count"], 0)
        self.assertIn("0 orphaned open paper trades", orphan_check["detail"])
        self.assertEqual(summary["paper_summary"]["open_count"], 1)

    def test_populate_health_does_not_fail_retired_low_buy_stream(self):
        from sniper.mcp_tools.read_tools import _populate_health

        health = {
            "db_path": self.db_path,
            "db_size_mb": 1.0,
            "table_counts": {"user_analysis": 0},
            "last_trade_ts": None,
            "paper_summary": {"open_count": 0},
            "checks": [
                {
                    "name": "orphaned_paper_trades",
                    "passed": True,
                    "count": 0,
                }
            ],
        }
        with (
            patch(
                "sniper.mcp_tools.read_tools.get_health_summary",
                return_value=health,
            ),
            patch(
                "sniper.mcp_tools.read_tools.get_telegram_health_summary",
                return_value={"status": "ok", "detail": "configured"},
            ),
        ):
            result = {"criteria": {"health": {}}}
            _populate_health(result)

        self.assertNotIn("stream", result["health_details"])
        self.assertEqual(result["criteria"]["health"]["status"], "pass")


class TestStrategyCRUD(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_insert_strategy_returns_id(self):
        sid = insert_strategy(
            self.conn,
            wallet="0xabc",
            strategy_type="insider_copy_v1",
            label="Test strat",
            parameters={"min_insider_score": 60},
            status="paused",
        )
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)

    def test_insert_strategy_stored_correctly(self):
        sid = insert_strategy(
            self.conn,
            wallet="0xdef",
            strategy_type="fade_v1",
            label="Fade test",
            parameters={"max_entry_price": 0.50},
            status="paused",
        )
        row = self.conn.execute(
            "SELECT * FROM strategies WHERE id = ?", (sid,)
        ).fetchone()
        self.assertEqual(row["wallet"], "0xdef")
        self.assertEqual(row["strategy_type"], "fade_v1")
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["label"], "Fade test")

    def test_update_strategy_status(self):
        sid = insert_strategy(
            self.conn,
            wallet="0xabc",
            strategy_type="insider_copy_v1",
            parameters={},
            status="paused",
        )
        updated = update_strategy_status(self.conn, sid, "active")
        self.assertTrue(updated)
        row = self.conn.execute(
            "SELECT status FROM strategies WHERE id = ?", (sid,)
        ).fetchone()
        self.assertEqual(row["status"], "active")

    def test_update_strategy_status_invalid_id(self):
        updated = update_strategy_status(self.conn, 9999, "active")
        self.assertFalse(updated)

    def test_update_strategy_status_rejects_invalid_status(self):
        sid = insert_strategy(
            self.conn,
            wallet="0xabc",
            strategy_type="insider_copy_v1",
            parameters={},
            status="paused",
        )
        with self.assertRaises(ValueError):
            update_strategy_status(self.conn, sid, "yolo")

    def test_insert_strategy_rejects_invalid_status(self):
        with self.assertRaises(ValueError):
            insert_strategy(
                self.conn,
                wallet="0xabc",
                strategy_type="insider_copy_v1",
                parameters={},
                status="actve",  # typo
            )


class TestResponseEnvelope(unittest.TestCase):
    def test_envelope_wraps_data(self):
        from sniper.mcp_tools.schemas import make_envelope

        data = {"wallets": 42}
        result = make_envelope(data, data_source="sniper_tracker.db")
        self.assertEqual(result["data"], data)
        self.assertEqual(result["data_source"], "sniper_tracker.db")
        self.assertIn("generated_at", result)
        self.assertIsNone(result["bias_warning"])
        self.assertIsInstance(result["freshness_seconds"], (int, float))

    def test_envelope_with_bias_warning(self):
        from sniper.mcp_tools.schemas import make_envelope

        result = make_envelope(
            {"x": 1},
            data_source="position_signals",
            bias_warning="signals source uses current prices (look-ahead bias)",
        )
        self.assertEqual(
            result["bias_warning"],
            "signals source uses current prices (look-ahead bias)",
        )

    def test_envelope_freshness_from_timestamp(self):
        from sniper.mcp_tools.schemas import make_envelope

        old_ts = time.time() - 120
        result = make_envelope({"x": 1}, data_source="db", last_write_ts=old_ts)
        self.assertGreaterEqual(result["freshness_seconds"], 119)
        self.assertLessEqual(result["freshness_seconds"], 125)

    def test_envelope_preserves_subsecond_freshness(self):
        from sniper.mcp_tools.schemas import make_envelope

        result = make_envelope({"x": 1}, data_source="db", last_write_ts=time.time() - 0.05)
        self.assertGreater(result["freshness_seconds"], 0)
        self.assertLess(result["freshness_seconds"], 1)


class TestGetRuntimeHealth(unittest.TestCase):
    def setUp(self):
        import tempfile, os

        self.fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.fd)
        conn = get_connection(self.db_path)
        init_db(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import os

        os.unlink(self.db_path)

    def test_runtime_health_returns_envelope(self):
        from sniper.mcp_tools.read_tools import runtime_health

        result = runtime_health(db_path=self.db_path)
        self.assertIn("generated_at", result)
        self.assertIn("data_source", result)
        self.assertIn("data", result)
        data = result["data"]
        self.assertIn("db_size_mb", data)
        self.assertIn("table_counts", data)

    def test_runtime_health_paper_summary(self):
        from sniper.mcp_tools.read_tools import runtime_health

        result = runtime_health(db_path=self.db_path)
        self.assertIn("paper_summary", result["data"])


class TestGetCurrentSignals(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        ts = int(time.time()) - 2
        # Seed position signal directly (bypass upsert gates for test simplicity)
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, is_open,
                entry_anchor_ts, entry_price, entry_size, entry_notional,
                current_price, current_size, latest_trade_ts,
                position_insider_score, position_speculator_score,
                position_classification, hold_hours, analyzed_ts)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xaaa", "cond1", "yes", "Will Test Resolve?",
             ts - 3600, 0.40, 100.0, 40.0,
             0.55, 100.0, ts,
             85.0, 15.0, "position_insider_candidate", 1.0, ts),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_get_current_signals_returns_signals(self):
        from sniper.mcp_tools.read_tools import current_signals

        result = current_signals(conn=self.conn, min_insider_score=50)
        self.assertIn("data", result)
        signals = result["data"]["signals"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["wallet"], "0xaaa")
        self.assertEqual(signals[0]["condition_id"], "cond1")

    def test_get_current_signals_filters_by_score(self):
        from sniper.mcp_tools.read_tools import current_signals

        result = current_signals(conn=self.conn, min_insider_score=90)
        signals = result["data"]["signals"]
        self.assertEqual(len(signals), 0)

    def test_get_current_signals_filters_by_price(self):
        from sniper.mcp_tools.read_tools import current_signals

        # entry_price is 0.40, should pass 0.50 cap
        result = current_signals(conn=self.conn, min_insider_score=50, max_entry_price=0.50)
        self.assertEqual(len(result["data"]["signals"]), 1)
        # Should fail 0.30 cap
        result = current_signals(conn=self.conn, min_insider_score=50, max_entry_price=0.30)
        self.assertEqual(len(result["data"]["signals"]), 0)

    def test_title_falls_back_to_market_metadata(self):
        """Fix 1: empty title should use market_title from joined metadata."""
        from sniper.mcp_tools.read_tools import current_signals

        ts = int(time.time())
        # Insert signal with empty title
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, is_open,
                entry_anchor_ts, entry_price, entry_size, entry_notional,
                current_price, current_size, latest_trade_ts,
                position_insider_score, position_speculator_score,
                position_classification, hold_hours, analyzed_ts)
               VALUES (?, ?, ?, '', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xbbb", "cond2", "yes",
             ts - 3600, 0.35, 50.0, 17.5,
             0.50, 50.0, ts,
             75.0, 10.0, "position_insider_candidate", 2.0, ts),
        )
        # Insert market metadata with a title
        self.conn.execute(
            "INSERT INTO market_metadata (condition_id, title) VALUES ('cond2', 'Metadata Title')"
        )
        self.conn.commit()
        result = current_signals(conn=self.conn, min_insider_score=70)
        signals = result["data"]["signals"]
        cond2 = [s for s in signals if s["condition_id"] == "cond2"]
        self.assertEqual(len(cond2), 1)
        self.assertEqual(cond2[0]["title"], "Metadata Title")

    def test_freshness_seconds_is_populated(self):
        """Fix 3: freshness_seconds should be > 0 for real data."""
        from sniper.mcp_tools.read_tools import current_signals

        result = current_signals(conn=self.conn, min_insider_score=50)
        # analyzed_ts was set to time.time(), so freshness should be small but > 0
        self.assertGreater(result["freshness_seconds"], 0)

    def test_get_current_signals_price_filter_applies_before_limit(self):
        """Price cap must not drop eligible rows beyond the initial sort window."""
        from sniper.mcp_tools.read_tools import current_signals

        ts = int(time.time())
        for i in range(6):
            self.conn.execute(
                """INSERT INTO position_signals
                   (wallet, condition_id, outcome, title, is_open,
                    entry_anchor_ts, entry_price, entry_size, entry_notional,
                    current_price, current_size, latest_trade_ts,
                    position_insider_score, position_speculator_score,
                    position_classification, hold_hours, analyzed_ts,
                    position_value, buys_1h)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"0xhigh{i}",
                    f"cond-high-{i}",
                    "yes",
                    f"High {i}",
                    ts - 3600,
                    0.95,
                    10.0,
                    9.5,
                    0.96,
                    10.0,
                    ts,
                    95.0 - i,
                    10.0,
                    "position_insider_candidate",
                    1.0,
                    ts,
                    1000.0 - i,
                    100 - i,
                ),
            )

        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, is_open,
                entry_anchor_ts, entry_price, entry_size, entry_notional,
                current_price, current_size, latest_trade_ts,
                position_insider_score, position_speculator_score,
                position_classification, hold_hours, analyzed_ts,
                position_value, buys_1h)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "0xgood",
                "cond-good",
                "yes",
                "Eligible",
                ts - 3600,
                0.40,
                10.0,
                4.0,
                0.50,
                10.0,
                ts,
                80.0,
                10.0,
                "position_insider_candidate",
                1.0,
                ts,
                500.0,
                50,
            ),
        )
        self.conn.commit()

        result = current_signals(
            conn=self.conn,
            min_insider_score=50,
            max_entry_price=0.70,
            limit=2,
        )
        signals = result["data"]["signals"]

        self.assertEqual(len(signals), 2)
        self.assertEqual(signals[0]["condition_id"], "cond1")
        self.assertEqual(signals[1]["condition_id"], "cond-good")


# ---- Task 7: get_wallet_profile ----
from db import upsert_user_analysis


class TestGetWalletProfile(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        upsert_user_analysis(
            self.conn, wallet="0xwallet1", analyzed_ts=int(time.time()),
            position_count=5, pnl=500.0, max_position_value=200.0,
            top_open_position_title="Big Market", top_open_position_value=200.0,
            total_open_value=400.0, trade_count=50, low_buy_hits_14d=10,
            avg_low_buy_notional_14d=30.0, market_diversity_14d=8,
            concentration_ratio=0.3, suspicion_score=0.1,
            insider_score=75, speculator_score=25,
            classification="insider_candidate", tier="high",
            strategy_group="conviction_sniper", strategy_confidence=0.9,
            strategy_note="strong edge", wins=20, losses=5,
            closed_sample_size=25, win_rate=0.80, high_winrate_qualified=True,
            first_activity_ts=int(time.time()) - 86400 * 60, account_age_days=60,
            is_fresh_account=False, total_closed_count=30,
            wallet_edge_tier="strong", commit=True,
        )

    def tearDown(self):
        self.conn.close()

    def test_wallet_profile_returns_data(self):
        from sniper.mcp_tools.read_tools import wallet_profile

        result = wallet_profile(wallet="0xwallet1", conn=self.conn)
        data = result["data"]
        self.assertEqual(data["wallet"], "0xwallet1")
        self.assertEqual(data["insider_score"], 75)
        self.assertEqual(data["classification"], "insider_candidate")
        self.assertEqual(data["closed_sample_size"], 25)
        self.assertEqual(data["total_closed_count"], 30)
        self.assertEqual(data["wallet_edge_tier"], "strong")

    def test_wallet_profile_not_found(self):
        from sniper.mcp_tools.read_tools import wallet_profile

        result = wallet_profile(wallet="0xnonexistent", conn=self.conn)
        self.assertIsNone(result["data"])


# ---- Task 8a: get_market_context ----

class TestGetMarketContext(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO market_metadata (condition_id, title, end_date_iso, volume_24h) VALUES (?, ?, ?, ?)",
            ("mkt1", "Will X happen?", "2026-06-01", 50000.0),
        )
        ts = int(time.time())
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, is_open,
                entry_anchor_ts, entry_price, entry_size, entry_notional,
                current_price, current_size, latest_trade_ts,
                position_insider_score, position_speculator_score,
                position_classification, hold_hours, analyzed_ts)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xaaa", "mkt1", "yes", "Will X happen?",
             ts - 3600, 0.40, 100.0, 40.0,
             0.55, 100.0, ts,
             85.0, 15.0, "position_insider_candidate", 1.0, ts),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_market_context_returns_data(self):
        from sniper.mcp_tools.read_tools import market_context

        result = market_context(condition_id="mkt1", conn=self.conn)
        data = result["data"]
        self.assertEqual(data["market"]["title"], "Will X happen?")
        self.assertEqual(data["market"]["volume_24h"], 50000.0)

    def test_market_context_unknown_market(self):
        from sniper.mcp_tools.read_tools import market_context

        result = market_context(condition_id="unknown", conn=self.conn)
        data = result["data"]
        self.assertEqual(data["market"]["title"], "Unknown")


# ---- Task 8b: get_paper_performance ----

class TestGetPaperPerformance(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        # Create paper_trades table matching production schema
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT,
                outcome TEXT, direction TEXT, entry_price REAL, exit_price REAL,
                size_usdc REAL, pnl_usdc REAL, roi_percent REAL,
                signal_insider_score REAL, signal_speculator_score REAL,
                status TEXT, opened_at TEXT, closed_at TEXT,
                duplicate_of INTEGER
            );
        """)
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 37.5, 150.0, 80, 20, "resolution_win", "2026-01-01", "2026-01-10"),
            ("0xa", "c2", "yes", "buy", 0.50, 0.0, 25.0, -25.0, -100.0, 70, 30, "resolution_loss", "2026-01-02", "2026-01-11"),
            ("0xb", "c3", "yes", "buy", 0.30, 1.0, 25.0, 58.3, 233.3, 90, 10, "resolution_win", "2026-01-03", "2026-01-12"),
        ]
        self.conn.executemany(
            "INSERT INTO paper_trades (wallet, condition_id, outcome, direction, entry_price, exit_price, size_usdc, pnl_usdc, roi_percent, signal_insider_score, signal_speculator_score, status, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trades,
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_paper_performance_returns_trades(self):
        from sniper.mcp_tools.read_tools import paper_performance

        result = paper_performance(conn=self.conn)
        data = result["data"]
        self.assertEqual(data["summary"]["count"], 3)
        self.assertEqual(data["summary"]["wins"], 2)
        self.assertGreater(data["summary"]["total_pnl"], 0)

    def test_paper_performance_filters_by_score(self):
        from sniper.mcp_tools.read_tools import paper_performance

        result = paper_performance(min_insider_score=85, conn=self.conn)
        data = result["data"]
        self.assertEqual(data["summary"]["count"], 1)  # only the 90-score trade


# ---- Task 8c: get_followable_leaderboard ----

class TestGetFollowableLeaderboard(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        # Seed a qualifying wallet
        upsert_user_analysis(
            self.conn, wallet="0xqualified", analyzed_ts=int(time.time()),
            position_count=5, pnl=1000.0, max_position_value=200.0,
            top_open_position_title="Market", top_open_position_value=200.0,
            total_open_value=400.0, trade_count=50, low_buy_hits_14d=10,
            avg_low_buy_notional_14d=30.0, market_diversity_14d=8,
            concentration_ratio=0.3, suspicion_score=0.1,
            insider_score=80, speculator_score=20,
            classification="insider_candidate", tier="high",
            strategy_group="conviction_sniper", strategy_confidence=0.9,
            strategy_note="", wins=20, losses=5,
            closed_sample_size=25, win_rate=0.80, high_winrate_qualified=True,
            first_activity_ts=int(time.time()) - 86400 * 10, account_age_days=60,
            is_fresh_account=False, total_closed_count=30,
            qualifying_roi=25.0, qualifying_capital=10000.0,
            roi_percent=25.0, wallet_edge_tier="strong", commit=True,
        )

    def tearDown(self):
        self.conn.close()

    def test_leaderboard_returns_qualified(self):
        from sniper.mcp_tools.read_tools import followable_leaderboard

        result = followable_leaderboard(conn=self.conn)
        data = result["data"]
        self.assertIn("qualified", data)
        self.assertIn("meta", data)


# ---- Task 8d: get_signal_history ----

class TestGetSignalHistory(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        ts = int(time.time())
        # signal_history is created by init_db
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, entry_price, current_price,
                position_insider_score, position_speculator_score,
                position_classification, activity_state, hold_hours,
                entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xaaa", "cond1", "yes", 0.40, 0.55, 85.0, 15.0,
             "position_insider_candidate", "holding", 2.0, ts - 7200, ts),
        )
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, entry_price, current_price,
                position_insider_score, position_speculator_score,
                position_classification, activity_state, hold_hours,
                entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("0xbbb", "cond2", "no", 0.60, 0.70, 70.0, 30.0,
             "position_insider_candidate", "active_buying", 5.0, ts - 18000, ts),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_signal_history_all(self):
        from sniper.mcp_tools.read_tools import signal_history

        result = signal_history(conn=self.conn)
        self.assertEqual(result["data"]["count"], 2)

    def test_signal_history_filter_by_wallet(self):
        from sniper.mcp_tools.read_tools import signal_history

        result = signal_history(wallet="0xaaa", conn=self.conn)
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["history"][0]["wallet"], "0xaaa")

    def test_signal_history_filter_by_condition(self):
        from sniper.mcp_tools.read_tools import signal_history

        result = signal_history(condition_id="cond2", conn=self.conn)
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["history"][0]["condition_id"], "cond2")


# ---- Task 9: Analyze tools ----

class TestAnalyzeTools(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT, condition_id TEXT,
                outcome TEXT, market_title TEXT, side TEXT,
                entry_price REAL, exit_price REAL,
                size_usdc REAL, pnl_usdc REAL, roi_pct REAL,
                signal_insider_score REAL, signal_speculator_score REAL,
                signal_classification TEXT, wallet_win_rate REAL,
                signal_type TEXT, status TEXT,
                opened_at TEXT, closed_at TEXT, roi_percent REAL,
                duplicate_of INTEGER
            );
            CREATE TABLE IF NOT EXISTS position_signals (
                wallet TEXT, condition_id TEXT, outcome TEXT, title TEXT,
                entry_price REAL, entry_notional REAL,
                position_insider_score REAL, position_speculator_score REAL,
                position_classification TEXT, analyzed_ts REAL,
                entry_anchor_ts REAL, is_open INTEGER DEFAULT 1,
                current_price REAL, hold_hours REAL
            );
            CREATE TABLE IF NOT EXISTS market_outcomes (
                condition_id TEXT, outcome TEXT,
                resolved_price REAL, resolved_at TEXT
            );
        """)
        trades = [
            ("0xa", "c1", "yes", "Market A", "buy", 0.40, 1.0, 25.0, 37.5, 150.0, 80, 20, "insider", 0.65, "copy", "resolution_win", "2026-01-01", "2026-01-10", 150.0),
            ("0xa", "c2", "yes", "Market B", "buy", 0.50, 0.0, 25.0, -25.0, -100.0, 70, 30, "insider", 0.65, "copy", "resolution_loss", "2026-01-02", "2026-01-11", -100.0),
            ("0xb", "c3", "yes", "Market C", "buy", 0.30, 1.0, 25.0, 58.3, 233.3, 90, 10, "insider", 0.80, "copy", "resolution_win", "2026-01-03", "2026-01-12", 233.3),
        ]
        self.conn.executemany(
            "INSERT INTO paper_trades (wallet, condition_id, outcome, market_title, side, entry_price, exit_price, size_usdc, pnl_usdc, roi_pct, signal_insider_score, signal_speculator_score, signal_classification, wallet_win_rate, signal_type, status, opened_at, closed_at, roi_percent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            trades,
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_analyze_historical_outcomes(self):
        from sniper.mcp_tools.analyze_tools import historical_outcomes

        result = historical_outcomes(
            data_source="paper_trades",
            min_insider_score=60,
            conn=self.conn,
        )
        self.assertIn("data", result)
        data = result["data"]
        self.assertGreater(data["trade_count"], 0)
        self.assertIn("win_rate", data)
        self.assertIn("total_pnl", data)
        self.assertIsNone(result["bias_warning"])

    def test_analyze_signals_has_bias_warning(self):
        from sniper.mcp_tools.analyze_tools import historical_outcomes

        result = historical_outcomes(
            data_source="position_signals",
            conn=self.conn,
        )
        self.assertIsNotNone(result["bias_warning"])
        self.assertIn("look-ahead", result["bias_warning"])

    def test_sweep_signal_filters(self):
        from sniper.mcp_tools.analyze_tools import sweep_filters

        result = sweep_filters(
            data_source="paper_trades",
            insider_range=[60, 90, 30],
            price_range=[0.50, 1.0, 0.50],
            conn=self.conn,
        )
        self.assertIn("data", result)
        grid = result["data"]
        self.assertIn("grid", grid)
        self.assertIn("best_cell", grid)


# ---- Chunk 1: Batch 3 fixes ----

class TestMarketContextMultiOutcome(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.conn.execute(
            "INSERT INTO market_metadata (condition_id, title) VALUES ('c_multi', 'Multi-Outcome Market')"
        )
        # YES-side wallets
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, entry_price, entry_notional,
                position_insider_score, position_speculator_score, position_classification,
                analyzed_ts, is_open, current_price, hold_hours)
               VALUES ('0xa1', 'c_multi', 'Yes', 'Multi-Outcome Market', 0.40, 5000.0,
                        80, 15, 'insider_candidate', ?, 1, 0.45, 10)""",
            (time.time(),),
        )
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, entry_price, entry_notional,
                position_insider_score, position_speculator_score, position_classification,
                analyzed_ts, is_open, current_price, hold_hours)
               VALUES ('0xa2', 'c_multi', 'Yes', 'Multi-Outcome Market', 0.42, 3000.0,
                        75, 20, 'insider_candidate', ?, 1, 0.46, 8)""",
            (time.time(),),
        )
        # NO-side wallet
        self.conn.execute(
            """INSERT INTO position_signals
               (wallet, condition_id, outcome, title, entry_price, entry_notional,
                position_insider_score, position_speculator_score, position_classification,
                analyzed_ts, is_open, current_price, hold_hours)
               VALUES ('0xb1', 'c_multi', 'No', 'Multi-Outcome Market', 0.55, 2000.0,
                        70, 25, 'insider_candidate', ?, 1, 0.50, 5)""",
            (time.time(),),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_returns_both_outcomes(self):
        from sniper.mcp_tools.read_tools import market_context
        result = market_context(condition_id="c_multi", conn=self.conn)
        signals = result["data"]["active_signals"]
        self.assertIsInstance(signals, list)
        outcomes = {s["outcome"] for s in signals}
        self.assertEqual(outcomes, {"Yes", "No"})

    def test_yes_side_has_correct_counts(self):
        from sniper.mcp_tools.read_tools import market_context
        result = market_context(condition_id="c_multi", conn=self.conn)
        signals = result["data"]["active_signals"]
        yes_signal = [s for s in signals if s["outcome"] == "Yes"][0]
        self.assertEqual(yes_signal["wallet_count"], 2)
        self.assertAlmostEqual(yes_signal["total_capital"], 8000.0, places=0)

    def test_no_side_has_correct_counts(self):
        from sniper.mcp_tools.read_tools import market_context
        result = market_context(condition_id="c_multi", conn=self.conn)
        signals = result["data"]["active_signals"]
        no_signal = [s for s in signals if s["outcome"] == "No"][0]
        self.assertEqual(no_signal["wallet_count"], 1)
        self.assertAlmostEqual(no_signal["total_capital"], 2000.0, places=0)

    def test_empty_market_returns_empty_list(self):
        from sniper.mcp_tools.read_tools import market_context
        result = market_context(condition_id="c_nonexistent", conn=self.conn)
        signals = result["data"]["active_signals"]
        self.assertIsInstance(signals, list)
        self.assertEqual(len(signals), 0)


class TestFreshnessSeconds(unittest.TestCase):
    """All MCP tools must return real freshness_seconds > 0 when data exists."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.now = time.time()
        from db import upsert_user_analysis
        # Seed timestamps 5s in the past so freshness_seconds is always > 0
        past = int(self.now) - 5
        upsert_user_analysis(
            self.conn, wallet="0xfresh", analyzed_ts=past,
            position_count=1, pnl=100.0, max_position_value=50.0,
            top_open_position_title="T", top_open_position_value=50.0,
            total_open_value=50.0, trade_count=10, low_buy_hits_14d=5,
            avg_low_buy_notional_14d=20.0, market_diversity_14d=3,
            concentration_ratio=0.3, suspicion_score=0.1,
            insider_score=80, speculator_score=20,
            classification="insider_candidate", tier="high",
            strategy_group="conviction_sniper", strategy_confidence=0.9,
            strategy_note="", wins=8, losses=2,
            closed_sample_size=10, win_rate=0.80, high_winrate_qualified=True,
            first_activity_ts=past - 86400, account_age_days=30,
            is_fresh_account=False, total_closed_count=12,
            qualifying_roi=25.0, qualifying_capital=5000.0,
            roi_percent=25.0, commit=True,
        )
        # Seed paper_trades
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT,
                outcome TEXT, direction TEXT, entry_price REAL, exit_price REAL,
                size_usdc REAL, pnl_usdc REAL, roi_percent REAL,
                signal_insider_score REAL, signal_speculator_score REAL,
                status TEXT, opened_at TEXT, closed_at TEXT,
                duplicate_of INTEGER
            );
        """)
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, direction, entry_price, exit_price,
                size_usdc, pnl_usdc, roi_percent, signal_insider_score,
                signal_speculator_score, status, opened_at, closed_at)
               VALUES ('0xfresh', 'c1', 'yes', 'buy', 0.40, 1.0, 25.0, 37.5,
                        150.0, 80, 20, 'resolution_win', '2026-01-01', '2026-01-10 12:00:00')""",
        )
        # Seed signal_history
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, entry_price, current_price,
                position_insider_score, position_speculator_score,
                position_classification, hold_hours, analyzed_ts)
               VALUES ('0xfresh', 'c1', 'yes', 0.40, 0.50, 80, 20,
                        'insider_candidate', 10, ?)""",
            (past,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_wallet_profile_has_freshness(self):
        from sniper.mcp_tools.read_tools import wallet_profile
        result = wallet_profile(wallet="0xfresh", conn=self.conn)
        self.assertGreater(result["freshness_seconds"], 0)

    def test_paper_performance_has_freshness(self):
        from sniper.mcp_tools.read_tools import paper_performance
        result = paper_performance(conn=self.conn)
        self.assertGreater(result["freshness_seconds"], 0)

    def test_followable_leaderboard_has_freshness(self):
        from sniper.mcp_tools.read_tools import followable_leaderboard
        result = followable_leaderboard(conn=self.conn)
        self.assertGreater(result["freshness_seconds"], 0)

    def test_signal_history_has_freshness(self):
        from sniper.mcp_tools.read_tools import signal_history
        result = signal_history(wallet="0xfresh", conn=self.conn)
        self.assertGreater(result["freshness_seconds"], 0)


# ---- Chunk 2: Shared strategy service ----

class TestStrategyService(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_validate_wallet_valid(self):
        from sniper.services.strategy_service import validate_wallet
        self.assertEqual(validate_wallet("0x" + "a" * 40), "0x" + "a" * 40)

    def test_validate_wallet_normalizes_case(self):
        from sniper.services.strategy_service import validate_wallet
        self.assertEqual(validate_wallet("0x" + "A" * 40), "0x" + "a" * 40)

    def test_validate_wallet_invalid_raises(self):
        from sniper.services.strategy_service import validate_wallet
        with self.assertRaises(ValueError):
            validate_wallet("not_a_wallet")

    def test_validate_wallet_empty_raises(self):
        from sniper.services.strategy_service import validate_wallet
        with self.assertRaises(ValueError):
            validate_wallet("")

    def test_validate_strategy_type_valid(self):
        from sniper.services.strategy_service import validate_strategy_type
        self.assertEqual(validate_strategy_type("insider_copy_v1"), "insider_copy_v1")

    def test_validate_strategy_type_invalid_raises(self):
        from sniper.services.strategy_service import validate_strategy_type
        with self.assertRaises(ValueError):
            validate_strategy_type("nonexistent_type")

    def test_validate_parameters_dict(self):
        from sniper.services.strategy_service import validate_parameters
        self.assertEqual(validate_parameters({"a": 1}), {"a": 1})

    def test_validate_parameters_json_string(self):
        from sniper.services.strategy_service import validate_parameters
        self.assertEqual(validate_parameters('{"a": 1}'), {"a": 1})

    def test_validate_parameters_invalid_returns_empty(self):
        from sniper.services.strategy_service import validate_parameters
        self.assertEqual(validate_parameters("not json"), {})

    def test_create_strategy_returns_paused(self):
        from sniper.services.strategy_service import create_strategy
        result = create_strategy(
            self.conn,
            wallet="0x" + "a" * 40,
            label="test",
            strategy_type="insider_copy_v1",
            parameters={"max_entry_price": 0.70},
        )
        self.assertEqual(result["status"], "paused")
        self.assertIn("id", result)

    def test_create_strategy_invalid_wallet_raises(self):
        from sniper.services.strategy_service import create_strategy
        with self.assertRaises(ValueError):
            create_strategy(
                self.conn, wallet="bad", label="", strategy_type="insider_copy_v1", parameters={},
            )

    def test_activate_strategy_from_paused(self):
        from sniper.services.strategy_service import create_strategy, activate_strategy
        created = create_strategy(
            self.conn,
            wallet="0x" + "a" * 40,
            label="test",
            strategy_type="insider_copy_v1",
            parameters={},
        )
        result = activate_strategy(self.conn, created["id"])
        self.assertEqual(result["status"], "active")

    def test_activate_strategy_already_active_raises(self):
        from sniper.services.strategy_service import create_strategy, activate_strategy
        created = create_strategy(
            self.conn,
            wallet="0x" + "a" * 40,
            label="test",
            strategy_type="insider_copy_v1",
            parameters={},
        )
        activate_strategy(self.conn, created["id"])
        with self.assertRaises(ValueError):
            activate_strategy(self.conn, created["id"])

    def test_activate_strategy_not_found_raises(self):
        from sniper.services.strategy_service import activate_strategy
        with self.assertRaises(ValueError):
            activate_strategy(self.conn, 99999)


class TestMcpAuditLog(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_table_created(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_audit_log'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_insert_and_read(self):
        row_id = insert_mcp_audit_log(
            self.conn,
            tool_name="create_paper_strategy",
            caller="test",
            args_json='{"wallet": "0xabc"}',
            result_summary="created id=1",
        )
        self.assertIsInstance(row_id, int)
        row = self.conn.execute(
            "SELECT * FROM mcp_audit_log WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["tool_name"], "create_paper_strategy")
        self.assertEqual(row["caller"], "test")
        self.assertEqual(row["result_summary"], "created id=1")
        self.assertEqual(row["error"], "")

    def test_insert_with_error(self):
        row_id = insert_mcp_audit_log(
            self.conn,
            tool_name="activate_paper_strategy",
            error="strategy not found",
        )
        row = self.conn.execute(
            "SELECT * FROM mcp_audit_log WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row["error"], "strategy not found")
        self.assertEqual(row["args_json"], "{}")

    def test_index_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_mcp_audit_log_tool_created'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_init_db_recreates_mcp_audit_log_when_missing_on_current_schema(self):
        self.conn.execute("DROP TABLE mcp_audit_log")
        self.conn.execute(f"PRAGMA user_version = {CORE_SCHEMA_VERSION}")
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_audit_log'"
        ).fetchall()
        self.assertEqual(len(rows), 0)

        init_db(self.conn)

        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mcp_audit_log'"
        ).fetchall()
        self.assertEqual(len(rows), 1)


class TestMcpWriteTools(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_create_paper_strategy_returns_paused(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        result = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            label="test strat",
            parameters='{"max_entry_price": 0.70}',
            conn=self.conn,
        )
        self.assertEqual(result["data"]["status"], "paused")
        self.assertIn("id", result["data"])
        self.assertIn("freshness_seconds", result)

    def test_create_paper_strategy_invalid_wallet_returns_error(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        result = create_paper_strategy_impl(
            wallet="bad",
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        self.assertIn("error", result["data"])

    def test_create_paper_strategy_writes_audit_log(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        rows = self.conn.execute(
            "SELECT * FROM mcp_audit_log WHERE tool_name = 'create_paper_strategy'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("created id=", rows[0]["result_summary"])
        self.assertEqual(rows[0]["error"], "")
        self.assertEqual(rows[0]["caller"], "mcp")

    def test_activate_paper_strategy_from_paused(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl, activate_paper_strategy_impl
        created = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        strategy_id = created["data"]["id"]
        result = activate_paper_strategy_impl(strategy_id=strategy_id, confirm=True, conn=self.conn)
        self.assertEqual(result["data"]["status"], "active")

    def test_activate_paper_strategy_without_confirm_returns_error(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl, activate_paper_strategy_impl
        created = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        strategy_id = created["data"]["id"]
        result = activate_paper_strategy_impl(strategy_id=strategy_id, conn=self.conn)
        self.assertIn("error", result["data"])
        self.assertIn("confirm=true", result["data"]["error"])

    def test_activate_paper_strategy_already_active_returns_error(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl, activate_paper_strategy_impl
        created = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        strategy_id = created["data"]["id"]
        activate_paper_strategy_impl(strategy_id=strategy_id, confirm=True, conn=self.conn)
        result = activate_paper_strategy_impl(strategy_id=strategy_id, confirm=True, conn=self.conn)
        self.assertIn("error", result["data"])

    def test_activate_paper_strategy_not_found_returns_error(self):
        from sniper.mcp_tools.write_tools import activate_paper_strategy_impl
        result = activate_paper_strategy_impl(strategy_id=99999, confirm=True, conn=self.conn)
        self.assertIn("error", result["data"])

    def test_activate_writes_audit_log(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl, activate_paper_strategy_impl
        created = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            conn=self.conn,
        )
        activate_paper_strategy_impl(
            strategy_id=created["data"]["id"], confirm=True, conn=self.conn,
        )
        rows = self.conn.execute(
            "SELECT * FROM mcp_audit_log WHERE tool_name = 'activate_paper_strategy'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("activated id=", rows[0]["result_summary"])
        self.assertEqual(rows[0]["caller"], "mcp")

    def test_create_invalid_strategy_type_returns_error(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        result = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="nonexistent_type",
            conn=self.conn,
        )
        self.assertIn("error", result["data"])

    def test_create_malformed_json_parameters_returns_error(self):
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        result = create_paper_strategy_impl(
            wallet="0x" + "a" * 40,
            strategy_type="insider_copy_v1",
            parameters="{bad json",
            conn=self.conn,
        )
        self.assertIn("error", result["data"])
        self.assertIn("not valid JSON", result["data"]["error"])
        # Verify no strategy was created
        rows = self.conn.execute("SELECT * FROM strategies").fetchall()
        self.assertEqual(len(rows), 0)

    def test_unexpected_exception_returns_internal_error(self):
        from unittest.mock import patch
        from sniper.mcp_tools.write_tools import create_paper_strategy_impl
        with patch("sniper.mcp_tools.write_tools.create_strategy", side_effect=RuntimeError("boom")):
            result = create_paper_strategy_impl(
                wallet="0x" + "a" * 40,
                strategy_type="insider_copy_v1",
                conn=self.conn,
            )
        self.assertEqual(result["data"]["error"], "internal error")
        rows = self.conn.execute(
            "SELECT * FROM mcp_audit_log WHERE error = 'boom'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
