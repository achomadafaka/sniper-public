"""Tests for performance agent SQL patterns and analysis logic."""

import sqlite3
import pytest


@pytest.fixture
def perf_db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE paper_trades (
            trade_id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT,
            outcome TEXT, market_title TEXT, entry_price REAL,
            exit_price REAL, size_usdc REAL, pnl_usdc REAL,
            signal_insider_score REAL, signal_speculator_score REAL,
            status TEXT, opened_at TEXT, closed_at TEXT
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY, strategy_id TEXT,
            condition_id TEXT, outcome TEXT, state TEXT,
            entry_price REAL, pnl_usdc REAL, realized_pnl REAL,
            opened_ts INTEGER, closed_ts INTEGER
        );
    """)
    return p, c


def test_insider_band_analysis(perf_db):
    """Score band WR query returns correct grouping."""
    p, c = perf_db
    # Insert trades in different bands
    c.executemany(
        "INSERT INTO paper_trades (trade_id, wallet, signal_insider_score, status, pnl_usdc, entry_price) VALUES (?,?,?,?,?,?)",
        [
            (1, "0xa", 95, "resolution_win", 10, 0.5),
            (2, "0xa", 92, "resolution_win", 8, 0.4),
            (3, "0xa", 91, "resolution_loss", -12, 0.6),
            (4, "0xb", 75, "resolution_win", 5, 0.3),
            (5, "0xb", 72, "resolution_loss", -8, 0.5),
            (6, "0xb", 71, "resolution_loss", -7, 0.4),
        ],
    )
    c.commit()
    rows = c.execute(
        """
        SELECT CASE WHEN signal_insider_score >= 90 THEN '90-100'
                    WHEN signal_insider_score >= 70 THEN '70-79' END as band,
          COUNT(*) as total,
          SUM(CASE WHEN status='resolution_win' THEN 1 ELSE 0 END) as wins,
          ROUND(SUM(pnl_usdc), 2) as pnl
        FROM paper_trades WHERE status IN ('resolution_win','resolution_loss')
        GROUP BY band ORDER BY band DESC
    """
    ).fetchall()
    assert len(rows) == 2
    # 90-100 band: 2 wins, 1 loss, PnL = 10+8-12 = 6
    assert rows[0][1] == 3  # total
    assert rows[0][2] == 2  # wins
    assert rows[0][3] == 6.0  # pnl
    # 70-79 band: 1 win, 2 losses
    assert rows[1][2] == 1


def test_false_positive_detection(perf_db):
    """High-score losses grouped by wallet."""
    p, c = perf_db
    c.executemany(
        "INSERT INTO paper_trades (trade_id, wallet, signal_insider_score, status, pnl_usdc, entry_price, market_title) VALUES (?,?,?,?,?,?,?)",
        [
            (1, "0xbad", 100, "resolution_loss", -50, 0.5, "Tennis A"),
            (2, "0xbad", 100, "resolution_loss", -40, 0.6, "Tennis B"),
            (3, "0xbad", 95, "resolution_loss", -30, 0.4, "Tennis C"),
            (4, "0xgood", 92, "resolution_win", 20, 0.5, "Politics A"),
        ],
    )
    c.commit()
    rows = c.execute(
        """
        SELECT wallet, COUNT(*) as loss_count, ROUND(SUM(pnl_usdc), 2) as total_loss
        FROM paper_trades WHERE signal_insider_score >= 85 AND status='resolution_loss'
        GROUP BY wallet ORDER BY total_loss ASC
    """
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xbad"
    assert rows[0][1] == 3
    assert rows[0][2] == -120.0


def test_plugin_performance_query(perf_db):
    """Per-plugin WR and PnL aggregation."""
    p, c = perf_db
    c.execute(
        "INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1, 'auto', 'Sports NO', 'sports_no_value_v1', 'active', '2026-03-20', '2026-03-20')"
    )
    c.executemany(
        "INSERT INTO strategy_positions (position_id, strategy_id, state, pnl_usdc, opened_ts, closed_ts) VALUES (?,?,?,?,?,?)",
        [
            (1, "1", "closed", 5, 1000, 2000),
            (2, "1", "closed", -15, 1000, 2000),
            (3, "1", "closed", -10, 1000, 2000),
            (4, "1", "open", 2, 1000, 0),
        ],
    )
    c.commit()
    rows = c.execute(
        """
        SELECT s.label,
          COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed,
          SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
          ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl
        FROM strategies s
        LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id
        GROUP BY s.id
    """
    ).fetchall()
    assert rows[0][1] == 3  # 3 closed
    assert rows[0][2] == 1  # 1 win
    assert rows[0][3] == -20.0  # total pnl


def test_consecutive_losses(perf_db):
    """Detect consecutive losses from most recent trades."""
    p, c = perf_db
    c.execute(
        "INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1, 'auto', 'Loser', 'term_structure_v1', 'active', '2026-03-20', '2026-03-20')"
    )
    c.executemany(
        "INSERT INTO strategy_positions (position_id, strategy_id, state, pnl_usdc, closed_ts) VALUES (?,?,?,?,?)",
        [
            (1, "1", "closed", -10, 100),
            (2, "1", "closed", -8, 200),
            (3, "1", "closed", -12, 300),
            (4, "1", "closed", 5, 400),  # one win breaks the streak
            (5, "1", "closed", -7, 500),
        ],
    )
    c.commit()
    # Count recent consecutive losses (from most recent backward)
    rows = c.execute(
        """
        SELECT pnl_usdc FROM strategy_positions
        WHERE strategy_id='1' AND state='closed'
        ORDER BY closed_ts DESC
    """
    ).fetchall()
    streak = 0
    for r in rows:
        if r[0] <= 0:
            streak += 1
        else:
            break
    assert streak == 1  # only the most recent is a loss, then a win at position 4
