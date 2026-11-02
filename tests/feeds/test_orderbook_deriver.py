from sniper.feeds.orderbook_deriver import point_from_book


def test_book_snapshot_derives_mid_spread_and_depth():
    msg = {
        "asset_id": "asset1",
        "timestamp": "1000",
        "bids": [{"price": ".48", "size": "30"}],
        "asks": [{"price": ".52", "size": "25"}],
    }
    p = point_from_book(msg)
    assert p.best_bid == 0.48
    assert p.best_ask == 0.52
    assert round(p.mid, 2) == 0.50
    assert round(p.spread, 2) == 0.04
    assert p.bid_depth_1 == 30 and p.ask_depth_1 == 25


def test_book_with_one_empty_side_has_no_mid():
    p = point_from_book({"asset_id": "a", "timestamp": "1", "bids": [{"price": ".4", "size": "1"}], "asks": []})
    assert p.best_ask is None and p.mid is None and p.spread is None
