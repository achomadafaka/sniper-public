"""Tests for the retrospective signal-outcome analyzer.

Tests use an in-memory SQLite database seeded with minimal fixture data.
"""

import sqlite3

import pytest

from sniper.analysis.retrospective_analyzer import (
    AnalysisConfig,
    AnalysisResult,
    BIAS_WARNINGS,
    VALID_DATA_SOURCES,
    run_analysis,
    sweep_2d,
    sweep_insider_threshold,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_db(conn: sqlite3.Connection) -> None:
    """Create tables and seed minimal test data."""
    conn.executescript(
        """
        -- paper_trades (subset of columns used by analyzer)
        CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id INTEGER PRIMARY KEY,
            wallet TEXT,
            condition_id TEXT,
            outcome TEXT,
            market_title TEXT,
            entry_price REAL,
            exit_price REAL,
            size_usdc REAL,
            pnl_usdc REAL,
            roi_percent REAL,
            status TEXT,
            signal_insider_score REAL,
            signal_speculator_score REAL,
            wallet_win_rate REAL,
            signal_type TEXT,
            opened_at TEXT,
            closed_at TEXT,
            duplicate_of INTEGER
        );

        -- position_signals (subset)
        CREATE TABLE IF NOT EXISTS position_signals (
            wallet TEXT,
            condition_id TEXT,
            outcome TEXT,
            title TEXT,
            entry_price REAL,
            entry_notional REAL,
            position_insider_score REAL DEFAULT 0,
            position_speculator_score REAL DEFAULT 0,
            position_classification TEXT,
            analyzed_ts INTEGER,
            entry_anchor_ts INTEGER,
            is_open INTEGER DEFAULT 0,
            PRIMARY KEY (wallet, condition_id, outcome)
        );

        -- market_outcomes
        CREATE TABLE IF NOT EXISTS market_outcomes (
            condition_id TEXT,
            outcome TEXT,
            resolved_price REAL DEFAULT 0.0,
            resolved_at INTEGER DEFAULT 0,
            source TEXT DEFAULT 'test',
            inserted_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (condition_id, outcome)
        );

        -- followed_wallet_activity (subset)
        CREATE TABLE IF NOT EXISTS followed_wallet_activity (
            activity_key TEXT PRIMARY KEY,
            wallet TEXT,
            transaction_hash TEXT,
            condition_id TEXT,
            activity_type TEXT,
            side TEXT,
            outcome TEXT,
            title TEXT,
            slug TEXT,
            event_slug TEXT,
            price REAL,
            size REAL,
            usdc_size REAL,
            activity_ts INTEGER,
            source_endpoint TEXT,
            raw_json TEXT,
            inserted_at TEXT
        );

        -- user_analysis (subset for FWA join)
        CREATE TABLE IF NOT EXISTS user_analysis (
            wallet TEXT PRIMARY KEY,
            insider_score REAL,
            speculator_score REAL,
            classification TEXT,
            win_rate REAL,
            qualifying_roi REAL
        );
        """
    )

    # Seed paper trades: 5 closed trades with varying insider scores
    paper_trades = [
        (1, "0xaaa", "cond1", "yes", "Will X happen?", 0.40, 1.0, 25.0, 37.5, 150.0, "resolution_win", 90.0, 10.0, 0.7, "insider", "2026-01-01", "2026-01-10"),
        (2, "0xbbb", "cond2", "yes", "Will Y happen?", 0.50, 0.0, 25.0, -25.0, -100.0, "resolution_loss", 80.0, 20.0, 0.6, "insider", "2026-01-02", "2026-01-11"),
        (3, "0xccc", "cond3", "yes", "Will Z happen?", 0.30, 1.0, 25.0, 58.33, 233.3, "resolution_win", 70.0, 30.0, 0.65, "insider", "2026-01-03", "2026-01-12"),
        (4, "0xddd", "cond4", "no", "Will A happen?", 0.60, 1.0, 25.0, 16.67, 66.7, "resolution_win", 50.0, 40.0, 0.55, "insider", "2026-01-04", "2026-01-13"),
        (5, "0xeee", "cond5", "yes", "Will B happen?", 0.80, 0.0, 25.0, -25.0, -100.0, "resolution_loss", 40.0, 60.0, 0.5, "speculator", "2026-01-05", "2026-01-14"),
    ]
    conn.executemany(
        """INSERT INTO paper_trades
           (trade_id, wallet, condition_id, outcome, market_title, entry_price, exit_price,
            size_usdc, pnl_usdc, roi_percent, status, signal_insider_score,
            signal_speculator_score, wallet_win_rate, signal_type, opened_at, closed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        paper_trades,
    )

    # Seed market_outcomes for position_signals join
    outcomes = [
        ("condA", "yes", 1.0, 1700000000),
        ("condA", "no", 0.0, 1700000000),
        ("condB", "yes", 0.0, 1700100000),
        ("condC", "yes", 1.0, 1700200000),
    ]
    conn.executemany(
        "INSERT INTO market_outcomes (condition_id, outcome, resolved_price, resolved_at) VALUES (?,?,?,?)",
        outcomes,
    )

    # Seed position_signals that join to market_outcomes
    signals = [
        ("0xf01", "condA", "yes", "Market A?", 0.35, 500.0, 85.0, 15.0, "position_insider", 1700000000, 1699900000),
        ("0xf02", "condA", "yes", "Market A?", 0.45, 300.0, 60.0, 50.0, "position_insider", 1700000000, 1699900000),
        ("0xf03", "condB", "yes", "Market B?", 0.55, 200.0, 75.0, 25.0, "position_insider", 1700100000, 1700000000),
        ("0xf04", "condC", "yes", "Market C?", 0.20, 1000.0, 95.0, 5.0, "position_insider", 1700200000, 1700100000),
    ]
    conn.executemany(
        """INSERT INTO position_signals
           (wallet, condition_id, outcome, title, entry_price, entry_notional,
            position_insider_score, position_speculator_score, position_classification,
            analyzed_ts, entry_anchor_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        signals,
    )

    # Seed FWA trades + user_analysis
    fwa = [
        ("fwa1", "0xwallet1", None, "condA", "TRADE", "BUY", "yes", "Market A?", "", "", 0.30, 100.0, 30.0, 1699800000, "activity", "", ""),
        ("fwa2", "0xwallet1", None, "condB", "TRADE", "BUY", "yes", "Market B?", "", "", 0.50, 50.0, 25.0, 1699900000, "activity", "", ""),
    ]
    conn.executemany(
        """INSERT INTO followed_wallet_activity
           (activity_key, wallet, transaction_hash, condition_id, activity_type, side,
            outcome, title, slug, event_slug, price, size, usdc_size, activity_ts,
            source_endpoint, raw_json, inserted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        fwa,
    )
    conn.execute(
        "INSERT INTO user_analysis (wallet, insider_score, speculator_score, classification, win_rate, qualifying_roi) VALUES (?,?,?,?,?,?)",
        ("0xwallet1", 80.0, 15.0, "insider", 0.65, 25.0),
    )
    conn.commit()


@pytest.fixture
def test_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_db(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPaperTradesSource:
    def test_no_filters(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="paper_trades"), conn=test_conn)
        assert result.trade_count == 5
        assert result.bias_warning is None
        assert result.data_source == "paper_trades"

    def test_insider_gate(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="paper_trades", min_insider_score=70),
            conn=test_conn,
        )
        assert result.trade_count == 3  # trades 1,2,3 have insider 90,80,70
        assert result.pre_filter_count == 5

    def test_speculator_gate(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="paper_trades", max_speculator_score=30),
            conn=test_conn,
        )
        # spec scores: 10, 20 pass (< 30); 30, 40, 60 filtered (>= 30)
        assert result.trade_count == 2

    def test_entry_price_gate(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="paper_trades", max_entry_price=0.50),
            conn=test_conn,
        )
        # entry prices: 0.40, 0.50, 0.30 pass; 0.60, 0.80 filtered
        assert result.trade_count == 3

    def test_combined_gates(self, test_conn):
        result = run_analysis(
            AnalysisConfig(
                data_source="paper_trades",
                min_insider_score=70,
                max_entry_price=0.50,
            ),
            conn=test_conn,
        )
        # insider>=70: trades 1(90),2(80),3(70)
        # entry<=0.50: trades 1(0.40),2(0.50),3(0.30) — all 3 pass both
        assert result.trade_count == 3

    def test_pnl_computation(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="paper_trades"), conn=test_conn)
        total = sum(t["pnl"] for t in result.trades)
        assert abs(total - result.total_pnl) < 0.01

    def test_win_rate(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="paper_trades"), conn=test_conn)
        # 3 wins (trades 1,3,4), 2 losses (trades 2,5)
        assert result.win_rate == 60.0

    def test_equity_curve(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="paper_trades"), conn=test_conn)
        assert len(result.equity_curve) == 5
        assert result.equity_curve[0]["trade_num"] == 1
        assert result.equity_curve[-1]["cumulative_pnl"] == result.total_pnl

    def test_wallet_filter(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="paper_trades", wallet_filter={"0xaaa"}),
            conn=test_conn,
        )
        assert result.trade_count == 1
        assert result.trades[0]["wallet"] == "0xaaa"


class TestPositionSignalsSource:
    def test_no_filters(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="position_signals"), conn=test_conn)
        assert result.trade_count == 4
        assert result.bias_warning is not None
        assert "LATEST" in result.bias_warning

    def test_insider_gate(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="position_signals", min_insider_score=80),
            conn=test_conn,
        )
        # Signals: 85, 60, 75, 95 → 85 and 95 pass
        assert result.trade_count == 2

    def test_resolved_price_pnl(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="position_signals", min_insider_score=95),
            conn=test_conn,
        )
        assert result.trade_count == 1
        t = result.trades[0]
        # entry=0.20, resolved=1.0 → roi = (1.0-0.20)/0.20 * 100 = 400%
        assert t["roi_pct"] == 400.0
        assert t["won"] is True


class TestFWASource:
    def test_no_filters(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="fwa"), conn=test_conn)
        assert result.trade_count == 2
        assert result.bias_warning is not None

    def test_insider_gate(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="fwa", min_insider_score=90),
            conn=test_conn,
        )
        # wallet has insider=80, so nothing passes
        assert result.trade_count == 0


class TestSweep:
    def test_sweep_returns_rows(self, test_conn):
        results = sweep_insider_threshold(
            data_source="paper_trades",
            thresholds=[0, 50, 80],
            conn=test_conn,
        )
        assert len(results) == 3
        # At threshold 0, all 5 trades pass
        assert results[0]["trade_count"] == 5
        # At threshold 80, only 2 pass (insider 90, 80)
        assert results[2]["trade_count"] == 2


class TestEdgeCases:
    def test_invalid_source(self, test_conn):
        with pytest.raises(ValueError, match="Invalid data_source"):
            run_analysis(AnalysisConfig(data_source="invalid"), conn=test_conn)

    def test_empty_result(self, test_conn):
        result = run_analysis(
            AnalysisConfig(data_source="paper_trades", min_insider_score=100),
            conn=test_conn,
        )
        assert result.trade_count == 0
        assert result.win_rate is None
        assert result.avg_roi is None
        assert result.total_pnl == 0.0
        assert result.max_drawdown == 0.0

    def test_max_drawdown(self, test_conn):
        result = run_analysis(AnalysisConfig(data_source="paper_trades"), conn=test_conn)
        assert result.max_drawdown >= 0.0

    def test_gate_config_in_result(self, test_conn):
        cfg = AnalysisConfig(
            data_source="paper_trades",
            min_insider_score=55,
            max_entry_price=0.70,
        )
        result = run_analysis(cfg, conn=test_conn)
        assert result.gate_config["min_insider_score"] == 55
        assert result.gate_config["max_entry_price"] == 0.70
        assert result.gate_config["data_source"] == "paper_trades"


class TestSweep2D:
    def test_returns_grid_structure(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0, 50, 80],
            price_thresholds=[0.50, 0.70, 1.00],
            conn=test_conn,
        )
        assert "insider_axis" in result
        assert "price_axis" in result
        assert "grid" in result
        assert "best_cell" in result
        assert "data_source" in result
        assert result["data_source"] == "paper_trades"
        assert result["insider_axis"] == [0, 50, 80]
        assert result["price_axis"] == [0.50, 0.70, 1.00]

    def test_grid_size(self, test_conn):
        ins = [0, 50, 80]
        pr = [0.50, 0.70, 1.00]
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=ins,
            price_thresholds=pr,
            conn=test_conn,
        )
        # 3 insider × 3 price = 9 cells
        assert len(result["grid"]) == 9

    def test_cell_keys(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0],
            price_thresholds=[1.00],
            conn=test_conn,
        )
        cell = result["grid"][0]
        assert "insider" in cell
        assert "price" in cell
        assert "trade_count" in cell
        assert "win_rate" in cell
        assert "total_pnl" in cell
        assert "avg_roi" in cell
        assert "max_drawdown" in cell

    def test_no_filter_cell_matches_all(self, test_conn):
        """insider=0 + price=1.00 means no filters — should match all trades."""
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0],
            price_thresholds=[1.00],
            conn=test_conn,
        )
        cell = result["grid"][0]
        assert cell["trade_count"] == 5

    def test_tight_filters_reduce_count(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0, 80],
            price_thresholds=[0.50, 1.00],
            conn=test_conn,
        )
        lookup = {(c["insider"], c["price"]): c for c in result["grid"]}
        # insider>=80, price<=0.50: trades with insider 90,80 AND entry<=0.50
        # trade 1: insider=90, entry=0.40 ✓  trade 2: insider=80, entry=0.50 ✓
        assert lookup[(80, 0.50)]["trade_count"] == 2
        # insider>=0, price<=1.00: all 5
        assert lookup[(0, 1.00)]["trade_count"] == 5

    def test_best_cell_requires_min_trades(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[95],
            price_thresholds=[0.50],
            conn=test_conn,
        )
        # Only 1 trade at insider>=95, price<=0.50 — below 3-trade minimum
        assert result["best_cell"] is None

    def test_best_cell_found(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0, 50, 70],
            price_thresholds=[0.50, 0.70, 1.00],
            conn=test_conn,
        )
        best = result["best_cell"]
        assert best is not None
        assert best["trade_count"] >= 3
        # Verify it's actually the highest P&L among cells with >=3 trades
        eligible = [c for c in result["grid"] if c["trade_count"] >= 3]
        max_pnl = max(c["total_pnl"] for c in eligible)
        assert best["total_pnl"] == max_pnl

    def test_position_signals_source(self, test_conn):
        result = sweep_2d(
            data_source="position_signals",
            insider_thresholds=[0, 80],
            price_thresholds=[0.50, 1.00],
            conn=test_conn,
        )
        assert result["data_source"] == "position_signals"
        assert result["bias_warning"] is not None
        assert len(result["grid"]) == 4

    def test_bias_warning_paper(self, test_conn):
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0],
            price_thresholds=[1.00],
            conn=test_conn,
        )
        assert result["bias_warning"] is None

    def test_base_config_speculator_gate(self, test_conn):
        """max_speculator_score from base_config should propagate to all cells."""
        base = AnalysisConfig(max_speculator_score=30)
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[0],
            price_thresholds=[1.00],
            base_config=base,
            conn=test_conn,
        )
        cell = result["grid"][0]
        # spec scores: 10, 20 pass (< 30); 30, 40, 60 filtered (>= 30)
        assert cell["trade_count"] == 2

    def test_price_cap_note_when_non_binding(self, test_conn):
        """price_cap_note appears when higher price thresholds add no trades."""
        result = sweep_2d(
            data_source="paper_trades",
            # At insider>=70: 3 trades with entry 0.40, 0.50, 0.30 — all below 0.50
            # So price ≤0.50, ≤0.70, ≤1.00 all give trade_count=3
            insider_thresholds=[70],
            price_thresholds=[0.50, 0.70, 1.00],
            conn=test_conn,
        )
        best = result["best_cell"]
        assert best is not None
        # best should be at price=0.50 (first with highest P&L since all same count)
        # price_cap_note should fire because higher thresholds have identical count
        note = result.get("price_cap_note")
        assert note is not None
        assert "non-binding" in note.lower()

    def test_price_cap_note_absent_when_binding(self, test_conn):
        """price_cap_note is None when higher price thresholds change trade count."""
        result = sweep_2d(
            data_source="paper_trades",
            # At insider>=0: all 5 trades. entry prices: 0.40,0.50,0.30,0.60,0.80
            # price≤0.50: 3 trades, price≤0.70: 4 trades, price≤1.00: 5 trades
            # So trade count changes — cap IS binding
            insider_thresholds=[0],
            price_thresholds=[0.50, 0.70, 1.00],
            conn=test_conn,
        )
        note = result.get("price_cap_note")
        assert note is None

    def test_price_cap_note_absent_when_no_best(self, test_conn):
        """price_cap_note is None when there's no best cell."""
        result = sweep_2d(
            data_source="paper_trades",
            insider_thresholds=[95],
            price_thresholds=[0.30],
            conn=test_conn,
        )
        assert result["best_cell"] is None
        assert result.get("price_cap_note") is None
