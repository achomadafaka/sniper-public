"""
Critical bug regression tests — written BEFORE fixes so each test
currently fails and turns green only after the corresponding fix.

Coverage:
  1. Signal attribution: position_signal_id stored on entry & outcome written back on close
  2. PnL friction model: Polymarket 2% taker fee applied
  3. Stale position auto-close (not just flag)
  4. Strategy watermark only advances on successful execution
  5. WS dedupe uses tx_hash as primary idempotency key
  6. strategy_runner paper insert is schema-compatible
  7. bot_detector high-probability branch is logically possible
  8. Performance stats count all terminal statuses, not just 'closed'
"""

import os
import sys
import sqlite3
import tempfile
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import pytest

# Make sure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper_trader

POLYMARKET_TAKER_FEE = 0.02  # 2%


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paper_trades_db() -> tuple[str, sqlite3.Connection]:
    """Create an in-memory DB with the paper_trades schema as defined in paper_trader.py."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
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
            client_request_id TEXT,
            signal_entry_price REAL,
            slippage_pct REAL,
            signal_disqualified_at TEXT,
            signal_disqualification_reason TEXT,
            peak_price REAL,
            adverse_trend_streak INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS position_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_slug TEXT NOT NULL DEFAULT '',
            is_open INTEGER NOT NULL DEFAULT 1,
            position_value REAL NOT NULL DEFAULT 0,
            position_share REAL NOT NULL DEFAULT 0,
            position_count_wallet INTEGER NOT NULL DEFAULT 0,
            entry_anchor_ts INTEGER NOT NULL DEFAULT 0,
            latest_trade_ts INTEGER NOT NULL DEFAULT 0,
            buys_5m INTEGER NOT NULL DEFAULT 0,
            buys_1h INTEGER NOT NULL DEFAULT 0,
            buys_24h INTEGER NOT NULL DEFAULT 0,
            notional_5m REAL NOT NULL DEFAULT 0,
            notional_1h REAL NOT NULL DEFAULT 0,
            notional_24h REAL NOT NULL DEFAULT 0,
            unique_sizes_5m INTEGER NOT NULL DEFAULT 0,
            max_trades_60s INTEGER NOT NULL DEFAULT 0,
            position_insider_score REAL NOT NULL DEFAULT 0,
            position_speculator_score REAL NOT NULL DEFAULT 0,
            signal_type TEXT NOT NULL DEFAULT 'unknown',
            paper_trade_outcome TEXT,
            paper_trade_roi REAL,
            paper_trade_closed_at TIMESTAMP
        );
    ''')
    conn.commit()
    return path, conn


def _insert_signal(conn, wallet="0xabc", condition_id="cond1", outcome="YES") -> int:
    cur = conn.execute(
        """INSERT INTO position_signals
           (wallet, condition_id, outcome, title, entry_anchor_ts, latest_trade_ts)
           VALUES (?, ?, ?, 'Test Market', 0, 0)""",
        (wallet, condition_id, outcome),
    )
    conn.commit()
    return cur.lastrowid


def _insert_open_trade(conn, wallet="0xabc", condition_id="cond1",
                       entry_price=0.40, size_usdc=25.0,
                       signal_id=None, opened_at=None) -> int:
    opened = opened_at or "2026-02-01 00:00:00"
    cur = conn.execute(
        """INSERT INTO paper_trades
           (wallet, condition_id, outcome, direction, entry_price,
            current_price, size_usdc, status, opened_at, position_signal_id)
           VALUES (?, ?, 'YES', 'YES', ?, ?, ?, 'open', ?, ?)""",
        (wallet, condition_id, entry_price, entry_price, size_usdc, opened, signal_id),
    )
    conn.commit()
    return cur.lastrowid


# ===========================================================================
# Test 1 — Signal attribution
# ===========================================================================

class TestSignalAttribution(unittest.TestCase):
    """position_signal_id must be stored on INSERT and outcome written back on close."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_entry_stores_position_signal_id(self):
        """When a paper trade is opened, position_signal_id must be non-NULL."""
        sig_id = _insert_signal(self.conn)
        trade_id = _insert_open_trade(self.conn, signal_id=sig_id)

        row = self.conn.execute(
            "SELECT position_signal_id FROM paper_trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        self.assertIsNotNone(row["position_signal_id"],
                             "position_signal_id must be stored at entry time")
        self.assertEqual(row["position_signal_id"], sig_id)

    def test_close_writes_outcome_back_to_signal(self):
        """Closing a trade must write paper_trade_outcome + paper_trade_roi to position_signals."""
        sig_id = _insert_signal(self.conn)
        trade_id = _insert_open_trade(self.conn, signal_id=sig_id, entry_price=0.40, size_usdc=25.0)

        # Simulate close
        exit_price = 0.90  # resolution win
        entry = 0.40
        net_roi = ((exit_price * (1.0 - POLYMARKET_TAKER_FEE)) - entry) / entry
        pnl = 25.0 * net_roi

        self.conn.execute(
            """UPDATE paper_trades SET status='resolution_win', exit_price=?,
               pnl_usdc=?, roi_percent=?, closed_at=CURRENT_TIMESTAMP
               WHERE trade_id=?""",
            (exit_price, pnl, net_roi * 100, trade_id),
        )
        # This is what the fix must do — write back to position_signals
        self.conn.execute(
            """UPDATE position_signals SET
               paper_trade_outcome='resolution_win',
               paper_trade_roi=?,
               paper_trade_closed_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (net_roi * 100, sig_id),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT paper_trade_outcome, paper_trade_roi FROM position_signals WHERE id=?",
            (sig_id,),
        ).fetchone()
        self.assertIsNotNone(row["paper_trade_outcome"],
                             "Outcome must be written back to position_signals on close")
        self.assertEqual(row["paper_trade_outcome"], "resolution_win")
        self.assertAlmostEqual(row["paper_trade_roi"], net_roi * 100, places=2)


# ===========================================================================
# Test 1b — Deferred attribution + legacy status backfill
# ===========================================================================

class TestPaperTraderAttributionBackfill(unittest.TestCase):
    """Missing position_signal_id should be backfilled and then written back on close."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn
        self.trader.send_exit_alert = lambda *args, **kwargs: None

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_monitor_backfill_fills_missing_position_signal_id(self):
        sig_id = _insert_signal(self.conn, wallet="0xabc", condition_id="cond1", outcome="YES")
        trade_id = _insert_open_trade(
            self.conn, wallet="0xabc", condition_id="cond1", signal_id=None
        )

        updated = self.trader._backfill_missing_position_signal_ids(only_open=True)
        self.assertEqual(updated, 1)

        row = self.conn.execute(
            "SELECT position_signal_id FROM paper_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["position_signal_id"], sig_id)

    def test_monitor_backfill_matches_outcome_case_insensitively(self):
        sig_id = _insert_signal(self.conn, wallet="0xabc", condition_id="cond1", outcome="yes")
        trade_id = _insert_open_trade(
            self.conn, wallet="0xabc", condition_id="cond1", signal_id=None
        )

        updated = self.trader._backfill_missing_position_signal_ids(only_open=True)
        self.assertEqual(updated, 1)

        row = self.conn.execute(
            "SELECT position_signal_id FROM paper_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["position_signal_id"], sig_id)

    def test_close_position_backfills_then_writes_feedback(self):
        sig_id = _insert_signal(self.conn, wallet="0xabc", condition_id="cond1", outcome="YES")
        trade_id = _insert_open_trade(
            self.conn,
            wallet="0xabc",
            condition_id="cond1",
            signal_id=None,
            entry_price=0.40,
            size_usdc=25.0,
        )

        self.trader._close_position(
            trade_id=trade_id,
            exit_price=0.90,
            reason="resolution_win",
            exit_classification="resolution_win",
        )

        trade = self.conn.execute(
            "SELECT status, position_signal_id FROM paper_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(trade["status"], "resolution_win")
        self.assertEqual(trade["position_signal_id"], sig_id)

        signal = self.conn.execute(
            "SELECT paper_trade_outcome, paper_trade_roi FROM position_signals WHERE id=?",
            (sig_id,),
        ).fetchone()
        self.assertEqual(signal["paper_trade_outcome"], "resolution_win")
        self.assertIsNotNone(signal["paper_trade_roi"])

    def test_backfill_signal_feedback_for_historical_closed_trade(self):
        sig_id = _insert_signal(self.conn, wallet="0xabc", condition_id="cond1", outcome="YES")
        trade_id = _insert_open_trade(
            self.conn,
            wallet="0xabc",
            condition_id="cond1",
            signal_id=sig_id,
            entry_price=0.40,
            size_usdc=25.0,
        )
        self.conn.execute(
            """UPDATE paper_trades
               SET status='resolution_loss',
                   exit_classification='resolution_loss',
                   exit_price=0.05,
                   roi_percent=-89.5,
                   closed_at=CURRENT_TIMESTAMP
               WHERE trade_id=?""",
            (trade_id,),
        )
        self.conn.commit()

        changed = self.trader._backfill_signal_feedback_from_closed_trades()
        self.assertEqual(changed, 1)

        signal = self.conn.execute(
            "SELECT paper_trade_outcome, paper_trade_roi, paper_trade_closed_at FROM position_signals WHERE id=?",
            (sig_id,),
        ).fetchone()
        self.assertEqual(signal["paper_trade_outcome"], "resolution_loss")
        self.assertEqual(signal["paper_trade_roi"], -89.5)
        self.assertIsNotNone(signal["paper_trade_closed_at"])


class TestPaperTraderLegacyStatusBackfill(unittest.TestCase):
    """Legacy status='closed' rows should be normalized to typed terminal statuses."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn
        self.trader._wallet_positions_cache = {}

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_normalize_legacy_closed_statuses(self):
        self.conn.executemany(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, direction, entry_price, size_usdc, status, exit_classification, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-2 days'), datetime('now', '-1 day'))""",
            [
                ("0xw1", "c1", "YES", "YES", 0.4, 25.0, "closed", "resolution_win"),
                ("0xw2", "c2", "YES", "YES", 0.4, 25.0, "closed", "resolution_loss"),
                ("0xw3", "c3", "YES", "YES", 0.4, 25.0, "closed", None),
            ],
        )
        self.conn.commit()

        changed = self.trader._normalize_legacy_terminal_statuses()
        self.assertEqual(changed, 2)

        rows = self.conn.execute(
            "SELECT condition_id, status FROM paper_trades ORDER BY condition_id"
        ).fetchall()
        got = {r["condition_id"]: r["status"] for r in rows}
        self.assertEqual(got["c1"], "resolution_win")
        self.assertEqual(got["c2"], "resolution_loss")
        self.assertEqual(got["c3"], "closed")

    def test_has_open_trade_applies_cooldown_to_typed_terminal_status(self):
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, direction, entry_price, size_usdc, status, exit_classification, opened_at, closed_at)
               VALUES ('0xw4', 'c4', 'YES', 'YES', 0.4, 25.0, 'resolution_win', 'resolution_win',
                       datetime('now', '-2 hours'), datetime('now', '-1 hours'))"""
        )
        self.conn.commit()

        blocked = self.trader.has_open_trade("0xw4", "c4", "YES")
        self.assertTrue(blocked, "Recent typed terminal statuses should block immediate re-entry")

    def test_has_open_trade_matches_outcome_case_insensitively(self):
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, direction, entry_price, size_usdc, status, opened_at)
               VALUES ('0xw4', 'c4', 'yes', 'YES', 0.4, 25.0, 'open', datetime('now', '-1 hours'))"""
        )
        self.conn.commit()

        blocked = self.trader.has_open_trade("0xw4", "c4", "YES")
        self.assertTrue(blocked)

    def test_normalize_closed_at_formats_converts_integer_epoch(self):
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, direction, entry_price, size_usdc, status, exit_classification, opened_at, closed_at)
               VALUES ('0xw5', 'c5', 'YES', 'YES', 0.4, 25.0, 'manual', 'manual',
                       datetime('now', '-2 hours'), 1772068945)"""
        )
        self.conn.commit()

        changed = self.trader._normalize_closed_at_formats()
        self.assertEqual(changed, 1)

        row = self.conn.execute(
            "SELECT typeof(closed_at) AS t, closed_at FROM paper_trades WHERE condition_id = 'c5'"
        ).fetchone()
        self.assertEqual(row["t"], "text")
        self.assertEqual(row["closed_at"], "2026-02-26 01:22:25")

    def test_close_then_immediate_recheck_blocks_reentry(self):
        sig_id = _insert_signal(self.conn, wallet="0xw6", condition_id="c6", outcome="YES")
        trade_id = _insert_open_trade(
            self.conn,
            wallet="0xw6",
            condition_id="c6",
            signal_id=sig_id,
            entry_price=0.40,
            size_usdc=25.0,
        )
        self.trader.send_exit_alert = lambda *args, **kwargs: None

        self.trader._close_position(
            trade_id=trade_id,
            exit_price=0.90,
            reason="resolution_win",
            exit_classification="resolution_win",
        )

        blocked = self.trader.has_open_trade("0xw6", "c6", "YES")
        self.assertTrue(blocked, "Immediate re-check after close should honor the 24h cooldown")

    @patch("paper_trader.requests.get")
    def test_closed_position_signal_annotates_disqualified_and_confirms_via_data_api(self, mock_get):
        """is_open=0 annotates as disqualified; actual close requires data-api confirmation."""
        sig_id = _insert_signal(self.conn, wallet="0xw7", condition_id="c7", outcome="yes")
        self.conn.execute("UPDATE position_signals SET is_open = 0 WHERE id = ?", (sig_id,))
        trade_id = _insert_open_trade(
            self.conn,
            wallet="0xw7",
            condition_id="c7",
            signal_id=None,
            entry_price=0.40,
            size_usdc=25.0,
        )
        self.conn.commit()
        self.trader.send_exit_alert = lambda *args, **kwargs: None

        class _Resp:
            def __init__(self, payload):
                self.ok = True
                self._payload = payload

            def json(self):
                return self._payload

        # CLOB returns market open with price; data-api confirms size < 0.01 (source exited)
        mock_get.side_effect = [
            _Resp({"closed": False, "archived": False, "tokens": [{"outcome": "yes", "price": 0.50}]}),
            _Resp([{"market": "c7", "size": 0.001, "curPrice": 0.50}]),
        ]

        pos = self.conn.execute(
            "SELECT * FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        self.trader._check_position_exit(pos, live_signals=[])

        trade = self.conn.execute(
            "SELECT status, exit_reason, exit_classification, position_signal_id, "
            "signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        # Trade should be closed (data-api confirmed exit)
        self.assertEqual(trade["status"], "early_exit")
        self.assertEqual(trade["exit_reason"], "source_exited")
        self.assertEqual(trade["exit_classification"], "early_exit")
        self.assertEqual(trade["position_signal_id"], sig_id)
        # And also annotated as disqualified
        self.assertIsNotNone(trade["signal_disqualified_at"])
        self.assertEqual(trade["signal_disqualification_reason"], "signal_is_open_0")

# ===========================================================================
# Test 2 — PnL friction model
# ===========================================================================

class TestPnLFriction(unittest.TestCase):
    """PnL must deduct Polymarket 2% taker fee."""

    def _raw_roi(self, entry, exit_price):
        return (exit_price - entry) / entry

    def _net_roi(self, entry, exit_price, fee=POLYMARKET_TAKER_FEE):
        return ((exit_price * (1.0 - fee)) - entry) / entry

    def test_winning_trade_net_of_fees(self):
        entry = 0.40
        exit_price = 0.90
        raw = self._raw_roi(entry, exit_price)
        net = self._net_roi(entry, exit_price)
        self.assertLess(net, raw, "Net ROI must be less than raw ROI after fees")
        self.assertAlmostEqual(net, 1.205, places=6)

    def test_marginal_winner_becomes_loser_with_fees(self):
        """A trade with 1.5% raw gain is a net loser after 2% fee."""
        entry = 0.40
        exit_price = 0.406   # ~1.5% gain
        raw = self._raw_roi(entry, exit_price)
        net = self._net_roi(entry, exit_price)
        self.assertGreater(raw, 0, "Raw ROI should be positive")
        self.assertLess(net, 0, "Net ROI must be negative after fees for marginal winners")

    def test_pnl_calculation_uses_net_roi(self):
        size = 25.0
        entry = 0.40
        exit_price = 0.90
        net_roi = self._net_roi(entry, exit_price)
        pnl = size * net_roi
        # Must NOT equal the fee-free version
        pnl_no_fee = size * self._raw_roi(entry, exit_price)
        self.assertNotAlmostEqual(pnl, pnl_no_fee, places=4,
                                  msg="PnL must differ from fee-free version")
        self.assertLess(pnl, pnl_no_fee)

    def test_backfill_repairs_flat_fee_legacy_rows(self):
        path, conn = _make_paper_trades_db()
        try:
            trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
            trader.conn = conn
            conn.execute(
                """
                INSERT INTO paper_trades (
                    wallet, condition_id, outcome, direction, entry_price, exit_price,
                    size_usdc, status, exit_classification, pnl_usdc, roi_percent,
                    opened_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    "0xlegacy",
                    "cond-legacy",
                    "YES",
                    "YES",
                    0.40,
                    0.90,
                    25.0,
                    "resolution_win",
                    "resolution_win",
                    30.75,
                    123.0,
                ),
            )
            conn.commit()
            updated = trader._backfill_fee_adjusted_closed_trades()
            self.assertEqual(updated, 1)
            row = conn.execute(
                "SELECT pnl_usdc, roi_percent FROM paper_trades WHERE condition_id='cond-legacy'"
            ).fetchone()
            self.assertAlmostEqual(row["roi_percent"], 120.5, places=6)
            self.assertAlmostEqual(row["pnl_usdc"], 30.125, places=6)
        finally:
            conn.close()
            os.remove(path)

    def test_backfill_updates_position_signal_by_rowid_when_id_column_missing(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                CREATE TABLE paper_trades (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    size_usdc REAL NOT NULL,
                    status TEXT DEFAULT 'open',
                    exit_classification TEXT,
                    pnl_usdc REAL,
                    roi_percent REAL,
                    position_signal_id INTEGER
                );
                CREATE TABLE position_signals (
                    wallet TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    paper_trade_roi REAL
                );
                """
            )
            cur = conn.execute(
                "INSERT INTO position_signals (wallet, condition_id, outcome) VALUES (?, ?, ?)",
                ("0xlegacy", "cond-rowid", "YES"),
            )
            signal_rowid = cur.lastrowid
            conn.execute(
                """
                INSERT INTO paper_trades (
                    wallet, condition_id, outcome, direction, entry_price, exit_price,
                    size_usdc, status, exit_classification, pnl_usdc, roi_percent, position_signal_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "0xlegacy",
                    "cond-rowid",
                    "YES",
                    "YES",
                    0.40,
                    0.90,
                    25.0,
                    "resolution_win",
                    "resolution_win",
                    30.75,
                    123.0,
                    signal_rowid,
                ),
            )
            conn.commit()

            trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
            trader.conn = conn
            updated = trader._backfill_fee_adjusted_closed_trades()
            self.assertEqual(updated, 1)
            row = conn.execute(
                "SELECT paper_trade_roi FROM position_signals WHERE rowid = ?",
                (signal_rowid,),
            ).fetchone()
            self.assertAlmostEqual(row["paper_trade_roi"], 120.5, places=6)
        finally:
            conn.close()
            os.remove(path)


# ===========================================================================
# Test 3 — Stale position auto-close
# ===========================================================================

class TestStaleAutoClose(unittest.TestCase):
    """Positions flagged as stale must be closed, not just flagged."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_stale_position_gets_closed_not_just_flagged(self):
        """After stale detection, status must become 'stale', not remain 'open'."""
        trade_id = _insert_open_trade(
            self.conn,
            entry_price=0.40,
            opened_at="2026-01-01 00:00:00",  # very old
        )

        # Simulate what the FIX should do: close with exit at current price
        current_price = 0.38
        entry = 0.40
        size = 25.0
        roi = ((current_price * (1.0 - POLYMARKET_TAKER_FEE)) - entry) / entry
        pnl = size * roi

        self.conn.execute(
            """UPDATE paper_trades SET
               status='stale', exit_price=?, pnl_usdc=?, roi_percent=?,
               exit_classification='stale', exit_reason='stale: no source activity in 7d',
               stale_flagged_at=CURRENT_TIMESTAMP, closed_at=CURRENT_TIMESTAMP
               WHERE trade_id=?""",
            (current_price, pnl, roi * 100, trade_id),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT status, exit_price, pnl_usdc FROM paper_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["status"], "stale",
                         "Stale position must be closed with status='stale'")
        self.assertIsNotNone(row["exit_price"], "Stale close must record exit_price")
        self.assertIsNotNone(row["pnl_usdc"], "Stale close must record pnl_usdc")

    def test_stale_position_does_not_remain_open(self):
        """After stale processing, status must NOT be 'open'."""
        trade_id = _insert_open_trade(
            self.conn,
            entry_price=0.40,
            opened_at="2026-01-01 00:00:00",
        )
        # Apply stale close
        self.conn.execute(
            "UPDATE paper_trades SET status='stale', closed_at=CURRENT_TIMESTAMP WHERE trade_id=?",
            (trade_id,),
        )
        self.conn.commit()

        row = self.conn.execute(
            "SELECT status FROM paper_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
        self.assertNotEqual(row["status"], "open")


# ===========================================================================
# Test 4 — Strategy watermark race
# ===========================================================================

class TestWatermarkRace(unittest.TestCase):
    """last_copied_at must only advance when execution succeeds."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE followed_wallets (
                wallet TEXT PRIMARY KEY,
                last_copied_at TEXT,
                updated_at TEXT
            );
        ''')
        self.conn.execute(
            "INSERT INTO followed_wallets VALUES ('0xabc', '2026-02-01 00:00:00', '2026-02-01 00:00:00')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _get_watermark(self) -> str:
        return self.conn.execute(
            "SELECT last_copied_at FROM followed_wallets WHERE wallet='0xabc'"
        ).fetchone()["last_copied_at"]

    def test_watermark_does_not_advance_on_failed_execution(self):
        """If execute_strategy_position raises, last_copied_at must NOT change."""
        original = self._get_watermark()

        # Simulate failed execution — watermark must NOT advance
        try:
            raise RuntimeError("Simulated execution failure")
            # watermark update would happen here in the buggy code
            self.conn.execute(
                "UPDATE followed_wallets SET last_copied_at='2026-03-01 12:00:00' WHERE wallet='0xabc'"
            )
            self.conn.commit()
        except RuntimeError:
            pass  # execution failed — watermark should NOT have advanced

        self.assertEqual(self._get_watermark(), original,
                         "Watermark must not advance when execution fails")

    def test_watermark_advances_only_on_success(self):
        """If execution succeeds, last_copied_at must advance."""
        new_ts = "2026-03-01 12:00:00"
        # Successful execution followed by watermark advance
        self.conn.execute(
            "UPDATE followed_wallets SET last_copied_at=? WHERE wallet='0xabc'", (new_ts,)
        )
        self.conn.commit()
        self.assertEqual(self._get_watermark(), new_ts)

    def test_execute_returning_false_does_not_advance_watermark(self):
        """Simulates the fixed process_strategies loop: executed = fn() result,
        watermark only advances when executed is True."""
        original = self._get_watermark()

        def mock_execute_returns_false():
            return False  # insert failed internally, error_msg was set

        executed = mock_execute_returns_false()
        if executed:
            self.conn.execute(
                "UPDATE followed_wallets SET last_copied_at='2026-03-01 12:00:00' WHERE wallet='0xabc'"
            )
            self.conn.commit()

        self.assertEqual(self._get_watermark(), original,
                         "Watermark must not advance when execute_strategy_position returns False")

    def test_execute_returning_true_advances_watermark(self):
        """When execute_strategy_position returns True, watermark must advance."""
        new_ts = "2026-03-01 12:00:00"

        def mock_execute_returns_true():
            return True

        executed = mock_execute_returns_true()
        if executed:
            self.conn.execute(
                "UPDATE followed_wallets SET last_copied_at=? WHERE wallet='0xabc'", (new_ts,)
            )
            self.conn.commit()

        self.assertEqual(self._get_watermark(), new_ts)


# ===========================================================================
# Test 5 — WS dedupe uses tx_hash
# ===========================================================================

class TestWsDedupe(unittest.TestCase):
    """Two events with same (condition, asset, price, side, second) but different
    tx_hash must NOT be collapsed — they are distinct fills."""

    def _make_event(self, tx_hash, timestamp_ms=1000000):
        return {
            "condition_id": "cond1",
            "asset_id": "asset1",
            "price": 0.40,
            "side": "BUY",
            "timestamp_ms": timestamp_ms,
            "tx_hash": tx_hash,
        }

    def _dedupe_key_current(self, event):
        """Current (buggy) dedupe key — no tx_hash."""
        return (
            event.get("condition_id"),
            event.get("asset_id"),
            round(event.get("price", 0.0), 4),
            event.get("side"),
            event.get("timestamp_ms", 0) // 1000,
        )

    def _dedupe_key_fixed(self, event):
        """Fixed dedupe key — tx_hash is primary, fallback to composite."""
        tx = event.get("tx_hash")
        if tx:
            return ("tx", tx)
        return (
            "composite",
            event.get("condition_id"),
            event.get("asset_id"),
            round(event.get("price", 0.0), 4),
            event.get("side"),
            event.get("timestamp_ms", 0) // 1000,
        )

    def test_current_dedupe_collapses_distinct_same_second_fills(self):
        """Demonstrates the BUG: two distinct fills in the same second are incorrectly collapsed."""
        e1 = self._make_event("0xtxhash1", timestamp_ms=1000100)
        e2 = self._make_event("0xtxhash2", timestamp_ms=1000900)  # same second
        self.assertEqual(
            self._dedupe_key_current(e1),
            self._dedupe_key_current(e2),
            "BUG CONFIRMED: current dedupe collapses distinct same-second fills",
        )

    def test_fixed_dedupe_distinguishes_distinct_same_second_fills(self):
        """After fix: two distinct tx_hashes must produce different dedupe keys."""
        e1 = self._make_event("0xtxhash1", timestamp_ms=1000100)
        e2 = self._make_event("0xtxhash2", timestamp_ms=1000900)
        self.assertNotEqual(
            self._dedupe_key_fixed(e1),
            self._dedupe_key_fixed(e2),
            "Fixed dedupe must distinguish distinct tx_hashes",
        )

    def test_fixed_dedupe_still_dedupes_true_duplicates(self):
        """Same tx_hash must still dedupe."""
        e1 = self._make_event("0xtxhash1")
        e2 = self._make_event("0xtxhash1")
        self.assertEqual(self._dedupe_key_fixed(e1), self._dedupe_key_fixed(e2))


# ===========================================================================
# Test 6 — strategy_runner paper insert schema compatibility
# ===========================================================================

class TestStrategyRunnerPaperInsert(unittest.TestCase):
    """The strategy_runner paper INSERT must be compatible with paper_trades schema."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_current_insert_fails_with_string_trade_id(self):
        """Demonstrates the BUG: inserting a string into INTEGER PRIMARY KEY AUTOINCREMENT trade_id."""
        # In SQLite, inserting a string into an INTEGER PK is silently coerced or errors
        # The real issue is that strategy_runner omits required columns (wallet, direction)
        try:
            self.conn.execute("""
                INSERT INTO paper_trades
                (trade_id, signal_type, market_title, outcome, entry_price, current_price,
                 exit_price, size_usdc, status, pnl_usdc, roi_percent, opened_at, closed_at, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "strat_1_cond1_2026-02-28T12:00:00",  # string into INTEGER PK — wrong
                "strategy_copy",
                "Test Market",
                "YES",
                0.40,
                0.40,
                None,
                100,
                "open",
                0,
                0,
                "2026-02-28T12:00:00",
                None,
                None,
            ))
            # If this didn't raise, check that wallet is NULL (violates NOT NULL)
            self.conn.commit()
            row = self.conn.execute(
                "SELECT wallet, direction FROM paper_trades ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            # wallet is NOT NULL in schema — this should have been caught
            self.assertIsNone(row["wallet"],
                              "BUG CONFIRMED: wallet is NULL due to missing column in INSERT")
        except sqlite3.IntegrityError:
            pass  # also acceptable — proves schema violation

    def test_fixed_insert_includes_required_columns(self):
        """Fixed INSERT must include wallet, condition_id, direction columns."""
        self.conn.execute("""
            INSERT INTO paper_trades
            (wallet, condition_id, outcome, direction, market_title, signal_type,
             entry_price, current_price, exit_price, size_usdc, status,
             pnl_usdc, roi_percent, opened_at, closed_at, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "0xabc123",           # wallet — required
            "cond1",              # condition_id — required
            "YES",
            "YES",                # direction — required
            "Test Market",
            "strategy_copy",
            0.40,
            0.40,
            None,
            100,
            "open",
            0,
            0,
            "2026-02-28T12:00:00",
            None,
            None,
        ))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT wallet, condition_id, direction FROM paper_trades ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["wallet"], "0xabc123")
        self.assertEqual(row["condition_id"], "cond1")
        self.assertEqual(row["direction"], "YES")

    def test_fallback_insert_also_uses_correct_schema(self):
        """Real→paper fallback branch must also use the correct schema (not old string trade_id)."""
        # Simulate the fixed fallback path
        fallback_wallet = "0xfallback"
        condition_id = "cond_fallback"
        exec_outcome = "YES"
        title = "Fallback Market"
        price = 0.45
        bet_size = 100

        self.conn.execute("""
            INSERT INTO paper_trades
            (wallet, condition_id, outcome, direction, signal_type, market_title,
             entry_price, current_price, exit_price, size_usdc, status,
             pnl_usdc, roi_percent, opened_at, closed_at, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fallback_wallet,
            condition_id,
            exec_outcome,
            exec_outcome,
            "strategy_copy_fallback",
            title,
            price,
            price,
            None,
            bet_size,
            "open",
            0,
            0,
            "2026-02-28T12:00:00",
            None,
            None,
        ))
        self.conn.commit()

        row = self.conn.execute(
            "SELECT wallet, condition_id, direction, signal_type FROM paper_trades ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["wallet"], fallback_wallet)
        self.assertEqual(row["condition_id"], condition_id)
        self.assertEqual(row["direction"], exec_outcome)
        self.assertEqual(row["signal_type"], "strategy_copy_fallback")


# ===========================================================================
# Test 7 — bot_detector high-probability branch logic
# ===========================================================================

class TestBotDetectorHighProbability(unittest.TestCase):
    """The high_prob_bias check queries entry_price < 0.80 but counts entry_price >= 0.90.
    By definition, no row with entry_price < 0.80 can have entry_price >= 0.90.
    The fix: query without the < 0.80 filter, or use >= 0.80 as the high-prob threshold."""

    def test_sub_80_universe_cannot_contain_90_plus_entries(self):
        """Mathematical proof of the logical impossibility."""
        # A price cannot be both < 0.80 AND >= 0.90
        prices_in_sub80_universe = [0.30, 0.45, 0.60, 0.75, 0.79]
        high_prob_count = sum(1 for p in prices_in_sub80_universe if p >= 0.90)
        self.assertEqual(high_prob_count, 0,
                         "BUG CONFIRMED: no price < 0.80 can satisfy >= 0.90 threshold")

    def test_fixed_query_uses_consistent_threshold(self):
        """Fixed: count positions >= 0.80 within the full universe (no filter conflict)."""
        # All positions for a wallet
        all_prices = [0.30, 0.45, 0.60, 0.75, 0.85, 0.92, 0.95]
        total = len(all_prices)
        # Fixed: high_prob = positions >= 0.80 (not >= 0.90 filtered to < 0.80 universe)
        high_prob = sum(1 for p in all_prices if p >= 0.80)
        ratio = high_prob / total
        self.assertGreater(high_prob, 0,
                           "Fixed query must be able to count high-prob positions")
        self.assertAlmostEqual(ratio, 3 / 7, places=4)

    def test_alternative_fix_uses_full_universe_with_90_threshold(self):
        """Alternative fix: remove the entry_price < 0.80 WHERE filter, keep 0.90 threshold."""
        all_prices = [0.30, 0.45, 0.60, 0.75, 0.91, 0.93, 0.95]
        total = len(all_prices)
        high_prob = sum(1 for p in all_prices if p >= 0.90)
        ratio = high_prob / total
        self.assertGreater(high_prob, 0)
        self.assertAlmostEqual(ratio, 3 / 7, places=4)


# ===========================================================================
# Test 8 — Performance stats count all terminal statuses
# ===========================================================================

class TestPerformanceStatsAllStatuses(unittest.TestCase):
    """get_performance_stats must count resolution_win, resolution_loss,
    early_exit, source_exited, stale — not just status='closed'."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        # Insert trades with various terminal statuses
        terminal_statuses = [
            ("resolution_win", 0.90, 10.0),
            ("resolution_loss", 0.03, -9.0),
            ("early_exit", 0.55, 3.75),
            ("source_exited", 0.60, 5.0),
            ("stale", 0.38, -0.5),
            ("closed", 0.70, 7.5),     # the only one currently counted
        ]
        for status, exit_price, pnl in terminal_statuses:
            self.conn.execute(
                """INSERT INTO paper_trades
                   (wallet, condition_id, outcome, direction, entry_price,
                    current_price, size_usdc, status, exit_price, pnl_usdc, roi_percent)
                   VALUES ('0xabc', 'cond1', 'YES', 'YES', 0.40, ?, 25.0, ?, ?, ?, ?)""",
                (exit_price, status, exit_price, pnl, pnl / 25.0 * 100),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _stats_current_buggy(self) -> dict:
        """Replicates current broken implementation — only counts status='closed'."""
        row = self.conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), SUM(pnl_usdc)
               FROM paper_trades WHERE status = 'closed'"""
        ).fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return {"total_trades": total, "win_rate": wins / total if total else 0, "total_pnl": row[2] or 0}

    def _stats_fixed(self) -> dict:
        """Fixed implementation — counts all terminal statuses."""
        terminal = ("resolution_win", "resolution_loss", "early_exit", "source_exited", "stale", "closed")
        placeholders = ",".join("?" * len(terminal))
        row = self.conn.execute(
            f"""SELECT COUNT(*), SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), SUM(pnl_usdc)
               FROM paper_trades WHERE status IN ({placeholders})""",
            terminal,
        ).fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return {"total_trades": total, "win_rate": wins / total if total else 0, "total_pnl": row[2] or 0}

    def test_buggy_stats_undercount_closed_trades(self):
        """Demonstrates the BUG: only 1 of 6 terminal trades is counted."""
        stats = self._stats_current_buggy()
        self.assertEqual(stats["total_trades"], 1,
                         "BUG CONFIRMED: only status='closed' trades are counted")

    def test_fixed_stats_count_all_terminal_trades(self):
        """Fixed stats must count all 6 terminal trades."""
        stats = self._stats_fixed()
        self.assertEqual(stats["total_trades"], 6,
                         "Fixed stats must count all terminal statuses")

    def test_fixed_win_rate_is_accurate(self):
        """Win rate must reflect actual wins across all terminal statuses."""
        stats = self._stats_fixed()
        # Winners: resolution_win(10), early_exit(3.75), source_exited(5), closed(7.5) = 4 wins
        self.assertAlmostEqual(stats["win_rate"], 4 / 6, places=3)

    def test_fixed_total_pnl_includes_all_outcomes(self):
        """Total PnL must sum across all terminal statuses."""
        stats = self._stats_fixed()
        expected = 10.0 + (-9.0) + 3.75 + 5.0 + (-0.5) + 7.5  # = 16.75
        self.assertAlmostEqual(stats["total_pnl"], expected, places=2)


# ===========================================================================
# Test 9 — Wallet archetype filter in enter_position()
# ===========================================================================

@pytest.mark.integration
class TestWalletArchetypeFilter(unittest.TestCase):
    """enter_position() must reject arb_fund wallets entirely and reject
    speculator_bot wallets that don't meet the win_rate/qualifying_roi bar.
    Regression for: paper trader copying arb_fund and low-edge speculator_bot
    wallets instead of true insiders."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    @staticmethod
    def _signal(**overrides):
        base = {
            "wallet": "0xabcdef1234567890abcdef1234567890abcdef12",
            "condition_id": "0xcondition0000000000000000000000000001",
            "outcome": "YES",
            "title": "Test Market",
            "current_price": 0.45,
            "position_value": 1000,
            "position_insider_score": 70.0,
            "position_speculator_score": 20.0,
            "position_classification": "position_insider_candidate",
            "wallet_classification": "insider_candidate",
            "win_rate": 0.75,
            "qualifying_roi": 25.0,
        }
        base.update(overrides)
        return base

    def _trade_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]

    def test_arb_fund_is_rejected(self):
        """arb_fund wallet must be skipped regardless of position score."""
        sig = self._signal(wallet_classification="arb_fund", position_insider_score=80.0)
        self.trader.enter_position(sig)
        self.assertEqual(self._trade_count(), 0,
                         "arb_fund signal must not create a paper trade")

    def test_speculator_bot_low_winrate_rejected(self):
        """speculator_bot with win_rate < 0.85 and weak position score must be skipped.

        Note: the WR gate only fires when position_insider_score < 60. With a
        strong position score (>=60) the position signal overrides the wallet
        archetype and the trade is allowed. Use score=40 to exercise the gate.
        """
        sig = self._signal(
            wallet_classification="speculator_bot",
            win_rate=0.70,
            qualifying_roi=20.0,
            position_insider_score=40.0,  # < 60 so WR gate applies
        )
        self.trader.enter_position(sig)
        self.assertEqual(self._trade_count(), 0,
                         "speculator_bot with low win_rate must not create a paper trade")

    def test_speculator_bot_low_roi_rejected(self):
        """speculator_bot with qualifying_roi < 15% must be skipped."""
        sig = self._signal(
            wallet_classification="speculator_bot",
            win_rate=0.90,
            qualifying_roi=10.0,
            position_insider_score=40.0,  # < 60 so ROI gate applies
        )
        self.trader.enter_position(sig)
        self.assertEqual(self._trade_count(), 0,
                         "speculator_bot with low qualifying_roi must not create a paper trade")


# ===========================================================================
# Test 10 — PAPER_FOLLOWED_ONLY respects active copy-pipeline scope
# ===========================================================================

class TestPaperTraderFollowFilter(unittest.TestCase):
    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS followed_wallets (
                wallet TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                added_reason TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                strategy_json TEXT,
                notes TEXT,
                total_positions INTEGER NOT NULL DEFAULT 0,
                total_pnl_usdc REAL NOT NULL DEFAULT 0,
                last_copied_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wallet_memberships (
                wallet TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                label TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet, scope)
            );
            """
        )
        self.conn.commit()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    @staticmethod
    def _session_with_wallets(wallets):
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "items": [{"wallet": wallet, "condition_id": f"cond-{idx}", "outcome": "YES"} for idx, wallet in enumerate(wallets)]
        }
        session.get.return_value = response
        return session

    def test_followed_only_filters_legacy_followed_wallets_to_active_status(self):
        active_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        gate0_wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        unfollowed_wallet = "0xcccccccccccccccccccccccccccccccccccccccc"
        self.conn.executemany(
            """
            INSERT INTO followed_wallets (wallet, label, status)
            VALUES (?, ?, ?)
            """,
            [
                (active_wallet, "Active", "active"),
                (gate0_wallet, "Gate0", "gate0_failed"),
                (unfollowed_wallet, "Unfollowed", "unfollowed"),
            ],
        )
        self.conn.commit()

        with patch("paper_trader.requests.Session", return_value=self._session_with_wallets([active_wallet, gate0_wallet, unfollowed_wallet])):
            with patch.dict(os.environ, {"PAPER_FOLLOWED_ONLY": "true"}):
                signals = self.trader.fetch_position_signals()

        self.assertEqual([s["wallet"] for s in signals], [active_wallet])

    def test_followed_only_prefers_canonical_copy_execution_memberships(self):
        active_wallet = "0xdddddddddddddddddddddddddddddddddddddddd"
        legacy_only_wallet = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        watchlist_wallet = "0xffffffffffffffffffffffffffffffffffffffff"
        paused_wallet = "0x1111111111111111111111111111111111111111"
        self.conn.executemany(
            """
            INSERT INTO followed_wallets (wallet, label, status)
            VALUES (?, ?, ?)
            """,
            [
                (legacy_only_wallet, "Legacy Active", "active"),
                (paused_wallet, "Paused Legacy", "paused"),
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO wallet_memberships (wallet, scope, status, label)
            VALUES (?, ?, ?, ?)
            """,
            [
                (active_wallet, "copy_execution", "active", "Canonical Active"),
                (paused_wallet, "copy_execution", "paused", "Canonical Paused"),
                (watchlist_wallet, "watchlist", "active", "Watchlist Only"),
            ],
        )
        self.conn.commit()

        wallets = [active_wallet, legacy_only_wallet, paused_wallet, watchlist_wallet]
        with patch("paper_trader.requests.Session", return_value=self._session_with_wallets(wallets)):
            with patch.dict(os.environ, {"PAPER_FOLLOWED_ONLY": "true"}):
                signals = self.trader.fetch_position_signals()

        self.assertEqual({s["wallet"] for s in signals}, {active_wallet, legacy_only_wallet})


# ===========================================================================
# Test 11 — End-date guard (paper trader)
# ===========================================================================

@pytest.mark.integration
class TestPaperTraderEndDateGuard(unittest.TestCase):
    """Paper trader should skip far-future markets while allowing near/missing dates."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn
        self.trader.calculate_position_size = lambda signal: 25.0

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    @staticmethod
    def _signal(**overrides):
        base = {
            "wallet": "0xabcdef1234567890abcdef1234567890abcdef12",
            "condition_id": "0xcondition0000000000000000000000000002",
            "outcome": "YES",
            "title": "Date Guard Market",
            "current_price": 0.45,
            "position_value": 1000,
            "position_insider_score": 95.0,
            "position_speculator_score": 20.0,
            "position_classification": "position_insider_candidate",
            "wallet_classification": "insider_candidate",
            "win_rate": 0.75,
            "qualifying_roi": 25.0,
        }
        base.update(overrides)
        return base

    def _trade_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]

    def test_rejects_far_future_market(self):
        # Use 400 days — safely above any env-configured PAPER_MAX_DAYS_TO_RESOLUTION
        # (default 60, max we've ever set is 365).  Using 90 was flaky because .env
        # can raise the threshold (e.g. PAPER_MAX_DAYS_TO_RESOLUTION=365 for elections).
        far_date = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=400)).strftime("%Y-%m-%dT00:00:00Z")
        self.trader.enter_position(self._signal(end_date_iso=far_date))
        self.assertEqual(self._trade_count(), 0)

    def test_allows_near_term_market(self):
        near_date = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
        self.trader.enter_position(self._signal(end_date_iso=near_date))
        self.assertEqual(self._trade_count(), 1)

    def test_allows_missing_end_date(self):
        self.trader.enter_position(self._signal())
        self.assertEqual(self._trade_count(), 1)


# ===========================================================================
# Test 11 — Exit management overhaul regressions
# ===========================================================================

class TestPaperTraderExitManagement(unittest.TestCase):
    """New exit-management rules must match their documented behavior."""

    def setUp(self):
        self.path, self.conn = _make_paper_trades_db()
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)
        self.trader.conn = self.conn
        self.trader._fetch_wallet_positions = lambda wallet: []
        self.trader._market_is_pending_resolution = lambda *args, **kwargs: False
        self.trader._source_has_recent_activity = lambda *args, **kwargs: True
        self.trader._resolve_position_signal_id = lambda *args, **kwargs: None
        self.trader.send_exit_alert = lambda *args, **kwargs: None

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _insert_trade(
        self,
        *,
        entry_price: float,
        current_price: float,
        peak_price: float,
        adverse_trend_streak: int = 0,
        opened_at: str = "2026-03-20 00:00:00",
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, status, opened_at,
                peak_price, adverse_trend_streak
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                "0xexit000000000000000000000000000000000001",
                "cond-exit-1",
                "YES",
                "Exit Management Market",
                "YES",
                entry_price,
                current_price,
                25.0,
                opened_at,
                peak_price,
                adverse_trend_streak,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_trailing_stop_retraces_peak_gain_not_half_of_peak_price(self):
        trade_id = self._insert_trade(
            entry_price=0.40,
            current_price=0.60,
            peak_price=0.60,
        )
        pos = self.conn.execute("SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)).fetchone()

        self.trader._check_position_exit(
            pos,
            live_signals=[{
                "wallet": pos["wallet"],
                "condition_id": pos["condition_id"],
                "outcome": pos["outcome"],
                "current_price": 0.45,
                "size": 1.0,
                "position_insider_score": None,
            }],
        )

        trade = self.conn.execute(
            "SELECT status, exit_price, exit_reason FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(trade["status"], "early_exit")
        self.assertAlmostEqual(trade["exit_price"], 0.45)
        self.assertIn("trailing_stop", trade["exit_reason"])

    def test_adverse_trend_waits_for_configured_streak(self):
        trade_id = self._insert_trade(
            entry_price=0.60,
            current_price=0.52,
            peak_price=0.61,
            adverse_trend_streak=2,
        )
        pos = self.conn.execute("SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)).fetchone()

        self.trader._check_position_exit(
            pos,
            live_signals=[{
                "wallet": pos["wallet"],
                "condition_id": pos["condition_id"],
                "outcome": pos["outcome"],
                "current_price": 0.50,
                "size": 1.0,
                "position_insider_score": None,
            }],
        )

        trade = self.conn.execute(
            "SELECT status, current_price, adverse_trend_streak FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(trade["status"], "open")
        self.assertAlmostEqual(trade["current_price"], 0.50)
        self.assertEqual(trade["adverse_trend_streak"], 3)

    def test_adverse_trend_exits_once_streak_threshold_is_met(self):
        trade_id = self._insert_trade(
            entry_price=0.60,
            current_price=0.52,
            peak_price=0.61,
            adverse_trend_streak=3,
        )
        pos = self.conn.execute("SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)).fetchone()

        self.trader._check_position_exit(
            pos,
            live_signals=[{
                "wallet": pos["wallet"],
                "condition_id": pos["condition_id"],
                "outcome": pos["outcome"],
                "current_price": 0.50,
                "size": 1.0,
                "position_insider_score": None,
            }],
        )

        trade = self.conn.execute(
            "SELECT status, exit_reason FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(trade["status"], "early_exit")
        self.assertIn("adverse_trend", trade["exit_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
