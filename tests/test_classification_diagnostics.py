import importlib
import os
import sys
import tempfile
import time
import unittest

from db import classification_diagnostics, get_connection, init_db
from sniper.mcp_tools.read_tools import classification_diagnostics_mcp


def _insert_analysis_row(
    conn,
    *,
    wallet: str,
    insider_score: float,
    classification: str,
    reason: str,
    closed_sample_size: int = 10,
    matches_criteria: bool = False,
    max_position_insider_score: float = 0.0,
    position_insider_candidate_count: int = 0,
    analyzed_ts: int = 1000,
) -> None:
    conn.execute(
        """
        INSERT INTO user_analysis (
            wallet, analyzed_ts, position_count, pnl, max_position_value, total_open_value,
            trade_count, insider_score, speculator_score, classification, closed_sample_size,
            position_signal_count, position_insider_candidate_count, position_speculator_count,
            max_position_insider_score, max_position_speculator_score, matches_criteria, reason
        ) VALUES (?, ?, 1, 100.0, 12000.0, 15000.0, 5, ?, 10.0, ?, ?, 1, ?, 0, ?, 0.0, ?, ?)
        """,
        (
            wallet,
            analyzed_ts,
            insider_score,
            classification,
            closed_sample_size,
            position_insider_candidate_count,
            max_position_insider_score,
            1 if matches_criteria else 0,
            reason,
        ),
    )


class TestClassificationDiagnosticsHelper(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        now_ts = int(time.time())

        _insert_analysis_row(
            self.conn,
            wallet="0x1111111111111111111111111111111111111111",
            insider_score=95.0,
            classification="low_signal",
            reason=(
                "insider_score_gte_threshold=yes, emerging_score_gte_threshold=yes, "
                "repeat_or_position_signal_ok=no, position_signal_strength_ok=no, "
                "classification=low_signal, insider_score=95.0"
            ),
            max_position_insider_score=42.0,
        )
        _insert_analysis_row(
            self.conn,
            wallet="0x2222222222222222222222222222222222222222",
            insider_score=88.0,
            classification="low_signal",
            reason=(
                "insider_score_gte_threshold=yes, emerging_score_gte_threshold=yes, "
                "total_open_value_ok=no, repeat_or_position_signal_ok=no, "
                "classification=low_signal, insider_score=88.0"
            ),
            max_position_insider_score=50.0,
        )
        _insert_analysis_row(
            self.conn,
            wallet="0x3333333333333333333333333333333333333333",
            insider_score=90.0,
            classification="emerging_insider",
            reason=(
                "insider_score_gte_threshold=yes, emerging_score_gte_threshold=yes, "
                "repeat_or_position_signal_ok=yes, classification=emerging_insider"
            ),
            matches_criteria=True,
            position_insider_candidate_count=1,
            max_position_insider_score=71.0,
        )
        self.conn.execute(
            """
            INSERT INTO position_signals (
                wallet, condition_id, outcome, title, event_slug, is_open,
                position_value, position_share, position_count_wallet,
                entry_anchor_ts, entry_price, entry_size, entry_notional,
                current_price, current_size, latest_trade_ts,
                position_insider_score, position_speculator_score,
                position_classification, analyzed_ts
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0x9999999999999999999999999999999999999999",
                "cond-1",
                "yes",
                "Test Market",
                "test-market",
                5000.0,
                10.0,
                1,
                now_ts - 100,
                0.50,
                100.0,
                50.0,
                0.52,
                100.0,
                now_ts - 50,
                32.0,
                10.0,
                "position_low_signal",
                now_ts,
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_helper_summarizes_failed_checks(self) -> None:
        result = classification_diagnostics(self.conn, min_insider_score=55.0, limit=10)

        summary = result["summary"]
        self.assertEqual(summary["wallets_above_threshold"], 3)
        self.assertEqual(summary["low_signal_wallets"], 2)
        self.assertEqual(summary["insider_like_wallets"], 1)
        self.assertEqual(summary["open_signals"], 1)
        self.assertEqual(summary["open_signals_meeting_insider_threshold"], 0)
        self.assertEqual(summary["max_open_signal_insider_score"], 32.0)

        top_checks = {item["check"]: item["count"] for item in result["top_failed_checks"]}
        self.assertEqual(top_checks["repeat_or_position_signal_ok"], 2)
        self.assertEqual(top_checks["position_signal_strength_ok"], 1)
        self.assertEqual(top_checks["total_open_value_ok"], 1)

        sample = result["sample_low_signal_wallets"][0]
        self.assertIn("failed_checks", sample)
        self.assertEqual(sample["classification"], "low_signal")

    def test_mcp_tool_wraps_diagnostics(self) -> None:
        result = classification_diagnostics_mcp(
            min_insider_score=55.0,
            limit=5,
            conn=self.conn,
        )
        self.assertEqual(result["data_source"], "user_analysis")
        self.assertIn("top_failed_checks", result["data"])
        self.assertEqual(result["data"]["summary"]["low_signal_wallets"], 2)


class TestClassificationDiagnosticsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_classification_diag.db")
        # Reimport only the server, which owns the DB connection. Route blueprints
        # receive the new connection through ctx. Removing route modules leaves stale
        # function objects in other test modules and breaks later monkeypatches.
        sys.modules.pop("sniper.web.server", None)
        cls.web = importlib.import_module("sniper.web.server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        conn = self.web.conn
        conn.execute("DELETE FROM user_analysis")
        conn.execute("DELETE FROM position_signals")
        _insert_analysis_row(
            conn,
            wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            insider_score=91.0,
            classification="low_signal",
            reason=(
                "insider_score_gte_threshold=yes, emerging_score_gte_threshold=yes, "
                "repeat_or_position_signal_ok=no, classification=low_signal"
            ),
        )
        _insert_analysis_row(
            conn,
            wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            insider_score=92.0,
            classification="emerging_insider",
            reason=(
                "insider_score_gte_threshold=yes, emerging_score_gte_threshold=yes, "
                "repeat_or_position_signal_ok=yes, classification=emerging_insider"
            ),
            matches_criteria=True,
            position_insider_candidate_count=1,
            max_position_insider_score=72.0,
        )
        conn.commit()

    def test_api_returns_diagnostics_payload(self) -> None:
        resp = self.client.get("/api/classification-diagnostics?min_insider_score=55&limit=5")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("summary", data)
        self.assertIn("top_failed_checks", data)
        self.assertIn("sample_low_signal_wallets", data)
        self.assertEqual(data["summary"]["wallets_above_threshold"], 2)
        self.assertEqual(data["summary"]["insider_like_wallets"], 1)
        self.assertEqual(data["summary"]["low_signal_wallets"], 1)


if __name__ == "__main__":
    unittest.main()
