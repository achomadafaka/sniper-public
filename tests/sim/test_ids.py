from sniper.sim.ids import make_market_id, parse_market_id


def test_polymarket_id():
    mid = make_market_id("polymarket", "0xabc123def456")
    assert mid == "pm:0xabc123def456"


def test_kalshi_id():
    mid = make_market_id("kalshi", "KXBTCD-26JAN2017-T100749.99")
    assert mid == "kalshi:KXBTCD-26JAN2017-T100749.99"


def test_parse_polymarket_id():
    venue, raw_id = parse_market_id("pm:0xabc123def456")
    assert venue == "polymarket"
    assert raw_id == "0xabc123def456"


def test_parse_kalshi_id():
    venue, raw_id = parse_market_id("kalshi:KXBTCD-26JAN2017-T100749.99")
    assert venue == "kalshi"
    assert raw_id == "KXBTCD-26JAN2017-T100749.99"


def test_unknown_venue_raises():
    import pytest
    with pytest.raises(ValueError, match="Unknown venue"):
        make_market_id("binance", "BTCUSDT")


def test_parse_invalid_raises():
    import pytest
    with pytest.raises(ValueError, match="Invalid market_id"):
        parse_market_id("nocolon")
