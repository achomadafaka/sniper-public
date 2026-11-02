from sniper.feeds.fomo_feed import normalize_feed_item, filter_curated, CopyEvent

RAW_BUY = {
    "id": "e1", "type": "large_buy", "userId": "u1",
    "tokenAddress": "MINT", "createdAt": "2026-07-05T10:00:00.000Z",
    "body": {"userHandle": "alice", "displayName": "alice", "ticker": "PEPE",
             "price": 0.0047, "marketCap": "4700000", "currentSizeUsd": 3318.0,
             "isFirstBuy": True},
}


def test_normalize_buy_uses_server_clock_for_lag():
    created_epoch = 1783245600.0  # 2026-07-05T10:00:00Z
    ev = normalize_feed_item(RAW_BUY, "2026-07-05T10:00:00.5Z", created_epoch + 0.5)
    assert ev.side == "buy" and ev.mint == "MINT" and ev.ticker == "PEPE"
    assert ev.source_user_id == "u1" and ev.source_handle == "alice"
    assert ev.their_size_usd == 3318.0 and ev.is_first_buy is True
    assert abs(ev.lag_s - 0.5) < 0.01           # skew-free via server epoch


def test_normalize_skips_non_trade_types():
    assert normalize_feed_item({"type": "thesis_created", "body": {}}, "x", None) is None
    assert normalize_feed_item({"type": "new_token_listing", "body": {}}, "x", None) is None


def test_normalize_returns_none_on_malformed():
    assert normalize_feed_item({"type": "large_buy"}, "x", None) is None  # no body/mint


def test_filter_curated():
    a = normalize_feed_item(RAW_BUY, "x", None)
    b = normalize_feed_item({**RAW_BUY, "id": "e2", "userId": "u2"}, "x", None)
    assert [e.source_user_id for e in filter_curated([a, b], {"u1"})] == ["u1"]
