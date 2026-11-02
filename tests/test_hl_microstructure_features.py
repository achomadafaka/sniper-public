from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.features import RollingMarketState


def _book(ts, bid=100.0, ask=100.2):
    return BookState.from_l2(
        "BTC",
        levels=[
            [{"px": str(bid), "sz": "5"}],
            [{"px": str(ask), "sz": "5"}],
        ],
        exchange_ts_ms=ts,
        receive_ts_ms=ts,
    )


def test_snapshot_excludes_events_received_after_decision_time():
    state = RollingMarketState(max_history_ms=120_000)
    state.update_book(_book(1_000, bid=100.0, ask=100.2))
    state.ingest_trade("BTC", side="B", price=100.2, size=1.0, exchange_ts_ms=1_500, receive_ts_ms=1_500)
    state.ingest_trade("BTC", side="B", price=105.0, size=10.0, exchange_ts_ms=2_000, receive_ts_ms=50_000)

    snap = state.snapshot("BTC", decision_ts_ms=2_000)

    assert snap.features["trade_count_10s"] == 1
    assert snap.features["buy_notional_10s"] == 100.2
    assert snap.features["flow_imbalance_10s"] == 1.0


def test_snapshot_reports_book_pressure_and_short_returns():
    state = RollingMarketState(max_history_ms=120_000)
    state.update_book(_book(1_000, bid=100.0, ask=100.2))
    state.update_book(_book(6_000, bid=101.0, ask=101.2))
    state.ingest_trade("BTC", side="A", price=101.0, size=2.0, exchange_ts_ms=6_000, receive_ts_ms=6_000)

    snap = state.snapshot("BTC", decision_ts_ms=6_000)

    assert snap.asset == "BTC"
    assert snap.features["mid"] == 101.1
    assert snap.features["return_5s"] > 0
    assert snap.features["sell_notional_10s"] == 202.0
    assert -0.002 < snap.features["book_imbalance_5"] < 0.0


def test_snapshot_reports_multi_horizon_flow_returns_and_volatility():
    state = RollingMarketState(max_history_ms=180_000)
    for ts, mid in (
        (1_000, 100.0),
        (16_000, 100.4),
        (31_000, 100.8),
        (46_000, 101.0),
        (61_000, 101.2),
    ):
        state.update_book(_book(ts, bid=mid - 0.1, ask=mid + 0.1))
    state.ingest_trade("BTC", side="B", price=101.3, size=1.0, exchange_ts_ms=59_000, receive_ts_ms=59_000)
    state.ingest_trade("BTC", side="A", price=101.1, size=2.0, exchange_ts_ms=20_000, receive_ts_ms=20_000)

    snap = state.snapshot("BTC", decision_ts_ms=61_000)

    assert snap.features["return_30s"] > 0
    assert snap.features["return_60s"] > snap.features["return_30s"]
    assert snap.features["realized_vol_30s"] is not None
    assert snap.features["trade_count_5s"] == 1
    assert snap.features["trade_count_30s"] == 1
    assert snap.features["trade_count_60s"] == 2
    assert snap.features["flow_imbalance_5s"] == 1.0
    assert snap.features["flow_imbalance_60s"] < 0
