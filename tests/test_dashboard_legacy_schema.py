import unittest
from flask import Flask
from unittest.mock import patch

from db import (
    ensure_paper_trades_duplicate_of_column,
    ensure_strategy_positions_live_price_columns,
    ensure_user_analysis_probabilistic_columns,
    get_connection,
    init_db,
)
from sniper.persistence.db_signals import position_signals
from sniper.routes.api_core import create_api_core_blueprint
from sniper.routes.api_misc import create_api_misc_blueprint
from sniper.routes.api_strategies import create_api_strategies_blueprint


LEGACY_PAPER_TRADES_SQL = """
CREATE TABLE paper_trades (
    trade_id INTEGER PRIMARY KEY,
    wallet TEXT,
    condition_id TEXT,
    outcome TEXT,
    market_title TEXT,
    direction TEXT,
    entry_price REAL,
    current_price REAL,
    exit_price REAL,
    size_usdc REAL,
    pnl_usdc REAL,
    roi_percent REAL,
    signal_type TEXT,
    exit_classification TEXT,
    exit_reason TEXT,
    signal_insider_score REAL,
    wallet_win_rate REAL,
    signal_entry_price REAL,
    slippage_pct REAL,
    opened_at TEXT,
    closed_at TEXT,
    status TEXT,
    position_signal_id INTEGER,
    shares REAL
)
"""

LEGACY_USER_ANALYSIS_SQL = """
CREATE TABLE user_analysis (
    wallet TEXT PRIMARY KEY,
    win_rate REAL NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    closed_sample_size INTEGER NOT NULL DEFAULT 0,
    total_closed_count INTEGER NOT NULL DEFAULT 0,
    resolution_rate REAL NOT NULL DEFAULT 0,
    roi_percent REAL NOT NULL DEFAULT 0,
    qualifying_roi REAL,
    qualifying_pnl REAL NOT NULL DEFAULT 0,
    qualifying_capital REAL NOT NULL DEFAULT 0,
    effective_resolved_wins INTEGER NOT NULL DEFAULT 0,
    effective_resolved_losses INTEGER NOT NULL DEFAULT 0,
    account_age_days REAL NOT NULL DEFAULT 99999,
    is_fresh_account INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'watch',
    classification TEXT NOT NULL DEFAULT 'low_signal',
    position_count INTEGER NOT NULL DEFAULT 0,
    strategy_group TEXT NOT NULL DEFAULT 'balanced_mixed',
    strategy_confidence REAL NOT NULL DEFAULT 0
)
"""

LEGACY_STRATEGY_POSITIONS_SQL = """
CREATE TABLE strategy_positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    state TEXT NOT NULL,
    entry_decision_id INTEGER,
    exit_decision_id INTEGER,
    token_id TEXT NOT NULL DEFAULT '',
    entry_price REAL NOT NULL DEFAULT 0,
    avg_entry_price REAL NOT NULL DEFAULT 0,
    entry_shares REAL NOT NULL DEFAULT 0,
    filled_entry_shares REAL NOT NULL DEFAULT 0,
    avg_exit_price REAL NOT NULL DEFAULT 0,
    filled_exit_shares REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    opened_ts INTEGER NOT NULL DEFAULT 0,
    closed_ts INTEGER NOT NULL DEFAULT 0,
    last_reconciled_ts INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL
)
"""


def _legacy_conn():
    conn = get_connection(":memory:")
    init_db(conn)
    conn.execute("DROP TABLE IF EXISTS paper_trades")
    conn.execute(LEGACY_PAPER_TRADES_SQL)
    conn.commit()
    return conn


def _legacy_user_analysis_conn():
    conn = get_connection(":memory:")
    init_db(conn)
    conn.execute("DROP TABLE IF EXISTS user_analysis")
    conn.execute(LEGACY_USER_ANALYSIS_SQL)
    conn.commit()
    return conn


def _legacy_strategy_positions_conn():
    conn = _legacy_conn()
    conn.execute("DROP TABLE IF EXISTS strategy_positions")
    conn.execute(LEGACY_STRATEGY_POSITIONS_SQL)
    conn.commit()
    return conn


def _app_with_blueprint(bp):
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app


class TestDashboardLegacySchema(unittest.TestCase):
    def tearDown(self):
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

    def test_duplicate_of_helper_backfills_legacy_table(self):
        self.conn = _legacy_conn()
        before = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
        self.assertNotIn("duplicate_of", before)

        ensure_paper_trades_duplicate_of_column(self.conn)

        after = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
        self.assertIn("duplicate_of", after)

    def test_user_analysis_helper_backfills_probabilistic_columns(self):
        self.conn = _legacy_user_analysis_conn()
        before = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(user_analysis)").fetchall()
        }
        self.assertNotIn("wallet_confidence_score", before)
        self.assertNotIn("wallet_tier", before)

        ensure_user_analysis_probabilistic_columns(self.conn)

        after = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(user_analysis)").fetchall()
        }
        self.assertIn("wallet_confidence_score", after)
        self.assertIn("wallet_tier", after)
        self.assertIn("wallet_domain_stats", after)
        self.assertIn("insider_score_trend", after)
        self.assertIn("effective_copy_fraction", after)

    def test_strategy_positions_helper_backfills_live_price_columns(self):
        self.conn = _legacy_strategy_positions_conn()
        before = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(strategy_positions)").fetchall()
        }
        self.assertNotIn("current_price", before)
        self.assertNotIn("current_value", before)

        ensure_strategy_positions_live_price_columns(self.conn)

        after = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(strategy_positions)").fetchall()
        }
        self.assertIn("end_date_iso", after)
        self.assertIn("entry_insider_score", after)
        self.assertIn("pnl_usdc", after)
        self.assertIn("current_price", after)
        self.assertIn("current_value", after)

    def test_system_stats_route_handles_legacy_paper_trades_schema(self):
        self.conn = _legacy_conn()
        self.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                signal_type, exit_classification, exit_reason, signal_insider_score,
                opened_at, closed_at, status, shares
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xabc",
                "cond-1",
                "YES",
                "Legacy market",
                "buy_yes",
                0.45,
                0.60,
                1.0,
                20.0,
                11.0,
                55.0,
                "insider_copy_v1",
                "resolution_win",
                "",
                72.0,
                "2026-03-01T00:00:00+00:00",
                "2026-03-02T00:00:00+00:00",
                "resolution_win",
                44.4444,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_core_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/system-stats")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertIn("closed_trades", payload)
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
        self.assertIn("duplicate_of", cols)

    def test_all_positions_route_handles_legacy_paper_trades_schema(self):
        self.conn = _legacy_conn()
        self.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, pnl_usdc, roi_percent,
                signal_type, signal_insider_score, opened_at, status, shares
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xdef",
                "cond-2",
                "NO",
                "Open legacy market",
                "buy_no",
                0.35,
                0.41,
                15.0,
                None,
                None,
                "manual_copy",
                61.0,
                "2026-03-03T00:00:00+00:00",
                "open",
                42.8571,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_misc_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/all-positions")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertIn("stats", payload)
        self.assertEqual(payload["stats"]["count"], 1)
        self.assertEqual(payload["open_count"], 1)
        self.assertIn("duplicate_of", payload["items"][0])
        self.assertIsNone(payload["items"][0]["duplicate_of"])

    def test_all_positions_route_handles_legacy_strategy_positions_schema(self):
        self.conn = _legacy_strategy_positions_conn()
        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, status)
            VALUES (?, ?, ?, ?)
            """,
            ("0xbeef", "Legacy signal strategy", "signal_only", "active"),
        )
        strategy_id = int(
            self.conn.execute("SELECT id FROM strategies WHERE wallet = ?", ("0xbeef",)).fetchone()["id"]
        )
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xbeef",
                "cond-strat-1",
                "YES",
                str(strategy_id),
                "open",
                0.42,
                0.42,
                100.0,
                100.0,
                0.0,
                1_710_100_000,
                0,
                1_710_100_100,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_misc_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/all-positions")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(any(item.get("source") == "strategy" for item in payload["items"]))
        self.assertEqual(payload["count"], len(payload["items"]))
        self.assertIn("stats", payload)
        self.assertEqual(payload["stats"]["open_count"], payload["open_count"])
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(strategy_positions)").fetchall()
        }
        self.assertIn("current_price", cols)

    def test_system_stats_health_ok_matches_warn_level_health(self):
        self.conn = _legacy_conn()
        app = _app_with_blueprint(create_api_core_blueprint({"conn": self.conn}))

        def _fake_populate_health(result):
            result["health_details"] = {
                "stream": {"status": "ok", "proof_required": True},
                "wal": {"status": "warn", "proof_required": False},
            }

        with patch("sniper.mcp_tools.read_tools._populate_health", side_effect=_fake_populate_health):
            res = app.test_client().get("/api/system-stats")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertFalse(payload["health_ok"])
        self.assertTrue(payload["proof_health_ok"])

    def test_system_stats_reports_memecoin_telegram_config(self):
        self.conn = _legacy_conn()
        app = _app_with_blueprint(create_api_core_blueprint({"conn": self.conn}))

        with (
            patch("sniper.settings.memecoin.MEMECOIN_TG_ALERTS", True),
            patch("sniper.settings.memecoin.MEMECOIN_TG_BOT_TOKEN", "token"),
            patch("sniper.settings.memecoin.MEMECOIN_TG_CHAT_ID", "chat"),
        ):
            payload = app.test_client().get("/api/system-stats").get_json()

        self.assertEqual(
            payload["memecoin_telegram"],
            {"configured": True, "enabled": True},
        )

    def test_system_stats_active_strategy_count_includes_paper_trader_card(self):
        self.conn = _legacy_conn()
        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, status)
            VALUES (?, ?, ?, ?)
            """,
            ("0xabc", "Test strategy", "signal_only", "active"),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_core_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/system-stats")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["active_strategies"], 2)

    def test_system_stats_exposes_browser_safe_pending_alert_count(self):
        self.conn = _legacy_conn()
        for dedupe_key, status in (("new-alert", "new"), ("sent-alert", "sent"),
                                   ("acked-alert", "acknowledged")):
            self.conn.execute(
                "INSERT INTO alerts_outbox (dedupe_key, alert_type, status) VALUES (?, ?, ?)",
                (dedupe_key, "test", status),
            )
        self.conn.commit()
        app = _app_with_blueprint(create_api_core_blueprint({"conn": self.conn}))

        payload = app.test_client().get("/api/system-stats").get_json()

        self.assertEqual(payload["pending_alert_count"], 2)

    def test_strategies_route_handles_legacy_paper_trades_schema(self):
        self.conn = _legacy_conn()
        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, status)
            VALUES (?, ?, ?, ?)
            """,
            ("0xfeed", "Legacy insider copy", "insider_copy_v1", "active"),
        )
        self.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, pnl_usdc, roi_percent,
                signal_type, signal_insider_score, opened_at, status, shares
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xfeed",
                "cond-3",
                "YES",
                "Strategy-linked market",
                "buy_yes",
                0.52,
                0.63,
                25.0,
                None,
                None,
                "insider_copy_v1",
                68.0,
                "2026-03-04T00:00:00+00:00",
                "open",
                48.0769,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_strategies_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/strategies")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertGreaterEqual(payload["count"], 1)
        self.assertTrue(any(item.get("label") == "Legacy insider copy" for item in payload["items"]))

    def test_strategies_route_handles_legacy_strategy_positions_schema(self):
        self.conn = _legacy_strategy_positions_conn()
        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, status)
            VALUES (?, ?, ?, ?)
            """,
            ("0xface", "Legacy autonomous strategy", "signal_only", "active"),
        )
        strategy_id = int(
            self.conn.execute("SELECT id FROM strategies WHERE wallet = ?", ("0xface",)).fetchone()["id"]
        )
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0xface",
                "cond-strat-2",
                "NO",
                str(strategy_id),
                "open",
                0.61,
                0.61,
                75.0,
                75.0,
                0.0,
                1_710_200_000,
                0,
                1_710_200_200,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(create_api_strategies_blueprint({"conn": self.conn}))
        res = app.test_client().get("/api/strategies")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(any(item.get("label") == "Legacy autonomous strategy" for item in payload["items"]))

    def test_open_position_signals_route_handles_legacy_user_analysis_schema(self):
        self.conn = _legacy_user_analysis_conn()
        wallet = "0x" + ("a" * 40)
        self.conn.execute(
            """
            INSERT INTO position_signals (
                wallet, condition_id, outcome, title, event_slug, is_open,
                position_value, position_share, position_count_wallet, entry_anchor_ts,
                entry_price, entry_size, entry_notional, current_price, current_size,
                latest_trade_ts, buys_1h, buys_24h, trades_1h, trades_24h,
                buys_since_entry, buy_notional_since_entry, net_notional_24h,
                last_trade_side, activity_state, hold_hours, position_insider_score,
                position_speculator_score, position_classification, signal_reason, analyzed_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                "cond-signal-1",
                "YES",
                "Legacy signal market",
                "legacy-signal-market",
                1,
                250.0,
                0.55,
                1,
                1_710_000_000,
                0.41,
                600.0,
                246.0,
                0.46,
                600.0,
                1_710_000_600,
                3,
                5,
                3,
                5,
                3,
                246.0,
                125.0,
                "BUY",
                "building",
                4.0,
                77.0,
                12.0,
                "position_watch",
                "legacy schema smoke test",
                1_710_000_900,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO user_analysis (
                wallet, win_rate, wins, losses, closed_sample_size, total_closed_count,
                resolution_rate, roi_percent, qualifying_roi, qualifying_pnl, qualifying_capital,
                effective_resolved_wins, effective_resolved_losses, account_age_days, is_fresh_account,
                tier, classification, position_count, strategy_group, strategy_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                0.66,
                6,
                3,
                9,
                9,
                0.78,
                12.5,
                10.2,
                125.0,
                1225.0,
                6,
                3,
                45.0,
                0,
                "watch",
                "position_watch",
                4,
                "balanced_mixed",
                0.61,
            ),
        )
        self.conn.commit()

        app = _app_with_blueprint(
            create_api_core_blueprint(
                {
                    "conn": self.conn,
                    "optional_float": lambda value: float(value) if value not in {None, ""} else None,
                    "safe_int": lambda value, default=0: int(value) if value not in {None, ""} else default,
                    "OPEN_SIGNALS_FOCUSED_DEFAULT": False,
                    "position_signals": position_signals,
                    "normalize_wallet": lambda value: str(value or "").strip().lower(),
                    "wallet_valid": lambda value: value.startswith("0x") and len(value) == 42,
                    "ignored_wallets": lambda conn: [],
                    "followed_positions": lambda conn, **kwargs: [],
                    "MAX_FOLLOWED_POSITION_UI_ROWS": 200,
                    "followed_position_key": lambda wallet, condition_id, outcome: (
                        wallet,
                        condition_id,
                        outcome,
                    ),
                    "wallet_profiles_map": lambda conn, wallets: {},
                    "wallet_identity": lambda wallet, profiles: {
                        "username": "",
                        "pseudonym": "",
                        "profile_image": "",
                    },
                    "format_unix": lambda ts: "",
                    "open_signal_is_focused": lambda item: True,
                    "open_signal_is_active_non_holding": lambda item: True,
                }
            )
        )
        res = app.test_client().get("/api/open-position-signals?show_positioned=1")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["wallet_confidence_score"], 0.0)
        self.assertEqual(payload["items"][0]["wallet_numeric_tier"], 0)
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(user_analysis)").fetchall()
        }
        self.assertIn("wallet_confidence_score", cols)
