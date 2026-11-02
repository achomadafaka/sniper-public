"""Tests for feeds/polymarket_trade_tap.py — parsing and state management only.

No live WebSocket connections are made. All tests are pure-unit.
"""

from sniper.feeds.polymarket_trade_tap import TradeTick, parse_trade_message, TradeTapState


def test_parse_valid_trade():
    msg = [
        {
            "event_type": "last_trade_price",
            "asset_id": "tok_abc",
            "market": "cond_123",
            "price": "0.45",
            "size": "100",
            "side": "SELL",
            "timestamp_ms": 1712000000000,
        }
    ]
    ticks = parse_trade_message(msg)
    assert len(ticks) == 1
    assert ticks[0].token_id == "tok_abc"
    assert ticks[0].price == 0.45
    assert ticks[0].size == 100.0
    assert ticks[0].side == "SELL"
    assert ticks[0].condition_id == "cond_123"
    assert ticks[0].timestamp_ms == 1712000000000


def test_parse_ignores_non_trade_events():
    msg = [{"event_type": "price_change", "asset_id": "tok_abc"}]
    assert parse_trade_message(msg) == []


def test_parse_handles_missing_fields():
    msg = [{"event_type": "last_trade_price"}]
    assert parse_trade_message(msg) == []


def test_parse_handles_malformed_price():
    msg = [
        {
            "event_type": "last_trade_price",
            "asset_id": "tok_abc",
            "market": "cond_123",
            "price": "not_a_number",
            "size": "100",
            "side": "BUY",
            "timestamp_ms": 1712000000000,
        }
    ]
    # ValueError on float() — entry should be skipped
    assert parse_trade_message(msg) == []


def test_parse_multiple_events_mixed():
    msg = [
        {
            "event_type": "last_trade_price",
            "asset_id": "tok_1",
            "market": "cond_A",
            "price": "0.60",
            "size": "50",
            "side": "BUY",
            "timestamp_ms": 1712000000001,
        },
        {"event_type": "book", "asset_id": "tok_2"},
        {
            "event_type": "last_trade_price",
            "asset_id": "tok_3",
            "market": "cond_B",
            "price": "0.30",
            "size": "200",
            "side": "SELL",
            "timestamp_ms": 1712000000002,
        },
    ]
    ticks = parse_trade_message(msg)
    assert len(ticks) == 2
    assert ticks[0].token_id == "tok_1"
    assert ticks[1].token_id == "tok_3"


def test_parse_empty_list():
    assert parse_trade_message([]) == []


def test_parse_non_list_entry_skipped():
    # Each entry in the outer list should be a dict; non-dicts are skipped
    msg = ["not_a_dict", None, 42]
    assert parse_trade_message(msg) == []


def test_state_watch_unwatch():
    state = TradeTapState()
    state.watch("tok_1")
    state.watch("tok_2")
    assert state.watched_tokens == {"tok_1", "tok_2"}
    state.unwatch("tok_1")
    assert state.watched_tokens == {"tok_2"}


def test_state_unwatch_nonexistent_is_noop():
    state = TradeTapState()
    state.watch("tok_1")
    state.unwatch("tok_999")  # should not raise
    assert state.watched_tokens == {"tok_1"}


def test_state_filters_ticks():
    state = TradeTapState()
    state.watch("tok_1")
    assert state.is_watched("tok_1") is True
    assert state.is_watched("tok_2") is False


def test_state_initially_empty():
    state = TradeTapState()
    assert state.watched_tokens == set()
    assert state.is_watched("anything") is False


def test_trade_tick_dataclass_fields():
    tick = TradeTick(
        token_id="tok_x",
        condition_id="cond_y",
        price=0.55,
        size=75.0,
        side="BUY",
        timestamp_ms=1712000000000,
    )
    assert tick.token_id == "tok_x"
    assert tick.condition_id == "cond_y"
    assert tick.price == 0.55
    assert tick.size == 75.0
    assert tick.side == "BUY"
    assert tick.timestamp_ms == 1712000000000
