"""Tests for paper_limit_engine.py — paper limit order lifecycle.

Tests cover:
- compute_bid_price math and floor
- PaperLimitEngine: place, cancel, expire, reprice, fill detection
- Dual-label fill logic (strict vs loose)
- strategy_positions creation on strict fill
- Safety rails (max_resting, max_daily_fills)
- Duplicate rejection
- Log helpers
"""

import json
import os
import logging
import sqlite3
import tempfile
import time

import pytest

from db import init_db
from sniper.feeds.polymarket_trade_tap import TradeTick
from sniper.services.paper_limit_engine import PaperLimitEngine, compute_bid_price


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_test_db() -> tuple[sqlite3.Connection, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn, path


def _make_tick(
    token_id: str = "tok_fav",
    condition_id: str = "cond_1",
    price: float = 0.40,
    size: float = 100.0,
    side: str = "SELL",
    timestamp_ms: int = 1_712_000_000_000,
) -> TradeTick:
    return TradeTick(
        token_id=token_id,
        condition_id=condition_id,
        price=price,
        size=size,
        side=side,
        timestamp_ms=timestamp_ms,
    )


def _place_order(engine: PaperLimitEngine, strategy_id: int = 1, condition_id: str = "cond_1") -> int:
    order_id = engine.place_order(
        strategy_id=strategy_id,
        condition_id=condition_id,
        market_title="Test Match",
        favorite_token_id="tok_fav",
        favorite_token_label="Player A",
        favorite_price=0.70,
        min_fill_size=50.0,
    )
    assert order_id is not None
    return order_id


# ---------------------------------------------------------------------------
# compute_bid_price
# ---------------------------------------------------------------------------


def test_compute_bid_price_basic():
    """0.70 × (1 - 0.30) = 0.49"""
    assert compute_bid_price(0.70) == 0.49


def test_compute_bid_price_rounds():
    """0.55 × 0.70 = 0.385 → rounds to 0.39"""
    result = compute_bid_price(0.55)
    assert result == 0.39


def test_compute_bid_price_floor():
    """Very low favorite → bid is floored at min_bid."""
    result = compute_bid_price(0.10, dip_pct=0.90, min_bid=0.05)
    assert result == 0.05


def test_compute_bid_price_custom_dip():
    """Custom dip_pct: 0.60 × (1 - 0.20) = 0.48"""
    assert compute_bid_price(0.60, dip_pct=0.20) == 0.48


def test_compute_bid_price_exact_min_bid():
    """Result exactly equals min_bid — passes through without floor clamping."""
    result = compute_bid_price(0.10, dip_pct=0.50, min_bid=0.05)
    assert result == 0.05


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_creates_row():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        row = conn.execute(
            "SELECT * FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row is not None
        assert row["state"] == "resting"
        assert row["bid_price"] == pytest.approx(0.49)
        assert row["strategy_id"] == 1
        assert row["condition_id"] == "cond_1"
        assert row["favorite_token_id"] == "tok_fav"
    finally:
        conn.close()
        os.unlink(path)


def test_place_order_logs_event():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE order_id = ? AND event_type = 'order_placed'",
            (order_id,),
        ).fetchone()
        assert event is not None
        details = json.loads(event["details_json"])
        assert "bid_price" in details
    finally:
        conn.close()
        os.unlink(path)


def test_place_order_duplicate_rejected():
    """Same (strategy_id, condition_id) returns None — no second row."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        first = _place_order(engine, strategy_id=1, condition_id="cond_dup")
        second = engine.place_order(
            strategy_id=1,
            condition_id="cond_dup",
            market_title="Dup Match",
            favorite_token_id="tok_x",
            favorite_token_label="Player X",
            favorite_price=0.65,
            min_fill_size=50.0,
        )
        assert second is None
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id=1 AND condition_id='cond_dup'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()
        os.unlink(path)


def test_place_order_max_resting_rail():
    """Exceeding max_resting_orders blocks new placements."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        for i in range(3):
            oid = engine.place_order(
                strategy_id=1,
                condition_id=f"cond_{i}",
                market_title=f"Match {i}",
                favorite_token_id=f"tok_{i}",
                favorite_token_label=f"Player {i}",
                favorite_price=0.70,
                min_fill_size=50.0,
                max_resting=3,
            )
            assert oid is not None
        # 4th should fail
        blocked = engine.place_order(
            strategy_id=1,
            condition_id="cond_blocked",
            market_title="Blocked Match",
            favorite_token_id="tok_b",
            favorite_token_label="Player B",
            favorite_price=0.70,
            min_fill_size=50.0,
            max_resting=3,
        )
        assert blocked is None
    finally:
        conn.close()
        os.unlink(path)


def test_place_order_max_daily_fills_rail():
    """After max_daily_fills strict fills, no more placements."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        # Simulate fills by injecting rows into paper_limit_fills via strict check_fill
        # Place 5 orders and fill them all strictly
        for i in range(5):
            oid = engine.place_order(
                strategy_id=1,
                condition_id=f"cond_f{i}",
                market_title=f"Match {i}",
                favorite_token_id="tok_fav",
                favorite_token_label="Player A",
                favorite_price=0.70,
                min_fill_size=50.0,
                max_daily_fills=10,  # high cap so we can place all 5
            )
            assert oid is not None
            tick = _make_tick(price=0.40, size=100.0)
            engine.check_fill(oid, tick, position_size_usdc=25.0)

        # Now try to place another — daily fill cap of 5 should block it
        blocked = engine.place_order(
            strategy_id=1,
            condition_id="cond_blocked2",
            market_title="Blocked 2",
            favorite_token_id="tok_fav",
            favorite_token_label="Player A",
            favorite_price=0.70,
            min_fill_size=50.0,
            max_daily_fills=5,
        )
        assert blocked is None
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# cancel_order / expire_order
# ---------------------------------------------------------------------------


def test_cancel_order_transitions_state():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        engine.cancel_order(order_id, reason="sets_completed")
        row = conn.execute(
            "SELECT state, cancel_reason FROM paper_limit_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        assert row["state"] == "cancelled"
        assert row["cancel_reason"] == "sets_completed"
    finally:
        conn.close()
        os.unlink(path)


def test_cancel_order_logs_event():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        engine.cancel_order(order_id, reason="favorite_flip")
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE order_id=? AND event_type='order_cancelled'",
            (order_id,),
        ).fetchone()
        assert event is not None
    finally:
        conn.close()
        os.unlink(path)


def test_expire_order_transitions_state():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        engine.expire_order(order_id, reason="market_resolved")
        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["state"] == "expired"
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# reprice_order
# ---------------------------------------------------------------------------


def test_reprice_order_large_move():
    """A large favorite price move triggers a reprice."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid = 0.49

        # New favorite price 0.80 → new bid = round(0.80*0.70,2) = 0.56
        # diff = |0.56 - 0.49| = 0.07 >> threshold
        repriced = engine.reprice_order(order_id, new_favorite_price=0.80)
        assert repriced is True

        row = conn.execute(
            "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["bid_price"] == pytest.approx(0.56)
    finally:
        conn.close()
        os.unlink(path)


def test_reprice_below_threshold_skipped():
    """A tiny move (well below threshold) is skipped."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid = 0.49

        # New favorite 0.701 → new bid = round(0.701*0.70,2) = 0.49 — no diff
        repriced = engine.reprice_order(order_id, new_favorite_price=0.701, threshold=0.02)
        assert repriced is False
    finally:
        conn.close()
        os.unlink(path)


def test_reprice_logs_event_on_success():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        engine.reprice_order(order_id, new_favorite_price=0.80)
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE order_id=? AND event_type='order_repriced'",
            (order_id,),
        ).fetchone()
        assert event is not None
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# check_fill — strict
# ---------------------------------------------------------------------------


def test_check_fill_strict():
    """Price <= bid AND size >= min_fill_size → strict fill, order transitions to filled."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid=0.49, min_fill_size=50

        tick = _make_tick(price=0.40, size=100.0)
        result = engine.check_fill(order_id, tick, position_size_usdc=25.0)
        assert result == "strict"

        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["state"] == "filled"
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_strict_creates_both_fill_rows():
    """On strict fill, both 'strict' and 'loose' fill labels are logged."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        tick = _make_tick(price=0.40, size=100.0)
        engine.check_fill(order_id, tick, position_size_usdc=25.0)

        fills = conn.execute(
            "SELECT fill_label FROM paper_limit_fills WHERE order_id = ?", (order_id,)
        ).fetchall()
        labels = {f["fill_label"] for f in fills}
        assert "strict" in labels
        assert "loose" in labels
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_strict_creates_strategy_position():
    """Strict fill creates a row in strategy_positions with current_price = entry_price."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        tick = _make_tick(price=0.40, size=100.0)
        engine.check_fill(order_id, tick, position_size_usdc=25.0)

        pos = conn.execute(
            "SELECT * FROM strategy_positions WHERE instance_tag = 'stink-bid-paper'"
        ).fetchone()
        assert pos is not None
        assert pos["state"] == "open"
        assert pos["entry_price"] == pytest.approx(0.40)
        assert pos["current_price"] == pytest.approx(0.40)
        assert pos["current_price"] > 0, "current_price must not be 0 (would trigger immediate loss)"
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_strict_wallet_prefix():
    """The synthetic wallet uses the stink_bid prefix."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine, strategy_id=7)
        tick = _make_tick(price=0.40, size=100.0)
        engine.check_fill(order_id, tick, position_size_usdc=25.0)

        pos = conn.execute(
            "SELECT wallet FROM strategy_positions WHERE instance_tag = 'stink-bid-paper'"
        ).fetchone()
        assert pos["wallet"].startswith("paper:stink_bid:")
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_slippage_stored():
    """slippage = bid_price - fill_price is stored correctly."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid = 0.49
        tick = _make_tick(price=0.35, size=100.0)
        engine.check_fill(order_id, tick, position_size_usdc=25.0)

        fill = conn.execute(
            "SELECT slippage FROM paper_limit_fills WHERE order_id = ? AND fill_label = 'strict'",
            (order_id,),
        ).fetchone()
        assert fill["slippage"] == pytest.approx(0.49 - 0.35, abs=1e-6)
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# check_fill — loose only
# ---------------------------------------------------------------------------


def test_check_fill_loose_only():
    """Price <= bid but size < min_fill_size → loose only, order stays resting."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid=0.49, min_fill_size=50

        tick = _make_tick(price=0.40, size=10.0)  # size below min
        result = engine.check_fill(order_id, tick, position_size_usdc=25.0)
        assert result == "loose"

        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["state"] == "resting"  # still resting

        fills = conn.execute(
            "SELECT fill_label FROM paper_limit_fills WHERE order_id = ?", (order_id,)
        ).fetchall()
        labels = {f["fill_label"] for f in fills}
        assert labels == {"loose"}  # only loose, no strict
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_loose_logs_fill_loose_event():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        tick = _make_tick(price=0.40, size=5.0)
        engine.check_fill(order_id, tick, position_size_usdc=25.0)
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE order_id=? AND event_type='fill_loose'",
            (order_id,),
        ).fetchone()
        assert event is not None
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# check_fill — price above bid (no fill)
# ---------------------------------------------------------------------------


def test_check_fill_price_above_bid():
    """Trade price above bid → no fill, returns None."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # bid=0.49

        tick = _make_tick(price=0.55, size=100.0)  # price > bid
        result = engine.check_fill(order_id, tick)
        assert result is None

        row = conn.execute(
            "SELECT state FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        assert row["state"] == "resting"
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_wrong_token_id():
    """Trade on a different token → no fill."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)  # tok_fav

        tick = _make_tick(token_id="tok_different", price=0.30, size=200.0)
        result = engine.check_fill(order_id, tick)
        assert result is None
    finally:
        conn.close()
        os.unlink(path)


def test_check_fill_not_resting_returns_none():
    """Cancelled/expired orders are not evaluated for fill."""
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        order_id = _place_order(engine)
        engine.cancel_order(order_id, reason="manual")

        tick = _make_tick(price=0.30, size=200.0)
        result = engine.check_fill(order_id, tick)
        assert result is None
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# Helper methods
# ---------------------------------------------------------------------------


def test_count_resting():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        assert engine.count_resting(strategy_id=1) == 0
        _place_order(engine, strategy_id=1, condition_id="c1")
        _place_order(engine, strategy_id=1, condition_id="c2")
        assert engine.count_resting(strategy_id=1) == 2
    finally:
        conn.close()
        os.unlink(path)


def test_has_order_for_match():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        assert engine.has_order_for_match(1, "cond_new") is False
        _place_order(engine, strategy_id=1, condition_id="cond_new")
        assert engine.has_order_for_match(1, "cond_new") is True
    finally:
        conn.close()
        os.unlink(path)


def test_get_resting_orders():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        _place_order(engine, strategy_id=1, condition_id="c1")
        _place_order(engine, strategy_id=1, condition_id="c2")
        orders = engine.get_resting_orders(strategy_id=1)
        assert len(orders) == 2
        assert all(o["state"] == "resting" for o in orders)
    finally:
        conn.close()
        os.unlink(path)


def test_get_resting_orders_all_strategies():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        _place_order(engine, strategy_id=1, condition_id="c1")
        _place_order(engine, strategy_id=2, condition_id="c2")
        all_orders = engine.get_resting_orders()  # no filter
        assert len(all_orders) == 2
    finally:
        conn.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# log_skip
# ---------------------------------------------------------------------------


def test_log_skip():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        engine.log_skip(
            strategy_id=1,
            condition_id="cond_x",
            reason="max_resting_reached",
            details={"max_resting": 3, "current": 3},
        )
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE event_type = 'order_skipped'"
        ).fetchone()
        assert event is not None
        details = json.loads(event["details_json"])
        assert details["reason"] == "max_resting_reached"
        assert details["strategy_id"] == 1
    finally:
        conn.close()
        os.unlink(path)


def test_log_skip_without_details():
    conn, path = _init_test_db()
    try:
        engine = PaperLimitEngine(conn)
        # Should not raise even without explicit details dict
        engine.log_skip(strategy_id=2, condition_id="cond_y", reason="below_volume")
        event = conn.execute(
            "SELECT * FROM paper_limit_events WHERE event_type = 'order_skipped'"
        ).fetchone()
        assert event is not None
    finally:
        conn.close()
        os.unlink(path)


def test_log_event_swallows_sqlite_lock_and_warns(caplog):
    class LockedConn:
        def __init__(self) -> None:
            self.row_factory = sqlite3.Row

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise AssertionError("commit should not run after execute lock failure")

    engine = PaperLimitEngine(LockedConn())

    with caplog.at_level(logging.WARNING):
        engine._log_event(
            order_id=123,
            event_type="order_skipped",
            state_before="resting",
            state_after="resting",
            details={"reason": "db_lock"},
        )

    assert "paper_limit_events insert failed" in caplog.text
    assert "event_type=order_skipped" in caplog.text
    assert "order_id=123" in caplog.text
