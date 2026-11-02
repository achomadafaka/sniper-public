import sqlite3
import unittest
from unittest.mock import patch

import pytest

import paper_trader


def _make_trader() -> paper_trader.PaperTrader:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE paper_trades (
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
            position_value_at_entry REAL,
            stop_loss_price REAL,
            take_profit_price REAL,
            signal_type TEXT DEFAULT 'unknown',
            exit_classification TEXT,
            stale_flagged_at TIMESTAMP,
            status TEXT DEFAULT 'open',
            pnl_usdc REAL,
            roi_percent REAL,
            exit_reason TEXT,
            signal_insider_score REAL,
            signal_speculator_score REAL,
            wallet_win_rate REAL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            alert_sent_entry INTEGER DEFAULT 0,
            alert_sent_exit INTEGER DEFAULT 0,
            duplicate_of INTEGER,
            position_signal_id INTEGER,
            client_request_id TEXT,
            signal_entry_price REAL,
            slippage_pct REAL,
            signal_disqualified_at TEXT,
            signal_disqualification_reason TEXT,
            peak_price REAL,
            adverse_trend_streak INTEGER DEFAULT 0
        );

        CREATE TABLE position_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            latest_trade_ts INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
    trader.conn = conn
    trader.send_entry_alert = lambda *args, **kwargs: None
    return trader


def _base_signal(*, title: str, slug: str, condition_id: str = "cond1") -> dict:
    return {
        "wallet": "0xabc123",
        "condition_id": condition_id,
        "outcome": "YES",
        "title": title,
        "slug": slug,
        "current_price": 0.55,
        "entry_price": 0.55,
        "position_insider_score": 84.0,
        "position_speculator_score": 10.0,
        "win_rate": 0.65,
        "qualifying_roi": 25.0,
        "wallet_classification": "emerging_insider",
        "wallet_confidence_score": 60.0,
        "position_classification": "position_insider_candidate",
        "position_value": 100.0,
    }


@pytest.mark.integration
class TestPaperTraderWorldBoosts(unittest.TestCase):
    def setUp(self) -> None:
        self.trader = _make_trader()

    def tearDown(self) -> None:
        self.trader.conn.close()

    @patch("paper_trader._load_world_boosts")
    def test_sports_market_does_not_get_world_boost_credit(self, mock_load_world_boosts):
        mock_load_world_boosts.return_value = {
            "cond1": {
                "boost": 6,
                "_raw_boost": 6,
                "_confidence_weight": 1.0,
                "confidence": "high",
                "direction": "up",
                "reason": "irrelevant global context",
            }
        }
        signal = _base_signal(
            title="Will the Phoenix Suns beat the Minnesota Timberwolves?",
            slug="suns-vs-timberwolves",
        )

        self.trader.enter_position(signal)

        self.assertEqual(signal["_effective_insider_score"], 84.0)
        row = self.trader.conn.execute("SELECT COUNT(*) AS count FROM paper_trades").fetchone()
        self.assertEqual(row["count"], 0)

    @patch("paper_trader._load_world_boosts")
    def test_non_sports_market_keeps_world_boost(self, mock_load_world_boosts):
        mock_load_world_boosts.return_value = {
            "cond2": {
                "boost": 6,
                "_raw_boost": 6,
                "_confidence_weight": 1.0,
                "confidence": "high",
                "direction": "up",
                "reason": "world event tailwind",
            }
        }
        signal = _base_signal(
            title="Will Trump announce a vice presidential pick by July 31?",
            slug="trump-vp-pick-july-31",
            condition_id="cond2",
        )

        self.trader.enter_position(signal)

        self.assertEqual(signal["_effective_insider_score"], 90.0)
        row = self.trader.conn.execute("SELECT COUNT(*) AS count FROM paper_trades").fetchone()
        self.assertEqual(row["count"], 1)


if __name__ == "__main__":
    unittest.main()
