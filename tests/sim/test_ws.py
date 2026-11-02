# tests/sim/test_ws.py
from sniper.sim.collector.ws import parse_ws_message
from sniper.sim.schema import EventType


def _make_seq_gen(start=0):
    """Create a sequence generator for testing."""
    seq = start
    def next_seq():
        nonlocal seq
        seq += 1
        return seq
    return next_seq


def test_parse_last_trade_price():
    raw = {
        "event_type": "last_trade_price",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "price": "0.65",
        "size": "100.5",
        "side": "BUY",
        "timestamp": "1711929600",
    }
    seq_gen = _make_seq_gen(41)
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        next_seq=seq_gen,
        capture_ts="2026-04-01T14:00:00.000Z",
    )
    assert len(events) == 1
    evt = events[0]
    assert evt.event_type == EventType.TRADE
    assert evt.asset_id == "36272374"
    assert evt.venue == "polymarket"
    assert evt.ingest_seq == 42
    assert evt.exchange_ts == "2024-04-01T00:00:00.000Z"
    assert evt.payload["price"] == "0.65"
    assert evt.payload["size"] == "100.5"
    assert evt.payload["side"] == "BUY"


def test_parse_price_change_unique_seqs():
    """price_change fan-out: each emitted event gets its own unique ingest_seq."""
    raw = {
        "event_type": "price_change",
        "market": "0xcondition123",
        "price_changes": [
            {"asset_id": "token_a", "price": "0.70", "size": "500", "side": "BUY"},
            {"asset_id": "token_b", "price": "0.30", "size": "500", "side": "SELL"},
            {"asset_id": "token_c", "price": "0.50", "size": "200", "side": "BUY"},
        ],
        "timestamp": "1711929601",
    }
    seq_gen = _make_seq_gen(0)
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        next_seq=seq_gen,
        capture_ts="2026-04-01T14:00:01.000Z",
    )
    assert len(events) == 3
    seqs = [e.ingest_seq for e in events]
    assert seqs == [1, 2, 3]  # each unique, monotonically increasing
    assert all(e.event_type == EventType.BOOK_DELTA for e in events)
    assert events[0].asset_id == "token_a"
    assert events[1].asset_id == "token_b"
    assert events[2].asset_id == "token_c"


def test_parse_book():
    raw = {
        "event_type": "book",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "bids": [{"price": "0.60", "size": "1000"}],
        "asks": [{"price": "0.65", "size": "500"}],
        "timestamp": "1711929602",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        next_seq=_make_seq_gen(43),
        capture_ts="2026-04-01T14:00:02.000Z",
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.BOOK_SNAPSHOT
    assert events[0].ingest_seq == 44
    assert events[0].exchange_ts == "2024-04-01T00:00:02.000Z"


def test_parse_best_bid_ask():
    raw = {
        "event_type": "best_bid_ask",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "best_bid": "0.61",
        "best_ask": "0.64",
        "spread": "0.03",
        "timestamp": "1711929603",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        next_seq=_make_seq_gen(44),
        capture_ts="2026-04-01T14:00:03.000Z",
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.BEST_BBA
    assert events[0].ingest_seq == 45
    assert events[0].exchange_ts == "2024-04-01T00:00:03.000Z"


def test_parse_unknown_event_type_returns_empty():
    raw = {"event_type": "unknown_type", "market": "0x123"}
    events = parse_ws_message(
        raw,
        market_id="pm:0x123",
        venue_market_id="0x123",
        market_type="other",
        next_seq=_make_seq_gen(),
        capture_ts="2026-04-01T00:00:00.000Z",
    )
    assert events == []
