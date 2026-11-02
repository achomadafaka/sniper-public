from sniper.feeds.clob_ws_capture import feed_event_from_clob_message


def test_price_change_message_becomes_feed_event():
    msg = {
        "event_type": "price_change",
        "market": "0xmarket",
        "timestamp": "1757908892351",
        "price_changes": [{"asset_id": "asset1", "price": "0.5"}],
    }
    out = feed_event_from_clob_message(msg, observed_ts_ms=1757908892400)
    assert out.source == "polymarket_clob_ws"
    assert out.source_event_type == "price_change"
    assert out.market_id == "0xmarket"
    assert out.asset_id == "asset1"
    assert out.event_ts_ms == 1757908892351
    assert out.observed_ts_ms == 1757908892400      # observed != source time
