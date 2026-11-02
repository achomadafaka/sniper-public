"""Tests for source exit correctness: disqualification tracking, exit verification."""
import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from contextlib import redirect_stdout

from db import get_connection, init_db


def _create_paper_trades_table(conn):
    """Create paper_trades table matching paper_trader._init_tables schema."""
    conn.executescript("""
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
            signal_disqualified_at TEXT,
            signal_disqualification_reason TEXT
        );
    """)


class TestDisqualificationColumns(unittest.TestCase):
    def test_columns_exist_after_init(self):
        conn = get_connection(":memory:")
        init_db(conn)
        _create_paper_trades_table(conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        self.assertIn("signal_disqualified_at", cols)
        self.assertIn("signal_disqualification_reason", cols)
        conn.close()

    def test_columns_default_null(self):
        conn = get_connection(":memory:")
        init_db(conn)
        _create_paper_trades_table(conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(conn)
        conn.execute(
            "INSERT INTO paper_trades (wallet, condition_id, outcome, direction, entry_price, size_usdc) "
            "VALUES ('0xabc', 'cond1', 'Yes', 'buy', 0.5, 25.0)"
        )
        row = conn.execute("SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades").fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        conn.close()


class TestMaybeMarkDisqualified(unittest.TestCase):
    def setUp(self):
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status) VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.5, 25.0, 'open')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_first_disqualification_sets_both_fields(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        row = self.conn.execute(
            "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "signal_is_open_0")

    def test_preserves_first_timestamp_on_repeat(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        first_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        second_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        self.assertEqual(first_ts, second_ts)

    def test_updates_reason_when_changed(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        first_ts = self.conn.execute(
            "SELECT signal_disqualified_at FROM paper_trades WHERE trade_id = 1"
        ).fetchone()[0]
        self.trader._maybe_mark_disqualified(1, "price_above_0.80")
        row = self.conn.execute(
            "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        self.assertEqual(row[0], first_ts)
        self.assertEqual(row[1], "price_above_0.80")

    def test_same_reason_is_noop(self):
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        self.trader._maybe_mark_disqualified(1, "signal_is_open_0")
        row = self.conn.execute(
            "SELECT signal_disqualification_reason FROM paper_trades WHERE trade_id = 1"
        ).fetchone()
        self.assertEqual(row[0], "signal_is_open_0")


class TestWalletPositionCache(unittest.TestCase):
    def setUp(self):
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.trader._wallet_positions_cache = {}

    @patch("paper_trader.requests.get")
    def test_cache_hit_avoids_second_api_call(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"market": "cond1", "size": 100.0, "curPrice": 0.5}]
        mock_get.return_value = mock_resp
        result1 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(len(result1), 1)
        self.assertEqual(mock_get.call_count, 1)
        result2 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(len(result2), 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("paper_trader.requests.get")
    def test_different_wallet_makes_new_call(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        self.trader._fetch_wallet_positions("0xabc")
        self.trader._fetch_wallet_positions("0xdef")
        self.assertEqual(mock_get.call_count, 2)

    @patch("paper_trader.requests.get")
    def test_cache_reset_between_cycles(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"market": "cond1", "size": 50.0}]
        mock_get.return_value = mock_resp
        self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(mock_get.call_count, 1)
        self.trader._wallet_positions_cache = {}
        self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(mock_get.call_count, 2)

    @patch("paper_trader.requests.get")
    def test_api_failure_does_not_cache(self, mock_get):
        """I12 fix: API failures should NOT be cached, so subsequent calls retry."""
        mock_get.side_effect = Exception("network error")
        result = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(result, [])
        result2 = self.trader._fetch_wallet_positions("0xabc")
        self.assertEqual(result2, [])
        # Both calls hit the API — failure result not cached
        self.assertEqual(mock_get.call_count, 2)


class TestCheckPositionExit(unittest.TestCase):
    def setUp(self):
        import paper_trader as pt_mod
        self.pt_mod = pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        self.trader._wallet_positions_cache = {}
        self.trader._sell_outcome_cache = {}

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS position_signals (
                rowid INTEGER PRIMARY KEY,
                wallet TEXT, condition_id TEXT, outcome TEXT,
                is_open INTEGER DEFAULT 1,
                entry_price REAL, current_price REAL,
                position_insider_score REAL, position_speculator_score REAL,
                position_classification TEXT,
                entry_anchor_ts REAL DEFAULT 0
            )
        """)
        # Use a dynamic recent date so the trade doesn't trigger the STALE_DAYS check
        from datetime import timedelta
        recent_dt = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status, opened_at, position_signal_id, market_title) "
            "VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.50, 25.0, 'open', ?, 100, 'Test Market')",
            (recent_dt,),
        )
        self.conn.execute(
            "INSERT INTO position_signals (rowid, wallet, condition_id, outcome, is_open, "
            "entry_price, current_price, position_insider_score) "
            "VALUES (100, '0xabc', 'cond1', 'Yes', 1, 0.50, 0.55, 70.0)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _get_trade(self):
        return self.conn.execute("SELECT * FROM paper_trades WHERE trade_id = 1").fetchone()

    @patch("paper_trader.requests.get")
    def test_is_open_zero_does_not_close(self, mock_get):
        """is_open=0 should NOT close the trade — only annotate."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.60}],
            "closed": False, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.60}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertEqual(trade["status"], "open")
        self.assertIsNotNone(trade["signal_disqualified_at"])
        self.assertEqual(trade["signal_disqualification_reason"], "signal_is_open_0")

    @patch("paper_trader.requests.get")
    def test_confirmed_full_exit_closes(self, mock_get):
        """Data-api confirms size < 0.01 -> should close with source_exited."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.65}],
            "closed": False, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 0.001, "curPrice": 0.65}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertEqual(trade["status"], "early_exit")
        self.assertEqual(trade["exit_reason"], "source_exited")

    @patch("paper_trader.requests.get")
    def test_market_closed_closes_trade(self, mock_get):
        """CLOB reports market closed -> should close trade."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.90}],
            "closed": True, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.90}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertNotEqual(trade["status"], "open")

    @patch("paper_trader.requests.get")
    def test_data_api_failure_keeps_trade_open_when_disqualified(self, mock_get):
        """If is_open=0 and data-api returns empty, trade stays open."""
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.60}],
            "closed": False, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = []
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertEqual(trade["status"], "open")
        self.assertIsNotNone(trade["signal_disqualified_at"])

    def test_live_signals_zero_size_closes(self):
        """Position in live_signals with size < 0.01 -> confirmed exit."""
        live_signals = [{
            "wallet": "0xabc", "condition_id": "cond1", "outcome": "Yes",
            "current_price": 0.65, "size": 0.005,
        }]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, live_signals)
        trade = self._get_trade()
        self.assertNotEqual(trade["status"], "open")
        self.assertEqual(trade["exit_reason"], "source_exited")

    @patch("paper_trader.requests.get")
    @patch("paper_trader.PaperTrader._source_has_recent_activity", return_value=False)
    def test_stale_timeout_still_works_when_disqualified(self, mock_activity, mock_get):
        """Disqualified trade past STALE_DAYS should still be closed by stale logic."""
        self.conn.execute(
            "UPDATE paper_trades SET opened_at = '2026-01-01 00:00:00' WHERE trade_id = 1"
        )
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE rowid = 100")
        self.conn.commit()
        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.55}],
            "closed": False, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.55}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]
        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertEqual(trade["status"], "stale")

    @patch("paper_trader.requests.get")
    @patch("paper_trader.PaperTrader._source_has_recent_activity", return_value=False)
    def test_future_dated_open_market_does_not_stale_close(self, mock_activity, mock_get):
        """Event markets with a future end date should stay open even without new source activity."""
        self.conn.execute(
            "UPDATE paper_trades SET opened_at = '2026-01-01 00:00:00' WHERE trade_id = 1"
        )
        self.conn.execute(
            "UPDATE position_signals SET is_open = 0, end_date_iso = '2099-12-31T00:00:00Z' WHERE rowid = 100"
        )
        self.conn.execute(
            """
            INSERT INTO market_metadata (condition_id, title, slug, event_slug, end_date_iso, is_open)
            VALUES ('cond1', 'Future Event', 'future-event', 'future-event', '2099-12-31T00:00:00Z', 1)
            """
        )
        self.conn.commit()

        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": 0.55}],
            "closed": False, "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = [
            {"market": "cond1", "size": 500.0, "curPrice": 0.55}
        ]
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        pos = self._get_trade()
        self.trader._check_position_exit(pos, [])
        trade = self._get_trade()
        self.assertEqual(trade["status"], "open")
        self.assertAlmostEqual(float(trade["current_price"]), 0.55)
        self.assertIsNotNone(trade["signal_disqualified_at"])


class TestCheckSellSignals(unittest.TestCase):
    """Test check_sell_signals() partial vs full exit."""

    def setUp(self):
        import paper_trader as pt_mod
        self.trader = pt_mod.PaperTrader.__new__(pt_mod.PaperTrader)
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.trader.conn = self.conn
        self.trader._wallet_positions_cache = {}
        self.trader._sell_outcome_cache = {}

        # Create paper_sell_signals table
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_sell_signals (
                wallet TEXT, condition_id TEXT, asset_id TEXT,
                price REAL, size REAL, timestamp TEXT,
                processed INTEGER DEFAULT 0
            );
        """)

        # Seed open paper trade
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status, opened_at, market_title) "
            "VALUES (1, '0xabc', 'cond1', 'Yes', 'buy', 0.50, 25.0, 'open', '2026-03-01', 'Test Market')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @patch("paper_trader.requests.get")
    def test_partial_sell_does_not_close(self, mock_get):
        """Sell signal where source still holds significant position -> trade stays open."""
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.55, 10.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        # Data-api: source still holds 490 shares
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {"market": "cond1", "size": 490.0, "curPrice": 0.55}
        ]
        mock_get.return_value = mock_resp

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertEqual(trade[0], "open")

        # Sell signal should still be marked processed
        sell = self.conn.execute("SELECT processed FROM paper_sell_signals").fetchone()
        self.assertEqual(sell[0], 1)

    @patch("paper_trader.requests.get")
    def test_multiple_sells_processed_individually(self, mock_get):
        """Multiple sell signals processed per-rowid, not bulk by (wallet, condition_id)."""
        # Two sell signals for the same wallet/condition, different markets
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.55, 10.0, '2026-03-05 10:00', 0)"
        )
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond2', 'asset2', 0.60, 50.0, '2026-03-05 10:05', 0)"
        )
        self.conn.commit()

        # Source still holds cond1 (partial sell)
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [{"market": "cond1", "size": 490.0, "curPrice": 0.55}]
        mock_get.return_value = mock_resp

        self.trader.check_sell_signals()

        # cond1 sell should be processed (partial sell, trade stays open)
        sells = self.conn.execute(
            "SELECT condition_id, processed FROM paper_sell_signals ORDER BY rowid"
        ).fetchall()
        self.assertEqual(sells[0][1], 1)  # cond1: processed
        self.assertEqual(sells[1][1], 1)  # cond2: processed (no matching open trade)

        # Trade for cond1 should still be open (partial sell)
        trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertEqual(trade[0], "open")

    @patch("paper_trader.requests.get")
    def test_sell_signal_uses_asset_id_to_match_correct_outcome(self, mock_get):
        self.conn.execute(
            "INSERT INTO paper_trades (trade_id, wallet, condition_id, outcome, direction, "
            "entry_price, size_usdc, status, opened_at, market_title) "
            "VALUES (2, '0xabc', 'cond1', 'No', 'sell', 0.50, 25.0, 'open', '2026-03-01', 'Alt Outcome')"
        )
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset-no', 0.60, 500.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        mock_clob_resp = MagicMock()
        mock_clob_resp.ok = True
        mock_clob_resp.json.return_value = {
            "tokens": [
                {"token_id": "asset-yes", "outcome": "Yes", "price": 0.40},
                {"token_id": "asset-no", "outcome": "No", "price": 0.60},
            ],
            "closed": False,
            "archived": False,
        }
        mock_data_resp = MagicMock()
        mock_data_resp.ok = True
        mock_data_resp.json.return_value = []
        mock_get.side_effect = [mock_clob_resp, mock_data_resp]

        self.trader.check_sell_signals()

        yes_trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        no_trade = self.conn.execute("SELECT status, exit_reason FROM paper_trades WHERE trade_id = 2").fetchone()
        self.assertEqual(yes_trade[0], "open")
        self.assertNotEqual(no_trade[0], "open")
        self.assertEqual(no_trade[1], "source_exited")

    @patch("paper_trader.requests.get")
    def test_full_sell_closes_trade(self, mock_get):
        """Sell signal where source has fully exited -> trade closes."""
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.60, 500.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        # Data-api: source position is gone
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = []  # No positions found
        mock_get.return_value = mock_resp

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status, exit_reason FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertNotEqual(trade[0], "open")
        self.assertEqual(trade[1], "source_exited")

    @patch("paper_trader.requests.get")
    def test_api_failure_does_not_close(self, mock_get):
        """If data-api fails, don't close — err on side of keeping open."""
        self.conn.execute(
            "INSERT INTO paper_sell_signals (wallet, condition_id, asset_id, price, size, timestamp, processed) "
            "VALUES ('0xabc', 'cond1', 'asset1', 0.55, 10.0, '2026-03-05', 0)"
        )
        self.conn.commit()

        mock_get.side_effect = Exception("network error")

        self.trader.check_sell_signals()

        trade = self.conn.execute("SELECT status FROM paper_trades WHERE trade_id = 1").fetchone()
        self.assertEqual(trade[0], "open")  # Kept open on API failure

        # Sell signal should still be marked processed
        sell = self.conn.execute("SELECT processed FROM paper_sell_signals").fetchone()
        self.assertEqual(sell[0], 1)


class TestRepairFalseSourceExitsScript(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = get_connection(self.db_path)
        init_db(self.conn)
        _create_paper_trades_table(self.conn)
        from db import ensure_paper_trades_disqualification_columns
        ensure_paper_trades_disqualification_columns(self.conn)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_sell_signals (
                wallet TEXT, condition_id TEXT, asset_id TEXT,
                price REAL, size REAL, timestamp TEXT,
                processed INTEGER DEFAULT 0
            )"""
        )
        self.conn.execute(
            """INSERT INTO paper_trades
               (trade_id, wallet, condition_id, outcome, direction, entry_price, size_usdc,
                status, exit_reason, exit_classification, opened_at, closed_at)
               VALUES (101, '0xabc', 'cond1', 'Yes', 'buy', 0.5, 25.0,
                       'early_exit', 'source_exited', 'early_exit', '2026-03-10 19:00:00', '2026-03-10 20:27:38')"""
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_apply_requires_explicit_trade_ids(self):
        from scripts import repair_false_source_exits as mod

        with patch("sys.argv", ["repair_false_source_exits.py", "--db", self.db_path, "--apply"]):
            with self.assertRaises(SystemExit) as cm:
                mod.main()
        self.assertEqual(cm.exception.code, 2)

    @patch("scripts.repair_false_source_exits.requests.get")
    def test_trade_ids_filter_limits_dry_run_candidates(self, mock_get):
        from scripts import repair_false_source_exits as mod

        mock_positions = MagicMock()
        mock_positions.ok = True
        mock_positions.json.return_value = [{"market": "cond1", "size": 10.0}]
        mock_market = MagicMock()
        mock_market.ok = True
        mock_market.json.return_value = {"closed": False, "archived": False}
        mock_get.side_effect = [mock_positions, mock_market]

        out = io.StringIO()
        with patch("sys.argv", ["repair_false_source_exits.py", "--db", self.db_path, "--trade-ids", "101"]):
            with redirect_stdout(out):
                mod.main()
        output = out.getvalue()
        self.assertIn("Trade IDs to reopen: [101]", output)

    @patch("scripts.repair_false_source_exits.requests.get")
    def test_dry_run_dedupes_same_wallet_market_outcome_to_newest_trade(self, mock_get):
        from scripts import repair_false_source_exits as mod

        self.conn.execute(
            """INSERT INTO paper_trades
               (trade_id, wallet, condition_id, outcome, direction, entry_price, size_usdc,
                status, exit_reason, exit_classification, opened_at, closed_at)
               VALUES (102, '0xabc', 'cond1', 'YES', 'buy', 0.5, 25.0,
                       'early_exit', 'source_exited', 'early_exit', '2026-03-10 19:30:00', '2026-03-10 20:27:39')"""
        )
        self.conn.commit()

        mock_positions = MagicMock()
        mock_positions.ok = True
        mock_positions.json.return_value = [{"market": "cond1", "size": 10.0}]
        mock_market = MagicMock()
        mock_market.ok = True
        mock_market.json.return_value = {"closed": False, "archived": False}
        mock_get.side_effect = [mock_positions, mock_market]

        out = io.StringIO()
        with patch("sys.argv", ["repair_false_source_exits.py", "--db", self.db_path]):
            with redirect_stdout(out):
                mod.main()
        output = out.getvalue()
        self.assertIn("Deduped to 1 newest candidate(s)", output)
        self.assertIn("Trade IDs to reopen: [102]", output)
        self.assertIn("#101: older duplicate candidate", output)

    @patch("scripts.repair_false_source_exits.requests.get")
    def test_apply_relabels_false_source_exit_when_newer_open_trade_exists(self, mock_get):
        from scripts import repair_false_source_exits as mod

        self.conn.execute(
            """INSERT INTO paper_trades
               (trade_id, wallet, condition_id, outcome, direction, entry_price, size_usdc,
                status, opened_at)
               VALUES (202, '0xabc', 'cond1', 'YES', 'buy', 0.55, 30.0, 'open', '2026-03-10 21:00:00')"""
        )
        self.conn.commit()

        mock_positions = MagicMock()
        mock_positions.ok = True
        mock_positions.json.return_value = [{"market": "cond1", "size": 10.0}]
        mock_market = MagicMock()
        mock_market.ok = True
        mock_market.json.return_value = {"closed": False, "archived": False}
        mock_get.side_effect = [mock_positions, mock_market]

        out = io.StringIO()
        with patch("sys.argv", ["repair_false_source_exits.py", "--db", self.db_path, "--trade-ids", "101", "--apply"]):
            with redirect_stdout(out):
                mod.main()

        trade = self.conn.execute(
            "SELECT exit_reason FROM paper_trades WHERE trade_id = 101"
        ).fetchone()
        self.assertEqual(trade["exit_reason"], "superseded by newer open trade #202")
        output = out.getvalue()
        self.assertIn("RELABEL — superseded by open trade #202", output)
        self.assertIn("Trade IDs relabeled: [101]", output)
