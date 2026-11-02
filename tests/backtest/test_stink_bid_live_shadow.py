import json

from scripts.stink_bid_live_shadow import (
    BookLevel,
    ShadowOrder,
    ShadowOrderMonitor,
    TradePrint,
    depth_ahead_for_bid,
    evaluate_shadow_fill,
    evaluate_queue_models,
    evaluate_trade_against_snapshot,
    parse_orderbook_snapshot,
    write_shadow_observation,
)


def test_depth_ahead_for_bid_counts_better_and_same_price_bids():
    bids = [
        BookLevel(price=0.43, size=10.0),
        BookLevel(price=0.42, size=15.0),
        BookLevel(price=0.41, size=20.0),
    ]

    assert depth_ahead_for_bid(bids, bid_price=0.42) == 25.0


def test_evaluate_shadow_fill_requires_trade_to_clear_queue_ahead():
    order = ShadowOrder(
        condition_id="cid",
        token_id="tok",
        bid_price=0.42,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )
    trade = TradePrint(token_id="tok", price=0.40, size=70.0, side="SELL", timestamp_sec=1)

    not_fillable = evaluate_shadow_fill(order, trade, depth_ahead=25.0)
    fillable = evaluate_shadow_fill(order, trade, depth_ahead=20.0)

    assert not_fillable.price_crossed is True
    assert not_fillable.size_qualified is True
    assert not_fillable.queue_fillable is False
    assert not_fillable.required_trade_size == 75.0
    assert fillable.queue_fillable is True


def test_parse_orderbook_snapshot_normalizes_polymarket_book_payload():
    raw = {
        "market": "cid",
        "asset_id": "tok",
        "timestamp": "1710000000",
        "hash": "bookhash",
        "bids": [{"price": "0.41", "size": "100"}],
        "asks": [{"price": "0.43", "size": "80"}],
        "min_order_size": "5",
        "tick_size": "0.01",
        "last_trade_price": "0.42",
    }

    snapshot = parse_orderbook_snapshot(raw)

    assert snapshot.condition_id == "cid"
    assert snapshot.token_id == "tok"
    assert snapshot.best_bid == 0.41
    assert snapshot.best_ask == 0.43
    assert snapshot.spread == 0.02
    assert snapshot.min_order_size == 5.0
    assert snapshot.tick_size == 0.01


def test_write_shadow_observation_emits_jsonl(tmp_path):
    path = tmp_path / "shadow.jsonl"
    order = ShadowOrder(
        condition_id="cid",
        token_id="tok",
        bid_price=0.42,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )
    trade = TradePrint(token_id="tok", price=0.40, size=80.0, side="SELL", timestamp_sec=1)
    evaluation = evaluate_shadow_fill(order, trade, depth_ahead=20.0)

    write_shadow_observation(path, order, depth_ahead=20.0, evaluation=evaluation)

    row = json.loads(path.read_text().strip())
    assert row["event_type"] == "trade_evaluation"
    assert row["condition_id"] == "cid"
    assert row["token_id"] == "tok"
    assert row["depth_ahead"] == 20.0
    assert row["queue_fillable"] is True


def test_evaluate_trade_against_snapshot_uses_latest_visible_depth():
    snapshot = parse_orderbook_snapshot(
        {
            "market": "cid",
            "asset_id": "tok",
            "timestamp": "1710000000",
            "hash": "bookhash",
            "bids": [
                {"price": "0.43", "size": "10"},
                {"price": "0.42", "size": "15"},
            ],
            "asks": [],
        }
    )
    order = ShadowOrder(
        condition_id="cid",
        token_id="tok",
        bid_price=0.42,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )
    trade = TradePrint(token_id="tok", price=0.40, size=75.0, side="SELL", timestamp_sec=1)

    evaluation = evaluate_trade_against_snapshot(order, snapshot, trade)

    assert evaluation.depth_ahead == 25.0
    assert evaluation.queue_fillable is True


def test_shadow_order_monitor_writes_trade_evaluation(tmp_path):
    order = ShadowOrder(
        condition_id="cid",
        token_id="tok",
        bid_price=0.42,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )
    snapshot = parse_orderbook_snapshot(
        {
            "market": "cid",
            "asset_id": "tok",
            "timestamp": "1710000000",
            "hash": "bookhash",
            "bids": [{"price": "0.42", "size": "20"}],
            "asks": [],
        }
    )
    monitor = ShadowOrderMonitor(order=order, output_path=tmp_path / "shadow.jsonl")
    monitor.set_snapshot(snapshot)

    evaluation = monitor.on_trade(
        TradePrint(token_id="tok", price=0.40, size=70.0, side="SELL", timestamp_sec=1)
    )

    assert evaluation is not None
    assert evaluation.queue_fillable is True
    row = json.loads((tmp_path / "shadow.jsonl").read_text().strip())
    assert row["event_type"] == "trade_evaluation"
    assert row["reason"] == "queue_fillable"
    assert row["book_hash"] == "bookhash"


def test_evaluate_queue_models_reports_fillability_sensitivity():
    order = ShadowOrder(
        condition_id="cid",
        token_id="tok",
        bid_price=0.42,
        min_fill_size=50.0,
        position_size_usdc=25.0,
    )
    trade = TradePrint(token_id="tok", price=0.40, size=60.0, side="SELL", timestamp_sec=1)

    rows = evaluate_queue_models(order, trade, depth_ahead=100.0)

    by_model = {row["queue_model"]: row for row in rows}
    assert by_model["price_cross"]["queue_fillable"] is True
    assert by_model["optimistic"]["queue_fillable"] is True
    assert by_model["midpoint"]["queue_fillable"] is False
    assert by_model["pessimistic"]["required_trade_size"] == 150.0
