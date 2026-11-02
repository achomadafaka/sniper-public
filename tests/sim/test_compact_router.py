# tests/sim/test_compact_router.py
from sniper.sim.compact.router import route_event
from sniper.sim.schema import RawEvent, EventType


def _make_event(event_type: EventType, payload: dict, **kwargs) -> RawEvent:
    defaults = dict(
        venue="polymarket", market_id="pm:0xtest", venue_market_id="0xtest",
        market_type="geopolitical", capture_ts="2026-04-03T12:00:00.000Z",
        ingest_seq=1, asset_id="asset123",
    )
    defaults.update(kwargs)
    return RawEvent(event_type=event_type, payload=payload, **defaults)


def test_route_trade():
    evt = _make_event(EventType.TRADE, {
        "price": "0.65", "size": "100.5", "side": "BUY",
        "fee_rate_bps": "0", "transaction_hash": "0xabc",
    }, exchange_ts="2026-04-03T12:00:00.000Z")
    table, row = route_event(evt)
    assert table == "trades"
    assert row["price"] == 0.65
    assert row["size"] == 100.5
    assert row["side"] == "BUY"
    assert row["fee_rate_bps"] == 0
    assert row["transaction_hash"] == "0xabc"
    assert row["market_id"] == "pm:0xtest"
    assert row["venue"] == "polymarket"


def test_route_trade_nullable_fields():
    evt = _make_event(EventType.TRADE, {
        "price": "0.50", "size": "10", "side": "SELL",
    })
    table, row = route_event(evt)
    assert table == "trades"
    assert row["fee_rate_bps"] is None
    assert row["transaction_hash"] is None


def test_route_book_delta_with_bba():
    evt = _make_event(EventType.BOOK_DELTA, {
        "price": "0.62", "size": "500", "side": "BUY",
        "best_bid": "0.61", "best_ask": "0.64",
    })
    tables = route_event(evt)
    # book_delta produces TWO rows: one for book_deltas table, one for quotes_top
    assert isinstance(tables, list)
    assert len(tables) == 2
    delta_table, delta_row = tables[0]
    quote_table, quote_row = tables[1]
    assert delta_table == "book_deltas"
    assert delta_row["price"] == 0.62
    assert delta_row["best_bid"] == 0.61
    assert delta_row["best_ask"] == 0.64
    assert quote_table == "quotes_top"
    assert quote_row["best_bid"] == 0.61
    assert quote_row["best_ask"] == 0.64
    assert quote_row["source_event"] == "book_delta"
    # bid_size: only set when side=BUY and price=best_bid
    assert quote_row["bid_size"] is None  # price 0.62 != best_bid 0.61


def test_route_book_delta_bid_size_when_at_top():
    evt = _make_event(EventType.BOOK_DELTA, {
        "price": "0.61", "size": "500", "side": "BUY",
        "best_bid": "0.61", "best_ask": "0.64",
    })
    tables = route_event(evt)
    quote_row = tables[1][1]
    assert quote_row["bid_size"] == 500.0  # price matches best_bid


def test_route_best_bba():
    evt = _make_event(EventType.BEST_BBA, {
        "best_bid": "0.60", "best_ask": "0.65",
    })
    table, row = route_event(evt)
    assert table == "quotes_top"
    assert row["best_bid"] == 0.60
    assert row["best_ask"] == 0.65
    assert row["bid_size"] is None
    assert row["ask_size"] is None
    assert row["source_event"] == "best_bba"


def test_route_book_snapshot():
    evt = _make_event(EventType.BOOK_SNAPSHOT, {
        "bids": [{"price": "0.60", "size": "1000"}, {"price": "0.59", "size": "500"}],
        "asks": [{"price": "0.65", "size": "800"}, {"price": "0.66", "size": "200"}],
    })
    tables = route_event(evt)
    assert isinstance(tables, list)
    # Should produce: 1 quotes_top row + 4 book_levels rows (2 bids + 2 asks)
    quote_rows = [(t, r) for t, r in tables if t == "quotes_top"]
    level_rows = [(t, r) for t, r in tables if t == "book_levels"]
    assert len(quote_rows) == 1
    assert len(level_rows) == 4
    assert quote_rows[0][1]["best_bid"] == 0.60
    assert quote_rows[0][1]["best_ask"] == 0.65
    assert quote_rows[0][1]["bid_size"] == 1000.0
    assert quote_rows[0][1]["ask_size"] == 800.0
    assert quote_rows[0][1]["source_event"] == "book_snapshot"
    # Check level ordering
    bid_levels = [r for t, r in level_rows if r["side"] == "bid"]
    assert bid_levels[0]["level_index"] == 0
    assert bid_levels[0]["price"] == 0.60
    assert bid_levels[1]["level_index"] == 1


def test_route_market_metadata():
    evt = _make_event(EventType.MARKET_METADATA, {
        "question": "US strikes Iran?", "outcomes": ["Yes", "No"],
    }, asset_id=None)
    table, row = route_event(evt)
    assert table == "market_lifecycle"
    assert row["event_type"] == "market_metadata"
    assert "question" in row["payload"]


def test_route_market_resolved():
    evt = _make_event(EventType.MARKET_RESOLVED, {
        "outcome_prices": ["0.01", "0.99"], "resolved_ts": "2026-04-15T12:00:00.000Z",
    }, asset_id=None)
    table, row = route_event(evt)
    assert table == "market_lifecycle"
    assert row["event_type"] == "market_resolved"
