from sniper.algos.hl_microstructure.book import BookState


def test_book_state_normalizes_unordered_l2_levels_and_spread():
    book = BookState.from_l2(
        "BTC",
        levels=[
            [{"px": "100.0", "sz": "2"}, {"px": "99.5", "sz": "5"}],
            [{"px": "101.0", "sz": "3"}, {"px": "100.5", "sz": "4"}],
        ],
        exchange_ts_ms=1000,
        receive_ts_ms=1100,
    )

    assert book.best_bid == 100.0
    assert book.best_ask == 100.5
    assert round(book.spread_bps, 6) == round((0.5 / 100.25) * 10000, 6)


def test_taker_fill_uses_visible_asks_for_long_and_bids_for_short():
    book = BookState.from_l2(
        "BTC",
        levels=[
            [{"px": "100.0", "sz": "1"}, {"px": "99.0", "sz": "10"}],
            [{"px": "101.0", "sz": "1"}, {"px": "102.0", "sz": "10"}],
        ],
        exchange_ts_ms=1000,
        receive_ts_ms=1100,
    )

    long_fill = book.taker_fill("long", notional_usdc=101)
    short_fill = book.taker_fill("short", notional_usdc=100)

    assert long_fill.fillable is True
    assert long_fill.worst_price == 101.0
    assert long_fill.avg_price == 101.0
    assert short_fill.fillable is True
    assert short_fill.worst_price == 100.0
    assert short_fill.avg_price == 100.0


def test_taker_fill_fails_closed_on_insufficient_depth():
    book = BookState.from_l2(
        "ETH",
        levels=[
            [{"px": "100.0", "sz": "0.01"}],
            [{"px": "101.0", "sz": "0.01"}],
        ],
        exchange_ts_ms=1000,
        receive_ts_ms=1100,
    )

    fill = book.taker_fill("long", notional_usdc=10)

    assert fill.fillable is False
    assert fill.reason == "insufficient_depth"
    assert fill.avg_price is None
