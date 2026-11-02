from sniper.algos.polymarket_crypto5m import maker_regime_eval as EV


def test_buy_fill_cash_edge_and_future_unwind_markout():
    fill = {"side": "bid", "price": 0.45, "size": 10.0, "fair": 0.52}
    future_book = {"bid": 0.42, "ask": 0.44}

    assert EV.fill_cash_delta(fill) == -4.5
    assert EV.fill_edge_value(fill) == 0.7
    assert EV.future_unwind_value(fill, future_book) == -0.3
    assert EV.adverse_selection_bps(fill, future_book) == -300.0


def test_sell_fill_cash_edge_and_future_unwind_markout():
    fill = {"side": "ask", "price": 0.58, "size": 10.0, "fair": 0.52}
    future_book = {"bid": 0.60, "ask": 0.62}

    assert EV.fill_cash_delta(fill) == 5.8
    assert EV.fill_edge_value(fill) == 0.6
    assert EV.future_unwind_value(fill, future_book) == -0.4
    assert EV.adverse_selection_bps(fill, future_book) == -400.0


def test_missing_future_executable_price_returns_none():
    buy_fill = {"side": "bid", "price": 0.45, "size": 10.0, "fair": 0.52}
    sell_fill = {"side": "ask", "price": 0.58, "size": 10.0, "fair": 0.52}

    assert EV.future_unwind_value(buy_fill, {"ask": 0.50}) is None
    assert EV.future_unwind_value(sell_fill, {"bid": 0.50}) is None
    assert EV.adverse_selection_bps(buy_fill, {"ask": 0.50}) is None


def test_mfe_mae_uses_executable_unwind_path():
    fill = {"side": "bid", "price": 0.50, "size": 20.0, "fair": 0.52}
    future_books = [
        {"bid": 0.49, "ask": 0.51},
        {"bid": 0.56, "ask": 0.58},
        {"bid": 0.47, "ask": 0.49},
    ]

    mfe, mae = EV.mfe_mae_bps(fill, future_books)

    assert mfe == 600.0
    assert mae == -300.0
