from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash


def test_payload_hash_is_stable_for_key_order():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert payload_hash(a) == payload_hash(b)


def test_feed_event_keeps_observed_and_source_time_separate():
    event = FeedEvent(
        source="polymarket_clob_ws", source_event_type="price_change",
        observed_ts_ms=1000, event_ts_ms=900, market_id="m", condition_id="c",
        asset_id="a", event_slug="slug", payload={"x": 1}, payload_hash=payload_hash({"x": 1}),
    )
    assert event.observed_ts_ms == 1000
    assert event.event_ts_ms == 900
