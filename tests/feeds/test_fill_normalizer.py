from sniper.feeds.fill_normalizer import fill_event_from_subgraph_row


def test_subgraph_order_filled_row_normalizes_to_fill_event():
    row = {
        "id": "log1", "timestamp": "1700000000", "maker": "0xM", "taker": "0xT",
        "makerAssetId": "100", "takerAssetId": "0",
        "makerAmountFilled": "5000000", "takerAmountFilled": "2500000",
        "transactionHash": "0xtx",
    }
    fill = fill_event_from_subgraph_row(row)
    assert fill.maker == "0xm"          # lowercased
    assert fill.taker == "0xt"
    assert fill.maker_asset_id == "100"
    assert fill.tx_hash == "0xtx"
    assert fill.maker_amount == 5000000.0


def test_normalizer_derives_price_from_usdc_side():
    from sniper.feeds.fill_normalizer import fill_event_from_subgraph_row
    # maker pays USDC (asset 0) for the token -> price = 10.8B/12B = 0.90
    buy = fill_event_from_subgraph_row({"makerAssetId": "0", "takerAssetId": "T",
                                        "makerAmountFilled": "10800000000", "takerAmountFilled": "12000000000"})
    assert round(buy.price, 4) == 0.9
    # taker pays USDC -> maker sells the token -> price = 8.4M/12M = 0.70
    sell = fill_event_from_subgraph_row({"makerAssetId": "T", "takerAssetId": "0",
                                         "makerAmountFilled": "12000000", "takerAmountFilled": "8400000"})
    assert round(sell.price, 4) == 0.7
    # token-for-token (no USDC side) -> price None
    none = fill_event_from_subgraph_row({"makerAssetId": "A", "takerAssetId": "B",
                                         "makerAmountFilled": "1", "takerAmountFilled": "1"})
    assert none.price is None
