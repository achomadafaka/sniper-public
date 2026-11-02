# tests/test_price_flow_e2e.py
"""End-to-end price flow tests — verify prices are correct at every step
from trade idea -> news_intel plugin discover -> strategy position.

These tests would have caught the BUY_NO price inversion bug."""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock


def _valid_condition_id(n: int) -> str:
    return f"0x{n:064x}"


@pytest.fixture
def e2e_db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE trade_ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, market_title TEXT, outcome TEXT,
            direction TEXT, reasoning TEXT, current_price REAL,
            target_price REAL, confidence TEXT, news_trigger TEXT,
            news_source TEXT, expires_at TEXT, status TEXT DEFAULT 'open',
            driving_event TEXT, alerted_at TEXT, source_agent TEXT
        );
        CREATE TABLE market_metadata (
            condition_id TEXT PRIMARY KEY, end_date_iso TEXT, category TEXT
        );
        CREATE TABLE paper_trades (
            trade_id INTEGER PRIMARY KEY, condition_id TEXT, outcome TEXT,
            status TEXT DEFAULT 'open', entry_price REAL, pnl_usdc REAL,
            duplicate_of TEXT
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT, condition_id TEXT, outcome TEXT,
            strategy_id TEXT, state TEXT DEFAULT 'open',
            entry_price REAL, avg_entry_price REAL, pnl_usdc REAL DEFAULT 0
        );
    """)
    return p, c


def test_buy_yes_price_flow(e2e_db):
    """BUY_YES: trade_ideas.current_price IS the YES price.
    Signal should carry the YES price through to entry."""
    p, c = e2e_db

    # Create a BUY_YES trade idea — current_price is YES price
    condition_id = _valid_condition_id(1)
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES (?, 'Will X happen?', 'YES', 'buy_yes', 0.35, 'high', 'open')""", (condition_id,))
    c.commit()

    # Simulate news_intel_v1 discover()
    from sniper.strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.35):
        signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    signal = signals[0]
    assert signal.outcome == "Yes"
    # For BUY_YES, current_price should be the YES price (0.35)
    assert signal.current_price == 0.35, f"BUY_YES signal price should be 0.35 (YES price), got {signal.current_price}"


def test_buy_no_price_flow(e2e_db):
    """BUY_NO: trade_ideas.current_price IS the NO price (tradeable side).
    Signal should carry the NO price through to entry."""
    p, c = e2e_db

    # Create a BUY_NO trade idea — current_price is the NO price (0.65)
    # This means YES price is 0.35, NO price is 0.65
    condition_id = _valid_condition_id(2)
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES (?, 'Will Y happen?', 'NO', 'buy_no', 0.65, 'high', 'open')""", (condition_id,))
    c.commit()

    from sniper.strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.35):
        signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    signal = signals[0]
    assert signal.outcome == "No"
    # For BUY_NO, current_price should be the NO price (0.65)
    # NOT the YES price, NOT 1-price inverted
    assert signal.current_price == 0.65, f"BUY_NO signal price should be 0.65 (NO price), got {signal.current_price}"


def test_buy_no_high_price_rejected(e2e_db):
    """BUY_NO with NO price > 0.92 should be rejected by price gate."""
    p, c = e2e_db

    # Near-certain NO market — NO price is 0.95 (YES is 0.05)
    condition_id = _valid_condition_id(3)
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES (?, 'Certain thing?', 'NO', 'buy_no', 0.95, 'high', 'open')""", (condition_id,))
    c.commit()

    from sniper.strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.05):
        signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    # Entry should be rejected by price gate
    decision = plugin.enter(c, strategy, signals[0], [])
    assert decision.should_enter == False
    assert "sane range" in decision.reason.lower() or "0.92" in decision.reason


def test_buy_no_low_price_rejected(e2e_db):
    """BUY_NO with NO price < 0.05 should be rejected."""
    p, c = e2e_db

    # Near-certain YES market — NO price is 0.03
    condition_id = _valid_condition_id(4)
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES (?, 'Almost certain?', 'NO', 'buy_no', 0.03, 'high', 'open')""", (condition_id,))
    c.commit()

    from sniper.strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    with patch("sniper.strategies.news_intel_v1._fetch_yes_price", return_value=0.97):
        signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    decision = plugin.enter(c, strategy, signals[0], [])
    assert decision.should_enter == False
