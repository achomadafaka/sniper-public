import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import alerter
from db import get_connection, init_db


class AlerterOperatorCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)

        @contextmanager
        def _mock_scope(*_args, **_kwargs):
            yield self.conn

        self._mock_scope = _mock_scope
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                market_title TEXT,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                exit_price REAL,
                size_usdc REAL NOT NULL,
                shares REAL,
                signal_type TEXT DEFAULT 'unknown',
                exit_classification TEXT,
                status TEXT DEFAULT 'open',
                pnl_usdc REAL,
                roi_percent REAL,
                exit_reason TEXT,
                signal_insider_score REAL,
                signal_speculator_score REAL,
                wallet_win_rate REAL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                position_signal_id INTEGER,
                duplicate_of INTEGER,
                signal_disqualified_at TEXT,
                signal_disqualification_reason TEXT
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status, pnl_usdc)
            VALUES
              (1, '0x1111111111111111111111111111111111111111', 'Alpha', 'insider_copy_v1', '{}', 'active', 4.5),
              (2, '0x2222222222222222222222222222222222222222', 'Beta', 'insider_copy_v1', '{}', 'paused', -1.0)
            """
        )
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                opened_ts, updated_ts
            ) VALUES (
                '0x1111111111111111111111111111111111111111', 'cond-1', 'YES', '1', 'open',
                0.4, 0.4, 10, 10, 1, 1
            )
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_status_command_summarizes_strategy_runtime(self) -> None:
        with patch.object(alerter, "connection_scope", self._mock_scope):
            reply = alerter._operator_command_response("/status")
        self.assertIn("Sniper Status", reply)
        self.assertIn("2 total, 1 active, 1 paused", reply)
        self.assertIn("Open strategy positions: 1", reply)

    def test_pause_and_resume_commands_update_strategy_status(self) -> None:
        with patch.object(alerter, "connection_scope", self._mock_scope):
            pause_reply = alerter._operator_command_response("/pause 1")
            self.assertIn("set to *paused*", pause_reply)
            paused = self.conn.execute("SELECT status FROM strategies WHERE id = 1").fetchone()
            self.assertEqual(paused["status"], "paused")

            resume_reply = alerter._operator_command_response("/resume 1")
            self.assertIn("set to *active*", resume_reply)
            active = self.conn.execute("SELECT status FROM strategies WHERE id = 1").fetchone()
            self.assertEqual(active["status"], "active")

    def test_process_operator_commands_replies_to_authorized_chat(self) -> None:
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 101,
                    "message": {
                        "chat": {"id": "12345"},
                        "text": "/strategies",
                    },
                }
            ],
        }
        with (
            patch.object(alerter, "connection_scope", self._mock_scope),
            patch.object(alerter, "TELEGRAM_BOT_TOKEN", "token"),
            patch.object(alerter, "TELEGRAM_CHAT_ID", "12345"),
            patch.object(alerter, "requests") as mock_requests,
            patch.object(alerter, "send_telegram_message", return_value=True) as mock_send,
            patch.object(alerter, "_get_watermark_raw", return_value=0),
            patch.object(alerter, "_set_watermark") as mock_set,
        ):
            mock_requests.get.return_value = fake_response
            processed = alerter.process_telegram_operator_commands()

        self.assertEqual(processed, 1)
        mock_send.assert_called_once()
        mock_set.assert_called_once_with(alerter.ALERT_WATERMARK_KEY_TELEGRAM_UPDATE_ID, 101)

    def test_proofbar_command_returns_readiness_summary(self) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, exit_price, size_usdc, shares,
                status, pnl_usdc, roi_percent, signal_insider_score, opened_at, closed_at
            ) VALUES
                ('0x1111111111111111111111111111111111111111', 'cond-p1', 'YES', 'Market A', 'YES',
                 0.50, 0.90, 0.90, 100.0, 200.0, 'resolution_win', 80.0, 80.0, 65.0,
                 '2026-02-01 00:00:00', '2026-02-03 00:00:00'),
                ('0x2222222222222222222222222222222222222222', 'cond-p2', 'NO', 'Market B', 'NO',
                 0.40, 0.20, 0.20, 100.0, 250.0, 'resolution_loss', -50.0, -50.0, 70.0,
                 '2026-02-04 00:00:00', '2026-02-05 00:00:00')
            """
        )
        self.conn.commit()
        fake_health = {
            "db_size_mb": 1.23,
            "table_counts": {"user_analysis": 42},
            "last_trade_ts": 1770000000,
            "paper_summary": {"closed_count": 2},
            "checks": [
                {"name": "orphaned_paper_trades", "passed": True, "detail": "0 open paper trades with no matching signal"}
            ],
        }
        with (
            patch.object(alerter, "connection_scope", self._mock_scope),
            patch.object(alerter, "get_health_summary", return_value=fake_health),
        ):
            reply = alerter._operator_command_response("/proofbar")
        self.assertIn("Proof Bar", reply)
        self.assertIn("Overall", reply)
        self.assertIn("Trades: 2/150", reply)
        self.assertIn("WR: 50.0% / 60.0%", reply)
        self.assertIn("Health: pass", reply)

    def test_proofbar_health_uses_recent_telegram_activity_when_token_missing(self) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts_sent (alert_type, wallet, message, channel, sent_at)
            VALUES ('test_alert', '0xabc', 'hello', 'telegram', datetime('now', '-1 hour'))
            """
        )
        self.conn.commit()
        fake_health = {
            "db_size_mb": 1.23,
            "table_counts": {"user_analysis": 42},
            "last_trade_ts": 1770000000,
            "paper_summary": {"closed_count": 0},
            "checks": [
                {"name": "orphaned_paper_trades", "passed": True, "detail": "0 open paper trades with no matching signal"}
            ],
        }
        with (
            patch.object(alerter, "connection_scope", self._mock_scope),
            patch.object(alerter, "get_health_summary", return_value=fake_health),
            patch.object(alerter, "get_telegram_health_summary", return_value={"status": "ok", "detail": "Recent Telegram alert: 2026-03-13 12:00:00"}),
        ):
            result = {"criteria": {"health": {}}}
            alerter._proof_bar_health_summary(result)
        self.assertEqual(result["health_details"]["alerter"]["status"], "ok")
        self.assertIn("Recent Telegram alert:", result["health_details"]["alerter"]["detail"])

    def test_proofbar_health_warns_when_no_token_and_no_recent_alerts(self) -> None:
        fake_health = {
            "db_size_mb": 1.23,
            "table_counts": {"user_analysis": 42},
            "last_trade_ts": 1770000000,
            "paper_summary": {"closed_count": 0},
            "checks": [
                {"name": "orphaned_paper_trades", "passed": True, "detail": "0 open paper trades with no matching signal"}
            ],
        }
        with (
            patch.object(alerter, "connection_scope", self._mock_scope),
            patch.object(alerter, "get_health_summary", return_value=fake_health),
            patch.object(alerter, "get_telegram_health_summary", return_value={"status": "warn", "detail": "Check Telegram config"}),
        ):
            result = {"criteria": {"health": {}}}
            alerter._proof_bar_health_summary(result)
        self.assertEqual(result["health_details"]["alerter"]["status"], "warn")
        self.assertEqual(result["health_details"]["alerter"]["detail"], "Check Telegram config")

    def test_positions_command_summarizes_paper_and_real_positions(self) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_trades (
                trade_id, wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares, status,
                signal_insider_score, opened_at, signal_disqualified_at
            ) VALUES (
                7, '0x1111111111111111111111111111111111111111', 'cond-open', 'YES', 'Open Market', 'YES',
                0.40, 0.55, 80.0, 200.0, 'open',
                70.0, '2026-03-09 00:00:00', '2026-03-10 10:00:00'
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY,
                wallet TEXT,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO real_trades (
                id, wallet, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at
            ) VALUES (
                1, ?, 'cond-real', 'Real Market', 'real-market', 'YES', 'BUY', 0.25, 40.0, 10.0, 1772000000
            )
            """,
            (alerter.REAL_TRADES_DEFAULT_WALLET.lower(),),
        )
        self.conn.commit()
        with patch.object(alerter, "connection_scope", self._mock_scope):
            reply = alerter._operator_command_response("/positions")
        self.assertIn("Positions", reply)
        self.assertIn("Paper: 1 open", reply)
        self.assertIn("Open Market", reply)
        self.assertIn("disq", reply)
        self.assertIn("Real: 1 open", reply)
        self.assertIn("Real Market", reply)

    def test_strategy_command_returns_single_strategy_detail(self) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, status, started_at, finished_at, error_msg
            ) VALUES (
                1, 'completed', '2026-03-10 00:00:00', '2026-03-10 00:01:00', ''
            )
            """
        )
        self.conn.commit()
        with patch.object(alerter, "connection_scope", self._mock_scope):
            reply = alerter._operator_command_response("/strategy 1")
        self.assertIn("Strategy `1`", reply)
        self.assertIn("Alpha", reply)
        self.assertIn("Status: *active*", reply)
        self.assertIn("Open positions: 1", reply)
        self.assertIn("Last run: completed", reply)

    def test_mute_mutes_list_and_unmute_commands_manage_agent_mutes(self) -> None:
        with patch.object(alerter, "connection_scope", self._mock_scope):
            create_reply = alerter._operator_command_response(
                "/mute 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 2h noisy wallet"
            )
            self.assertIn("Mute `", create_reply)
            self.assertIn("wallet", create_reply)

            rows = self.conn.execute(
                "SELECT id, scope_type, scope_value, reason, expires_ts FROM agent_mutes"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            mute_id = int(rows[0]["id"])
            self.assertEqual(rows[0]["scope_type"], "wallet")
            self.assertEqual(rows[0]["scope_value"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
            self.assertEqual(rows[0]["reason"], "noisy wallet")

            listing = alerter._operator_command_response("/mutes")
            self.assertIn("Active Mutes", listing)
            self.assertIn(f"`{mute_id}`", listing)

            delete_reply = alerter._operator_command_response(f"/unmute {mute_id}")
            self.assertIn("removed", delete_reply)
            remaining = self.conn.execute("SELECT COUNT(*) AS cnt FROM agent_mutes").fetchone()
            self.assertEqual(int(remaining["cnt"]), 0)

    def test_unauthorized_chat_is_silently_dropped(self) -> None:
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 200,
                    "message": {
                        "chat": {"id": "999999999"},  # Wrong chat ID
                        "text": "/status",
                    },
                }
            ],
        }
        with (
            patch.object(alerter, "connection_scope", self._mock_scope),
            patch.object(alerter, "TELEGRAM_BOT_TOKEN", "token"),
            patch.object(alerter, "TELEGRAM_CHAT_ID", "12345"),
            patch.object(alerter, "requests") as mock_requests,
            patch.object(alerter, "send_telegram_message", return_value=True) as mock_send,
            patch.object(alerter, "_get_watermark_raw", return_value=0),
            patch.object(alerter, "_set_watermark"),
        ):
            mock_requests.get.return_value = fake_response
            processed = alerter.process_telegram_operator_commands()

        self.assertEqual(processed, 0)
        mock_send.assert_not_called()

    def test_db_exception_in_command_does_not_crash_loop(self) -> None:
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 300,
                    "message": {
                        "chat": {"id": "12345"},
                        "text": "/status",
                    },
                }
            ],
        }
        with (
            patch.object(alerter, "TELEGRAM_BOT_TOKEN", "token"),
            patch.object(alerter, "TELEGRAM_CHAT_ID", "12345"),
            patch.object(alerter, "requests") as mock_requests,
            patch.object(alerter, "send_telegram_message", return_value=True) as mock_send,
            patch.object(alerter, "_get_watermark_raw", return_value=0),
            patch.object(alerter, "_set_watermark"),
            patch.object(alerter, "_operator_command_response", side_effect=sqlite3.OperationalError("DB locked")),
        ):
            mock_requests.get.return_value = fake_response
            processed = alerter.process_telegram_operator_commands()

        self.assertEqual(processed, 0)
        mock_send.assert_not_called()

    def test_mute_position_command_creates_position_scope_mute(self) -> None:
        with patch.object(alerter, "connection_scope", self._mock_scope):
            reply = alerter._operator_command_response("/mute position cond-123 YES 1d thesis churn")
        self.assertIn("Mute `", reply)
        row = self.conn.execute(
            "SELECT scope_type, scope_value, reason FROM agent_mutes"
        ).fetchone()
        self.assertEqual(row["scope_type"], "position")
        self.assertEqual(row["scope_value"], "cond-123:yes")
        self.assertEqual(row["reason"], "thesis churn")
