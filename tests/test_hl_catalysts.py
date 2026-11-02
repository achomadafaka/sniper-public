from sniper.algos.hl_microstructure.catalysts import match_catalysts


def test_match_catalysts_maps_explicit_symbol_headline():
    news = [{
        "title": "ZEC surges after privacy upgrade receives exchange support",
        "source": "coindesk",
        "published_at_ms": 10_000,
        "received_ts_ms": 10_100,
    }]

    matched = match_catalysts(news, {"BTC", "ZEC"}, now_ms=12_000)

    assert matched["ZEC"].asset == "ZEC"
    assert matched["ZEC"].direction == "long"
    assert matched["ZEC"].catalyst_type == "upgrade"
    assert "explicit_symbol" in matched["ZEC"].reason
    assert "BTC" not in matched


def test_match_catalysts_rejects_stale_and_generic_crypto_for_small_caps():
    news = [
        {"title": "Crypto market rallies as risk appetite improves", "source": "rss", "received_ts_ms": 10_000},
        {"title": "HYPE listing rumor grows after exchange teaser", "source": "theblock", "received_ts_ms": 1_000},
    ]

    matched = match_catalysts(news, {"HYPE", "FARTCOIN"}, now_ms=20_000, max_age_ms=5_000)

    assert matched == {}
