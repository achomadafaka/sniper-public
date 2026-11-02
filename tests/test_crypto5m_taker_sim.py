"""Deterministic FOK taker-fill estimator."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import taker_sim as T


def test_single_level_complete_fill_zero_fee():
    out = T.estimate_taker_buy(asks=[["0.50", "1000"]], fee_rate=0.0, cash_budget=10.0,
                               min_order_size=5.0)
    assert out["filled"] is True
    assert abs(out["shares"] - 20.0) < 1e-9            # $10 / $0.50
    assert abs(out["total_cash_cost"] - 10.0) < 1e-9
    assert out["depth_weighted_fill_price"] == 0.50 and out["slippage_vs_best_ask"] == 0.0


def test_multi_level_fill_and_slippage():
    out = T.estimate_taker_buy(asks=[["0.50", "10"], ["0.55", "1000"]], fee_rate=0.0,
                               cash_budget=10.0, min_order_size=1.0)
    # level 1: 10 sh * 0.50 = $5; level 2: remaining $5 / 0.55 = 9.0909 sh
    assert out["levels_consumed"] == 2
    assert abs(out["shares"] - (10 + 5 / 0.55)) < 1e-6
    assert out["worst_ask_level"] == 0.55 and out["slippage_vs_best_ask"] > 0


def test_fee_inclusive_budget_is_respected():
    out = T.estimate_taker_buy(asks=[["0.50", "1000"]], fee_rate=0.10, cash_budget=10.0,
                               min_order_size=1.0)
    # cost_per_share = 0.50 + 0.10*0.25 = 0.525; shares = 10/0.525
    assert abs(out["total_cash_cost"] - 10.0) < 1e-9   # budget inclusive of fee
    assert abs(out["shares"] - 10.0 / 0.525) < 1e-9
    assert out["taker_fee"] > 0 and out["fee_inclusive_average_cost"] > out["depth_weighted_fill_price"]


def test_insufficient_depth_skips():
    out = T.estimate_taker_buy(asks=[["0.50", "10"]], fee_rate=0.0, cash_budget=20.0,
                               min_order_size=1.0)
    assert out["filled"] is False and out["reason"] == "insufficient_depth"


def test_below_min_order_skips():
    out = T.estimate_taker_buy(asks=[["0.90", "1000"]], fee_rate=0.0, cash_budget=1.0,
                               min_order_size=5.0)  # $1/0.90 = 1.11 sh < 5
    assert out["filled"] is False and out["reason"] == "below_min_order"


def test_rejects_stale_quarantined_closed_unknownfee():
    assert T.estimate_taker_buy(asks=[["0.5", "100"]], fee_rate=0.0, market_closed=True)["reason"] == "market_closed"
    assert T.estimate_taker_buy(asks=[["0.5", "100"]], fee_rate=0.0,
                                book_integrity_status="quarantined")["reason"] == "book_quarantined"
    assert T.estimate_taker_buy(asks=[["0.5", "100"]], fee_rate=0.0,
                                book_integrity_status="stale")["reason"] == "book_stale"
    assert T.estimate_taker_buy(asks=[["0.5", "100"]], fee_rate=None,
                                fee_known=False)["reason"] == "fee_unknown"
    assert T.estimate_taker_buy(asks=[], fee_rate=0.0)["reason"] == "insufficient_depth"


def test_result_independent_of_input_ask_order():
    asks = [["0.55", "1000"], ["0.50", "10"], ["0.53", "20"]]
    a = T.estimate_taker_buy(asks=asks, fee_rate=0.0, cash_budget=10.0, min_order_size=1.0)
    b = T.estimate_taker_buy(asks=list(reversed(asks)), fee_rate=0.0, cash_budget=10.0,
                             min_order_size=1.0)
    assert abs(a["shares"] - b["shares"]) < 1e-12 and a["best_ask"] == b["best_ask"] == 0.50
