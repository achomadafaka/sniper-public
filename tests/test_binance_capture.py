import os

from sniper.research.leadlag import binance_capture as B


def test_parse_book_ticker_extracts_best_levels_and_lag():
    msg = {"stream": "btcusdt@bookTicker",
           "data": {"e": "bookTicker", "u": 1, "s": "BTCUSDT", "b": "63945.20", "B": "2.683",
                    "a": "63945.30", "A": "6.340", "E": 1000, "T": 990}}
    row = B.parse_message(msg, local_receive_ts=1100)
    assert row["symbol"] == "BTCUSDT"
    assert row["best_bid"] == 63945.20 and row["bid_qty"] == 2.683
    assert row["best_ask"] == 63945.30 and row["ask_qty"] == 6.340
    assert row["source_event_ts"] == 1000 and row["transaction_ts"] == 990
    assert row["capture_lag_ms"] == 100  # local_receive - E


def test_parse_agg_trade_extracts_price_qty_aggressor():
    msg = {"stream": "ethusdt@aggTrade",
           "data": {"e": "aggTrade", "E": 2000, "s": "ETHUSDT", "p": "3001.5", "q": "0.4",
                    "T": 1995, "m": True}}
    row = B.parse_message(msg, local_receive_ts=2050)
    assert row["price"] == 3001.5 and row["quantity"] == 0.4
    assert row["is_buyer_maker"] == 1
    assert row["transaction_ts"] == 1995


def test_parse_raw_trade_stream_extracts_price_qty_aggressor():
    # @aggTrade is dead on some endpoints; @trade (e="trade") carries the same fields.
    msg = {"stream": "btcusdt@trade",
           "data": {"e": "trade", "E": 5000, "T": 4990, "s": "BTCUSDT", "t": 1, "p": "64000.1",
                    "q": "0.01", "m": False}}
    row = B.parse_message(msg, local_receive_ts=5050)
    assert row["price"] == 64000.1 and row["quantity"] == 0.01
    assert row["is_buyer_maker"] == 0
    assert row["transaction_ts"] == 4990


def test_parse_depth_uses_top_of_book():
    msg = {"stream": "solusdt@depth5@100ms",
           "data": {"e": "depthUpdate", "E": 3000, "s": "SOLUSDT",
                    "b": [["150.10", "12.0"], ["150.09", "5.0"]],
                    "a": [["150.12", "8.0"], ["150.13", "3.0"]]}}
    row = B.parse_message(msg, local_receive_ts=3030)
    assert row["best_bid"] == 150.10 and row["bid_qty"] == 12.0
    assert row["best_ask"] == 150.12 and row["ask_qty"] == 8.0


def test_parse_unknown_returns_none():
    assert B.parse_message({"stream": "x@kline", "data": {"e": "kline"}}, 1) is None


def test_insert_rows_persists_and_is_appendonly(tmp_path):
    db = os.path.join(str(tmp_path), "cap.sqlite")
    conn = B.connect(db)
    B.ensure_schema(conn)
    row = B.parse_message({"stream": "btcusdt@bookTicker",
                           "data": {"s": "BTCUSDT", "b": "1", "B": "2", "a": "3", "A": "4", "E": 10}}, 20)
    assert B.insert_rows(conn, [row, None]) == 1
    got = conn.execute("SELECT symbol, best_bid, capture_lag_ms FROM binance_raw_events").fetchone()
    assert got["symbol"] == "BTCUSDT" and got["best_bid"] == 1.0 and got["capture_lag_ms"] == 10
