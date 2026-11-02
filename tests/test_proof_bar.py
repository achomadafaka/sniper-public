"""Tests for proof_bar_status() and related helpers."""
import unittest
from datetime import datetime, timezone, timedelta

from db import get_connection, init_db, proof_bar_status


def _seed_paper_trades(conn, trades):
    """Seed paper_trades with list of trade tuples.

    Each tuple: (wallet, condition_id, outcome, direction, entry_price,
                 exit_price, size_usdc, pnl_usdc, roi_percent,
                 signal_insider_score, status, opened_at, closed_at)
    """
    conn.executemany(
        """INSERT INTO paper_trades
           (wallet, condition_id, outcome, direction, entry_price, exit_price,
            size_usdc, pnl_usdc, roi_percent, signal_insider_score,
            status, opened_at, closed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        trades,
    )
    conn.commit()


class TestProofBarStatus(unittest.TestCase):
    """Test proof_bar_status() computation."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        # paper_trades is not part of core init_db schema — create it here
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY,
                wallet TEXT,
                condition_id TEXT,
                outcome TEXT,
                direction TEXT,
                entry_price REAL,
                exit_price REAL,
                size_usdc REAL,
                pnl_usdc REAL,
                roi_percent REAL,
                signal_insider_score REAL,
                status TEXT,
                opened_at TEXT,
                closed_at TEXT,
                duplicate_of INTEGER
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_empty_db_returns_zero_state(self):
        """No paper_trades → all zeros, not_ready."""
        result = proof_bar_status(self.conn)
        self.assertEqual(result["overall"], "not_ready")
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 0)
        self.assertEqual(result["criteria"]["win_rate"]["value"], 0.0)
        self.assertEqual(result["criteria"]["slip_pnl"]["value"], 0.0)
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 0)

    def test_insider_score_filter(self):
        """Only trades with signal_insider_score >= 60 count."""
        trades = [
            # insider=80 → qualifies
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # insider=50 → excluded
            ("0xb", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 50,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)

    def test_status_filter_excludes_stale_and_source_exited(self):
        """Only resolution_win/loss, early_exit, manual count."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 0.5, 25.0, 5.0, 20.0, 80,
             "source_exited", "2026-03-01", "2026-03-05"),
            ("0xa", "c3", "yes", "buy", 0.40, 0.5, 25.0, 5.0, 20.0, 80,
             "stale", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)

    def test_win_rate_uses_entry_price_filter(self):
        """WR denominator only includes entry_price < 0.80 (ADR-002)."""
        trades = [
            # entry=0.40 < 0.80, win → counts in WR
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # entry=0.85 >= 0.80, win → excluded from WR
            ("0xa", "c2", "yes", "buy", 0.85, 1.0, 25.0, 4.0, 16.0, 80,
             "resolution_win", "2026-03-01", "2026-03-06"),
            # entry=0.50 < 0.80, loss → counts in WR
            ("0xa", "c3", "yes", "buy", 0.50, 0.0, 25.0, -25.0, -100.0, 80,
             "resolution_loss", "2026-03-01", "2026-03-07"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # 3 trades count toward trade count
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 3)
        # WR: 1 win / 2 qualifying = 50%
        self.assertAlmostEqual(result["criteria"]["win_rate"]["value"], 50.0, places=1)

    def test_slippage_calculation_one_sided(self):
        """Slip PnL = sum(roi/100 * size - size * 0.025) across all qualifying."""
        trades = [
            # roi=60%, size=25 → raw_pnl=15, slip=0.625, net=14.375
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # raw_pnl = 60/100 * 25 = 15.0; slippage = 25 * 0.025 = 0.625
        # slip_pnl = 15.0 - 0.625 = 14.375
        self.assertAlmostEqual(result["criteria"]["slip_pnl"]["value"], 14.375, places=2)

    def test_null_roi_excluded_from_pnl(self):
        """Trades with NULL roi_percent are excluded from P&L but counted in trades."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            # NULL roi_percent
            ("0xa", "c2", "yes", "buy", 0.40, None, 25.0, None, None, 80,
             "manual", "2026-03-01", "2026-03-06"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 2)
        # Only the first trade contributes to P&L
        self.assertAlmostEqual(result["criteria"]["slip_pnl"]["value"], 14.375, places=2)
        self.assertEqual(result["data_gaps"]["null_roi"], 1)

    def test_monthly_grouping_complete_vs_incomplete(self):
        """Complete months count toward criterion 4; in-progress months don't."""
        # Use a date far in the past so both months are "complete"
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-01-01", "2025-01-15"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-02-01", "2025-02-15"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        monthly = result["monthly"]
        self.assertEqual(len(monthly), 2)
        self.assertTrue(all(m["complete"] for m in monthly))
        # Both months profitable → criterion value = 2
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 2)

    def test_losing_month_blocks_criterion(self):
        """If ANY complete month is unprofitable, criterion 4 = 0 (fail)."""
        trades = [
            # 2025-01: profitable
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2025-01-01", "2025-01-15"),
            # 2025-02: unprofitable (loss > slippage-adjusted gain)
            ("0xa", "c2", "yes", "buy", 0.40, 0.0, 100.0, -100.0, -100.0, 80,
             "resolution_loss", "2025-02-01", "2025-02-15"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # 2 complete months but 1 is losing → value should be 0
        self.assertEqual(result["criteria"]["no_losing_month"]["value"], 0)
        self.assertEqual(result["criteria"]["no_losing_month"]["status"], "fail")

    def test_overall_ready_when_all_pass(self):
        """Overall = ready only when all 5 criteria pass."""
        # Seed 160 winning trades across 2 past months
        trades = []
        for i in range(160):
            month = "01" if i < 80 else "02"
            trades.append(
                ("0xa", f"c{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
                 "resolution_win", f"2025-{month}-01", f"2025-{month}-15")
            )
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["status"], "pass")
        self.assertEqual(result["criteria"]["win_rate"]["status"], "pass")
        self.assertEqual(result["criteria"]["slip_pnl"]["status"], "pass")
        self.assertEqual(result["criteria"]["no_losing_month"]["status"], "pass")
        self.assertEqual(result["overall"], "ready")

    def test_warn_threshold_win_rate(self):
        """WR within 2% of target → warn (not fail)."""
        # 59% WR: 59 wins, 41 losses (all qualifying entry_price < 0.80)
        trades = []
        for i in range(59):
            trades.append(("0xa", f"w{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
                           "resolution_win", "2026-03-01", "2026-03-05"))
        for i in range(41):
            trades.append(("0xa", f"l{i}", "yes", "buy", 0.40, 0.0, 25.0, -25.0, -100.0, 80,
                           "resolution_loss", "2026-03-01", "2026-03-05"))
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["win_rate"]["status"], "warn")

    def test_pace_calculations(self):
        """Pace section shows 7d rate and trades remaining."""
        now = datetime.now(timezone.utc)
        trades = [
            ("0xa", f"c{i}", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01",
             (now - timedelta(days=i % 7)).isoformat())
            for i in range(10)
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertIn("pace", result)
        self.assertEqual(result["pace"]["trades_to_go"], 140)
        self.assertGreater(result["pace"]["seven_day_rate"], 0)

    def test_entry_price_zero_excluded(self):
        """Trades with entry_price <= 0 are excluded."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.0, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        self.assertEqual(result["criteria"]["closed_trades"]["value"], 1)
        self.assertEqual(result["data_gaps"]["zero_entry_price"], 1)

    def test_chart_data_daily_granularity(self):
        """Chart data has one point per day."""
        trades = [
            ("0xa", "c1", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-05"),
            ("0xa", "c2", "yes", "buy", 0.40, 1.0, 25.0, 15.0, 60.0, 80,
             "resolution_win", "2026-03-01", "2026-03-07"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        cum_trades = result["charts"]["cumulative_trades"]
        self.assertGreater(len(cum_trades), 0)
        # Each point has date and count keys
        self.assertIn("date", cum_trades[0])
        self.assertIn("count", cum_trades[0])

    def test_win_is_pnl_positive(self):
        """A 'win' is defined as pnl_usdc > 0."""
        trades = [
            # pnl > 0 → win
            ("0xa", "c1", "yes", "buy", 0.40, 0.60, 25.0, 5.0, 20.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
            # pnl < 0 → loss
            ("0xa", "c2", "yes", "buy", 0.40, 0.30, 25.0, -2.5, -10.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
            # pnl = 0 → not a win
            ("0xa", "c3", "yes", "buy", 0.40, 0.40, 25.0, 0.0, 0.0, 80,
             "early_exit", "2026-03-01", "2026-03-05"),
        ]
        _seed_paper_trades(self.conn, trades)
        result = proof_bar_status(self.conn)
        # WR: 1 win / 3 qualifying (all entry < 0.80) = 33.3%
        self.assertAlmostEqual(result["criteria"]["win_rate"]["value"], 33.3, places=0)


class TestProofBarAPI(unittest.TestCase):
    """Test /api/proof-bar endpoint returns valid data."""

    def test_endpoint_returns_criteria(self):
        """Smoke test: endpoint returns dict with required keys."""
        conn = get_connection(":memory:")
        init_db(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY,
                wallet TEXT, condition_id TEXT, outcome TEXT, direction TEXT,
                entry_price REAL, exit_price REAL, size_usdc REAL,
                pnl_usdc REAL, roi_percent REAL, signal_insider_score REAL,
                status TEXT, opened_at TEXT, closed_at TEXT,
                duplicate_of INTEGER
            );
        """)
        result = proof_bar_status(conn)
        conn.close()
        self.assertIn("criteria", result)
        self.assertIn("overall", result)
        self.assertIn("pace", result)
        self.assertIn("monthly", result)
        self.assertIn("charts", result)
        self.assertIn("data_gaps", result)
        self.assertIn("closed_trades", result["criteria"])
        self.assertIn("win_rate", result["criteria"])
        self.assertIn("slip_pnl", result["criteria"])
        self.assertIn("no_losing_month", result["criteria"])
        self.assertIn("health", result["criteria"])


class TestProofBarMCPTool(unittest.TestCase):
    """Test get_proof_bar_status MCP tool."""

    def test_returns_envelope(self):
        from sniper.mcp_tools.read_tools import proof_bar_mcp
        conn = get_connection(":memory:")
        init_db(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY,
                wallet TEXT, condition_id TEXT, outcome TEXT, direction TEXT,
                entry_price REAL, exit_price REAL, size_usdc REAL,
                pnl_usdc REAL, roi_percent REAL, signal_insider_score REAL,
                status TEXT, opened_at TEXT, closed_at TEXT,
                duplicate_of INTEGER
            );
        """)
        result = proof_bar_mcp(conn=conn)
        conn.close()
        self.assertIn("data", result)
        self.assertIn("data_source", result)
        self.assertEqual(result["data_source"], "proof_bar")
        self.assertIn("criteria", result["data"])
