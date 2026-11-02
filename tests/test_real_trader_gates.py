"""
Deterministic tests for real_trader.py entry gates and sizing.

Validates that real trader mirrors paper trader logic:
- Signal quality gates (insider, speculator, price, qualifying_roi)
- Archetype exclusion (arb_fund, speculator_bot thresholds)
- Multiplicative sizing (upside, qROI, insider, win_rate factors)
- Circuit breaker passthrough
"""

import os
import sys
import sqlite3
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch out heavy imports before importing real_trader
with patch.dict(os.environ, {
    'POLY_CLOB_SIGNATURE_TYPE': '0',
    'DB_PATH': ':memory:',
}):
    from sniper.services import real_trader


def _make_trader():
    """Create a RealTrader with mocked DB and CLOB connections."""
    from datetime import datetime
    with patch.object(real_trader.RealTrader, '__init__', lambda self, dry_run=True: None):
        trader = real_trader.RealTrader.__new__(real_trader.RealTrader)
        trader.dry_run = True
        trader.clob = None
        trader.daily_pnl = 0.0
        trader.last_reset = datetime.now().date()
        trader.worker_id = 'test:0'
        trader.conn = sqlite3.connect(':memory:')
        trader.conn.row_factory = sqlite3.Row
        # Create minimal real_trades table for circuit breaker checks
        trader.conn.execute('''
            CREATE TABLE real_trades (
                trade_id INTEGER PRIMARY KEY,
                status TEXT, size_usd REAL, pnl_usd REAL,
                opened_at TIMESTAMP, closed_at TIMESTAMP, dry_run INTEGER
            )
        ''')
        return trader


def _good_signal():
    """A signal that should pass all gates."""
    return {
        'wallet': '0xabc123',
        'condition_id': 'cond_1',
        'outcome': 'Yes',
        'current_price': 0.45,
        'entry_price': 0.44,
        'position_insider_score': 72,
        'position_speculator_score': 25,
        'end_date_iso': '2026-04-01T00:00:00Z',
        'entry_anchor_ts': 0,
    }


def _good_wallet():
    """Wallet data that should pass all gates."""
    return {
        'wallet': '0xabc123',
        'win_rate': 0.75,
        'trades': 50,
        'pnl': 500,
        'qualifying_roi': 25.0,
        'insider_score': 70,
        'speculator_score': 20,
        'classification': 'insider',
    }


class TestEntryGates(unittest.TestCase):
    """Test that evaluate_signal mirrors paper_trader.py gates."""

    def setUp(self):
        self.trader = _make_trader()

    def tearDown(self):
        self.trader.conn.close()

    def test_good_signal_passes(self):
        ok, reason, meta = self.trader.evaluate_signal(_good_signal(), _good_wallet())
        self.assertTrue(ok, f"Good signal should pass, got: {reason}")

    def test_reject_high_price(self):
        sig = _good_signal()
        sig['current_price'] = 0.75
        ok, reason, _ = self.trader.evaluate_signal(sig, _good_wallet())
        self.assertFalse(ok)
        self.assertIn('0.7', reason)

    def test_reject_low_insider_score(self):
        sig = _good_signal()
        sig['position_insider_score'] = 40
        ok, reason, _ = self.trader.evaluate_signal(sig, _good_wallet())
        self.assertFalse(ok)
        self.assertIn('insider_score', reason)

    def test_reject_high_speculator_score(self):
        sig = _good_signal()
        sig['position_speculator_score'] = 75
        ok, reason, _ = self.trader.evaluate_signal(sig, _good_wallet())
        self.assertFalse(ok)
        self.assertIn('speculator_score', reason)

    def test_reject_low_qualifying_roi(self):
        wallet = _good_wallet()
        wallet['qualifying_roi'] = 5.0
        ok, reason, _ = self.trader.evaluate_signal(_good_signal(), wallet)
        self.assertFalse(ok)
        self.assertIn('qualifying_roi', reason)

    def test_null_qroi_with_high_insider_passes(self):
        """qualifying_roi=None allowed if insider_score >= 90."""
        sig = _good_signal()
        sig['position_insider_score'] = 92
        wallet = _good_wallet()
        wallet['qualifying_roi'] = None
        ok, reason, _ = self.trader.evaluate_signal(sig, wallet)
        self.assertTrue(ok, f"High insider + null qROI should pass, got: {reason}")

    def test_null_qroi_with_low_insider_rejects(self):
        wallet = _good_wallet()
        wallet['qualifying_roi'] = None
        ok, reason, _ = self.trader.evaluate_signal(_good_signal(), wallet)
        self.assertFalse(ok)
        self.assertIn('qualifying_roi=None', reason)

    def test_reject_arb_fund(self):
        wallet = _good_wallet()
        wallet['classification'] = 'arb_fund'
        ok, reason, _ = self.trader.evaluate_signal(_good_signal(), wallet)
        self.assertFalse(ok)
        self.assertIn('archetype', reason)

    def test_speculator_bot_high_wr_roi_passes(self):
        wallet = _good_wallet()
        wallet['classification'] = 'speculator_bot'
        wallet['win_rate'] = 0.90
        wallet['qualifying_roi'] = 20.0
        ok, reason, _ = self.trader.evaluate_signal(_good_signal(), wallet)
        self.assertTrue(ok, f"Strong speculator_bot should pass, got: {reason}")

    def test_speculator_bot_low_wr_rejects(self):
        wallet = _good_wallet()
        wallet['classification'] = 'speculator_bot'
        wallet['win_rate'] = 0.60
        wallet['qualifying_roi'] = 20.0
        ok, reason, _ = self.trader.evaluate_signal(_good_signal(), wallet)
        self.assertFalse(ok)
        self.assertIn('speculator_bot', reason)

    def test_reject_far_future_market(self):
        sig = _good_signal()
        sig['end_date_iso'] = '2027-01-01T00:00:00Z'
        ok, reason, _ = self.trader.evaluate_signal(sig, _good_wallet())
        self.assertFalse(ok)
        self.assertIn('Resolution too far', reason)

    def test_reject_high_adverse_drift(self):
        sig = _good_signal()
        sig['entry_price'] = 0.30
        sig['current_price'] = 0.45  # +50% adverse drift
        ok, reason, _ = self.trader.evaluate_signal(sig, _good_wallet())
        self.assertFalse(ok)
        self.assertIn('drift', reason.lower())

    def test_freshness_not_a_gate(self):
        """Stale signals should NOT be rejected (paper trader has no freshness gate)."""
        sig = _good_signal()
        sig['entry_anchor_ts'] = 1  # Very old timestamp
        ok, reason, meta = self.trader.evaluate_signal(sig, _good_wallet())
        # Should still pass — freshness is logged but not an entry gate
        self.assertTrue(ok, f"Stale signal should pass (no freshness gate), got: {reason}")


class TestSizing(unittest.TestCase):
    """Test that sizing mirrors paper trader's multiplicative logic."""

    def setUp(self):
        self.trader = _make_trader()

    def tearDown(self):
        self.trader.conn.close()

    def test_size_within_bounds(self):
        size = self.trader.calculate_position_size(_good_signal(), _good_wallet())
        self.assertGreaterEqual(size, real_trader.MIN_POSITION_SIZE)
        self.assertLessEqual(size, real_trader.MAX_POSITION_SIZE)

    def test_lower_price_sizes_up(self):
        sig_low = _good_signal()
        sig_low['current_price'] = 0.15
        sig_high = _good_signal()
        sig_high['current_price'] = 0.65
        size_low = self.trader.calculate_position_size(sig_low, _good_wallet())
        size_high = self.trader.calculate_position_size(sig_high, _good_wallet())
        self.assertGreater(size_low, size_high, "Lower price should size up")

    def test_higher_insider_sizes_up(self):
        sig_hi = _good_signal()
        sig_hi['position_insider_score'] = 95
        sig_lo = _good_signal()
        sig_lo['position_insider_score'] = 50
        size_hi = self.trader.calculate_position_size(sig_hi, _good_wallet())
        size_lo = self.trader.calculate_position_size(sig_lo, _good_wallet())
        self.assertGreater(size_hi, size_lo, "Higher insider score should size up")

    def test_higher_qroi_sizes_up(self):
        wallet_hi = _good_wallet()
        wallet_hi['qualifying_roi'] = 60.0
        wallet_lo = _good_wallet()
        wallet_lo['qualifying_roi'] = 12.0
        size_hi = self.trader.calculate_position_size(_good_signal(), wallet_hi)
        size_lo = self.trader.calculate_position_size(_good_signal(), wallet_lo)
        self.assertGreater(size_hi, size_lo, "Higher qualifying_roi should size up")

    def test_null_qroi_conservative(self):
        wallet_null = _good_wallet()
        wallet_null['qualifying_roi'] = None
        wallet_known = _good_wallet()
        wallet_known['qualifying_roi'] = 30.0
        size_null = self.trader.calculate_position_size(_good_signal(), wallet_null)
        size_known = self.trader.calculate_position_size(_good_signal(), wallet_known)
        self.assertLess(size_null, size_known, "Null qROI should be more conservative")


# ---------------------------------------------------------------------------
# Exit safety tests
# ---------------------------------------------------------------------------

def _make_exit_trader():
    """Create a RealTrader with real_trades + position_signals tables for exit tests."""
    from datetime import datetime
    with patch.object(real_trader.RealTrader, '__init__', lambda self, dry_run=True: None):
        trader = real_trader.RealTrader.__new__(real_trader.RealTrader)
        trader.dry_run = True
        trader.clob = None
        trader.daily_pnl = 0.0
        trader.last_reset = datetime.now().date()
        trader.worker_id = 'test:0'
        trader.conn = sqlite3.connect(':memory:')
        trader.conn.row_factory = sqlite3.Row
        trader.conn.execute('''
            CREATE TABLE real_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id INTEGER,
                wallet TEXT,
                market_title TEXT,
                condition_id TEXT,
                outcome TEXT,
                direction TEXT,
                entry_price REAL,
                executed_price REAL,
                size_usd REAL,
                size_shares REAL,
                token_id TEXT,
                status TEXT DEFAULT 'open',
                opened_at TIMESTAMP,
                closed_at TIMESTAMP,
                exit_price REAL,
                pnl_usd REAL,
                notes TEXT,
                tx_hash TEXT,
                order_id TEXT,
                dry_run INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        trader.conn.execute('''
            CREATE TABLE position_signals (
                wallet TEXT,
                condition_id TEXT,
                outcome TEXT,
                is_open INTEGER,
                current_price REAL,
                current_size REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        return trader


def _insert_open_trade(trader, trade_id=1, wallet='0xabc', condition_id='cond1',
                       outcome='YES', entry_price=0.40, size_usd=25.0,
                       notes=None, opened_at=None):
    """Insert a minimal open real_trade row."""
    import json
    if notes is None:
        notes = json.dumps({'source_size': 100.0})
    if opened_at is None:
        opened_at = 'datetime("now")'
        trader.conn.execute(f'''
            INSERT INTO real_trades
            (trade_id, wallet, condition_id, outcome, entry_price, executed_price,
             size_usd, size_shares, status, opened_at, notes, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', datetime('now'), ?, 1)
        ''', (trade_id, wallet, condition_id, outcome, entry_price, entry_price,
              size_usd, size_usd / entry_price, notes, ))
    else:
        trader.conn.execute('''
            INSERT INTO real_trades
            (trade_id, wallet, condition_id, outcome, entry_price, executed_price,
             size_usd, size_shares, status, opened_at, notes, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, 1)
        ''', (trade_id, wallet, condition_id, outcome, entry_price, entry_price,
              size_usd, size_usd / entry_price, opened_at, notes))
    trader.conn.commit()


def _insert_signal(trader, wallet='0xabc', condition_id='cond1', outcome='YES',
                   is_open=1, current_price=0.50, size=100.0):
    trader.conn.execute('''
        INSERT INTO position_signals (wallet, condition_id, outcome, is_open, current_price, current_size)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (wallet, condition_id, outcome, is_open, current_price, size))
    trader.conn.commit()


class TestExitSafety(unittest.TestCase):

    def _get_trade(self, trader, trade_id=1):
        return dict(trader.conn.execute(
            'SELECT * FROM real_trades WHERE trade_id = ?', (trade_id,)
        ).fetchone())

    # --- Source exit (existing behavior, now with CLOB price fallback) ---

    @patch.object(real_trader.RealTrader, '_fetch_live_price', return_value=None)
    def test_source_exit_closes_position(self, _mock_price):
        trader = _make_exit_trader()
        _insert_open_trade(trader)
        _insert_signal(trader, is_open=0, current_price=0.85)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'closed')
        self.assertIn('source_exited', trade['notes'])

    # --- Resolution auto-exit ---

    @patch.object(real_trader.RealTrader, '_fetch_live_price')
    def test_resolution_win_auto_exit(self, mock_price):
        mock_price.return_value = 0.98
        trader = _make_exit_trader()
        _insert_open_trade(trader)
        _insert_signal(trader, is_open=1, current_price=0.60)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'closed')
        self.assertIn('resolution_win', trade['notes'])
        self.assertGreater(trade['pnl_usd'], 0)

    @patch.object(real_trader.RealTrader, '_fetch_live_price')
    def test_resolution_loss_auto_exit(self, mock_price):
        mock_price.return_value = 0.02
        trader = _make_exit_trader()
        _insert_open_trade(trader)
        _insert_signal(trader, is_open=1, current_price=0.60)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'closed')
        self.assertIn('resolution_loss', trade['notes'])
        self.assertLess(trade['pnl_usd'], 0)

    @patch.object(real_trader.RealTrader, '_fetch_live_price')
    def test_no_exit_when_price_normal(self, mock_price):
        """Position stays open when price is between thresholds."""
        mock_price.return_value = 0.55
        trader = _make_exit_trader()
        _insert_open_trade(trader)
        _insert_signal(trader, is_open=1, current_price=0.55)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'open')

    # --- Stale position timeout ---

    @patch.object(real_trader.RealTrader, '_fetch_live_price', return_value=0.45)
    @patch.object(real_trader.RealTrader, '_source_has_recent_activity', return_value=False)
    def test_stale_position_auto_exit(self, _mock_activity, _mock_price):
        trader = _make_exit_trader()
        old_date = '2026-02-01T00:00:00+00:00'
        _insert_open_trade(trader, opened_at=old_date)
        _insert_signal(trader, is_open=1, current_price=0.45)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'closed')
        self.assertIn('stale', trade['notes'])

    @patch.object(real_trader.RealTrader, '_fetch_live_price', return_value=0.45)
    @patch.object(real_trader.RealTrader, '_source_has_recent_activity', return_value=True)
    def test_stale_position_kept_if_source_active(self, _mock_activity, _mock_price):
        """Old position stays open if source wallet is still active."""
        trader = _make_exit_trader()
        old_date = '2026-02-01T00:00:00+00:00'
        _insert_open_trade(trader, opened_at=old_date)
        _insert_signal(trader, is_open=1, current_price=0.45)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'open')

    # --- Source partial sell ---

    @patch.object(real_trader.RealTrader, '_fetch_live_price', return_value=0.55)
    def test_source_partial_sell_triggers_exit(self, _mock_price):
        """Source reducing position below 50% triggers exit."""
        import json
        trader = _make_exit_trader()
        _insert_open_trade(trader, notes=json.dumps({'source_size': 100.0}))
        # Source now holds only 30 (30% of original 100)
        _insert_signal(trader, is_open=1, current_price=0.55, size=30.0)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'closed')
        self.assertIn('source_partial_exit', trade['notes'])

    @patch.object(real_trader.RealTrader, '_fetch_live_price', return_value=0.55)
    def test_source_small_sell_no_exit(self, _mock_price):
        """Source reducing position by a small amount does NOT trigger exit."""
        import json
        trader = _make_exit_trader()
        _insert_open_trade(trader, notes=json.dumps({'source_size': 100.0}))
        # Source now holds 80 (80% of original) — above 50% threshold
        _insert_signal(trader, is_open=1, current_price=0.55, size=80.0)
        trader.check_exits()
        trade = self._get_trade(trader)
        self.assertEqual(trade['status'], 'open')

    # --- Parse source entry size ---

    def test_parse_source_entry_size_json(self):
        import json
        pos = {'notes': json.dumps({'source_size': 150.0, 'size': 25})}
        self.assertEqual(real_trader.RealTrader._parse_source_entry_size(pos), 150.0)

    def test_parse_source_entry_size_with_appended_exit(self):
        import json
        notes = json.dumps({'source_size': 200.0}) + ' | Exit: source_exited'
        pos = {'notes': notes}
        self.assertEqual(real_trader.RealTrader._parse_source_entry_size(pos), 200.0)

    def test_parse_source_entry_size_missing(self):
        pos = {'notes': 'some random text'}
        self.assertIsNone(real_trader.RealTrader._parse_source_entry_size(pos))

    def test_parse_source_entry_size_empty(self):
        pos = {'notes': ''}
        self.assertIsNone(real_trader.RealTrader._parse_source_entry_size(pos))


class TestRunLoopModes(unittest.TestCase):
    """Exercise loop modes without touching live exchange state."""

    def setUp(self):
        self.trader = _make_trader()

    def tearDown(self):
        self.trader.conn.close()

    def test_exits_only_skips_entry_evaluation(self):
        self.trader.check_exits = MagicMock()
        self.trader.reconcile_open_intents = MagicMock()
        self.trader._count_open_positions = MagicMock(return_value=1)
        self.trader._get_capital_deployed = MagicMock(return_value=10.0)
        self.trader._check_circuit_breakers = MagicMock(side_effect=AssertionError("circuit breakers should not run"))
        self.trader.get_proven_wallets = MagicMock(side_effect=AssertionError("entries should not run"))

        with patch('sniper.services.real_trader.time.sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.trader.run_loop(interval=1, exits_only=True)

        self.trader.check_exits.assert_called_once()
        self.trader.reconcile_open_intents.assert_called_once()
        self.trader._count_open_positions.assert_called_once()
        self.trader._get_capital_deployed.assert_called_once()


class TestDeploymentGate(unittest.TestCase):
    """Test the hard paper-trade deployment gate interlock."""

    def _make_conn_with_paper_trades(self, closed=50, wins=30, total_pnl=500.0, avg_roi=22.0):
        """Create in-memory DB with paper_trades matching gate criteria."""
        from db import init_db
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT, pnl_usdc REAL, roi_percent REAL,
                duplicate_of INTEGER
            )
        ''')
        # Insert winning trades
        for i in range(wins):
            pnl = total_pnl / max(wins, 1)
            roi = avg_roi + (i * 0.1)
            conn.execute(
                'INSERT INTO paper_trades (status, pnl_usdc, roi_percent) VALUES (?, ?, ?)',
                ('closed', pnl, roi),
            )
        # Insert losing trades
        losses = closed - wins
        for i in range(losses):
            conn.execute(
                'INSERT INTO paper_trades (status, pnl_usdc, roi_percent) VALUES (?, ?, ?)',
                ('closed', -2.0, -10.0),
            )
        conn.commit()
        return conn

    def test_gate_passes_when_all_conditions_met(self):
        from db import paper_trade_gate_status
        # Use 150 trades (matches PROOF_MIN_TRADES default). 95W/55L = 63.3% WR.
        conn = self._make_conn_with_paper_trades(closed=150, wins=95, total_pnl=1500.0, avg_roi=25.0)
        gate = paper_trade_gate_status(conn)
        self.assertTrue(gate['gate_ready'])
        self.assertEqual(gate['fail_reasons'], [])
        conn.close()

    def test_gate_fails_below_count(self):
        from db import paper_trade_gate_status
        # 30 trades is clearly below any reasonable PROOF_MIN_TRADES threshold (default 150)
        conn = self._make_conn_with_paper_trades(closed=30, wins=20, total_pnl=300.0, avg_roi=22.0)
        gate = paper_trade_gate_status(conn)
        self.assertFalse(gate['gate_ready'])
        self.assertTrue(any('trades' in r for r in gate['fail_reasons']))
        conn.close()

    def test_gate_fails_negative_pnl(self):
        from db import paper_trade_gate_status
        # 150 trades (meets count), 95W positive ROI but PnL sum is negative
        conn = self._make_conn_with_paper_trades(closed=150, wins=95, total_pnl=-50.0, avg_roi=15.0)
        gate = paper_trade_gate_status(conn)
        self.assertFalse(gate['gate_ready'])
        self.assertTrue(any('pnl' in r for r in gate['fail_reasons']))
        conn.close()

    def test_gate_fails_low_win_rate(self):
        from db import paper_trade_gate_status
        # 150 trades (meets count), 60W/90L = 40% WR — below 60% threshold
        conn = self._make_conn_with_paper_trades(closed=150, wins=60, total_pnl=200.0, avg_roi=15.0)
        gate = paper_trade_gate_status(conn)
        self.assertFalse(gate['gate_ready'])
        self.assertTrue(any('WR' in r for r in gate['fail_reasons']))
        conn.close()

    def test_gate_fails_low_avg_roi(self):
        from db import paper_trade_gate_status
        # 150 trades (meets count), 95W, avg_roi=5.0 is below 10% threshold
        conn = self._make_conn_with_paper_trades(closed=150, wins=95, total_pnl=200.0, avg_roi=5.0)
        gate = paper_trade_gate_status(conn)
        self.assertFalse(gate['gate_ready'])
        self.assertTrue(any('ROI' in r for r in gate['fail_reasons']))
        conn.close()

    def test_startup_hard_fail(self):
        """real_trader --real should sys.exit if gate not met."""
        trader = _make_trader()
        trader.dry_run = False
        # No paper_trades table = gate fails
        trader.conn.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY, status TEXT, pnl_usdc REAL, roi_percent REAL,
                duplicate_of INTEGER
            )
        ''')
        with self.assertRaises(SystemExit):
            trader._enforce_paper_gate(at_startup=True)
        trader.conn.close()

    def test_per_cycle_pause_not_exit(self):
        """Per-cycle gate check should pause entries, not kill process."""
        trader = _make_trader()
        trader.dry_run = False
        trader.conn.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY, status TEXT, pnl_usdc REAL, roi_percent REAL,
                duplicate_of INTEGER
            )
        ''')
        ok, reason = trader._enforce_paper_gate(at_startup=False)
        self.assertFalse(ok)
        self.assertIn('gate NOT MET', reason)
        trader.conn.close()


if __name__ == '__main__':
    unittest.main()
