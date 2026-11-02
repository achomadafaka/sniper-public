"""Tests for the trade idea execution endpoint (news_intel_v1 strategy)."""
import sqlite3
import time

import pytest

from sniper.routes.api_core import execute_trade_idea


def _setup_db(db_path: str) -> sqlite3.Connection:
    """Create a minimal test DB with required tables and seed data."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_ideas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            market_title TEXT NOT NULL,
            outcome      TEXT NOT NULL,
            direction    TEXT NOT NULL,
            current_price REAL,
            target_price  REAL,
            confidence   TEXT NOT NULL DEFAULT 'low',
            reasoning    TEXT NOT NULL,
            news_trigger TEXT,
            news_source  TEXT,
            status       TEXT NOT NULL DEFAULT 'open',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at   TEXT,
            acted_at     TEXT,
            notes        TEXT,
            adversarial_score INTEGER,
            adversarial_reasoning TEXT,
            adversarial_approved INTEGER DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            strategy_type TEXT NOT NULL DEFAULT 'signal_only',
            parameters TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            ai_generated INTEGER NOT NULL DEFAULT 0,
            ai_analysis TEXT NOT NULL DEFAULT '',
            trades_triggered INTEGER NOT NULL DEFAULT 0,
            pnl_usdc REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('pending_entry', 'open', 'pending_exit', 'closed', 'error')),
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
            updated_ts INTEGER NOT NULL,
            size_usdc REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS strategy_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            execution_type TEXT NOT NULL DEFAULT 'paper',
            size_usdc REAL NOT NULL DEFAULT 0,
            entry_price REAL NOT NULL DEFAULT 0,
            token_id TEXT NOT NULL DEFAULT '',
            order_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            pnl_usdc REAL NOT NULL DEFAULT 0,
            error_msg TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Seed: active news_intel_v1 strategy
    conn.execute(
        """INSERT INTO strategies (wallet, label, strategy_type, status)
           VALUES ('system', 'News Intel v1', 'news_intel_v1', 'active')"""
    )
    # Seed: open trade idea
    conn.execute(
        """INSERT INTO trade_ideas
           (condition_id, market_title, outcome, direction, current_price,
            confidence, reasoning, status, adversarial_approved)
           VALUES (
               '0xabc123', 'Will X happen?', 'YES', 'buy_yes', 0.35,
               'high', 'Strong news signal', 'open', 1
           )"""
    )
    conn.commit()
    return conn


def test_execute_creates_position(tmp_path):
    """Executing an open idea creates a strategy_position and marks idea as acted."""
    db_path = str(tmp_path / "test.db")
    conn = _setup_db(db_path)

    result = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)

    assert result["ok"] is True
    assert result["idea_id"] == 1
    assert result["position_id"] is not None
    assert result["outcome"] == "YES"
    assert result["size_usdc"] == 10.0
    assert result["entry_price"] > 0

    # Verify strategy_position was created
    pos = conn.execute(
        "SELECT * FROM strategy_positions WHERE position_id = ?",
        (result["position_id"],)
    ).fetchone()
    assert pos is not None
    assert pos["state"] == "open"
    assert pos["condition_id"] == "0xabc123"
    assert pos["entry_price"] > 0
    assert pos["entry_shares"] > 0

    # Verify strategy_execution was created
    exc = conn.execute(
        "SELECT * FROM strategy_executions WHERE condition_id = '0xabc123'"
    ).fetchone()
    assert exc is not None
    assert exc["execution_type"] == "paper"
    assert exc["status"] == "open"

    # Verify idea marked as acted
    idea = conn.execute("SELECT * FROM trade_ideas WHERE id = 1").fetchone()
    assert idea["status"] == "acted"
    assert idea["acted_at"] is not None

    # Verify trades_triggered incremented
    strat = conn.execute("SELECT * FROM strategies WHERE strategy_type = 'news_intel_v1'").fetchone()
    assert strat["trades_triggered"] == 1

    conn.close()


def test_execute_rejects_acted_idea(tmp_path):
    """Executing an already-acted idea returns an error."""
    db_path = str(tmp_path / "test.db")
    conn = _setup_db(db_path)

    # First execution should succeed
    result1 = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)
    assert result1["ok"] is True

    # Second execution should fail (idea is now 'acted')
    result2 = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)
    assert result2["ok"] is False
    assert "acted" in result2["error"].lower() or "must be 'open'" in result2["error"]

    conn.close()


def test_execute_rejects_pending_adversarial_review(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = _setup_db(db_path)
    conn.execute("UPDATE trade_ideas SET adversarial_approved = NULL WHERE id = 1")
    conn.commit()

    result = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)

    assert result["ok"] is False
    assert "pending adversarial review" in result["error"].lower()
    conn.close()


def test_execute_rejects_adversarially_blocked_idea(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = _setup_db(db_path)
    conn.execute(
        """UPDATE trade_ideas
           SET adversarial_approved = 0,
               adversarial_score = 82,
               adversarial_reasoning = 'Obvious crowded headline with weak timing edge'
           WHERE id = 1"""
    )
    conn.commit()

    result = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)

    assert result["ok"] is False
    assert "rejected by adversarial review" in result["error"].lower()
    conn.close()


def test_execute_duplicate_position_guard(tmp_path):
    """execute_trade_idea is rejected when an open position already exists for that market/outcome.

    Pre-seeds an open position directly (bypassing execute_trade_idea) to prove
    the INSERT...WHERE NOT EXISTS guard fires independently of idea status flow.
    """
    db_path = str(tmp_path / "test.db")
    conn = _setup_db(db_path)

    # Look up the active strategy id that execute_trade_idea would use
    strategy_id = conn.execute(
        "SELECT id FROM strategies WHERE strategy_type='news_intel_v1' AND status='active' LIMIT 1"
    ).fetchone()["id"]

    # Pre-seed an open position for the same (strategy, condition_id, outcome) tuple
    now_ts = int(time.time())
    conn.execute(
        """INSERT INTO strategy_positions
           (wallet, condition_id, outcome, strategy_id, state,
            entry_price, avg_entry_price, entry_shares, filled_entry_shares,
            opened_ts, version, updated_ts, size_usdc)
           VALUES ('news_intel_manual', '0xabc123', 'YES', ?, 'open',
                   0.35, 0.35, 28.5, 28.5, ?, 1, ?, 10.0)""",
        (str(strategy_id), now_ts, now_ts),
    )
    conn.commit()

    existing_count = conn.execute(
        "SELECT COUNT(*) FROM strategy_positions WHERE condition_id='0xabc123' AND outcome='YES' AND state='open'"
    ).fetchone()[0]
    assert existing_count == 1, "Pre-seeded position must exist before the guard test"

    # Attempt to execute the trade idea — should be blocked by duplicate guard
    result = execute_trade_idea(conn, idea_id=1, size_usdc=10.0)
    assert result["ok"] is False, f"Expected duplicate rejection, got: {result}"
    assert "duplicate" in result["error"].lower(), (
        f"Expected 'duplicate' in error message, got: {result['error']}"
    )

    # Confirm no second open position was created
    final_count = conn.execute(
        "SELECT COUNT(*) FROM strategy_positions WHERE condition_id='0xabc123' AND outcome='YES' AND state='open'"
    ).fetchone()[0]
    assert final_count == 1, "Duplicate open position was inserted despite guard"

    conn.close()
