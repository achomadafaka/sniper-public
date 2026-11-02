"""Phase 1D #3 — forward paper execution + fee scenarios + settlement PnL.

Gross $10 fee-EXCLUSIVE ask walk (shares fixed regardless of fee source); documented fee computed
per-level on the frozen fill; PnL per fee track. Never infer the ask from 1 - opposite; require complete
visible-depth execution.
"""
from sniper.algos.polymarket_crypto5m import forward_paper as FP


def test_walk_single_level_full_fill():
    f = FP.walk_ask_ladder([[0.50, 100]], gross_usdc=10.0, min_order_size=5)
    assert f["filled"] is True
    assert abs(f["shares"] - 20.0) < 1e-9 and abs(f["gross_trade_cost"] - 10.0) < 1e-9
    assert f["best_ask"] == 0.50 and abs(f["depth_weighted_fill_price"] - 0.50) < 1e-9
    assert f["levels_consumed"] == 1 and abs(f["slippage_vs_best_ask"]) < 1e-9


def test_walk_insufficient_depth_no_fill():
    f = FP.walk_ask_ladder([[0.50, 10]], gross_usdc=10.0, min_order_size=5)   # only $5 of depth
    assert f["filled"] is False and f["reason"] == "insufficient_depth"


def test_walk_below_min_order():
    f = FP.walk_ask_ladder([[0.99, 1000]], gross_usdc=2.0, min_order_size=5)   # ~2.02 shares < 5
    assert f["filled"] is False and f["reason"] == "below_min_order"


def test_walk_multi_level_ascending_and_slippage():
    f = FP.walk_ask_ladder([[0.50, 10], [0.55, 100]], gross_usdc=10.0, min_order_size=5)
    assert f["filled"] is True and f["levels_consumed"] == 2
    assert abs(f["gross_trade_cost"] - 10.0) < 1e-9
    assert f["slippage_vs_best_ask"] > 0                       # paid above best ask walking up
    # ask ordering independent
    g = FP.walk_ask_ladder([[0.55, 100], [0.50, 10]], gross_usdc=10.0, min_order_size=5)
    assert abs(g["depth_weighted_fill_price"] - f["depth_weighted_fill_price"]) < 1e-9


def test_documented_fee_per_level_five_dp():
    levels = [{"price": 0.50, "shares": 20.0}]
    assert FP.documented_fee(levels, rate=0.07) == round(20.0 * 0.07 * 0.5 * 0.5, 5)   # 0.35


def test_fee_scenarios():
    levels = [{"price": 0.50, "shares": 20.0}]
    sc = FP.fee_scenarios(levels)
    assert sc["F0_gross"] == 0.0
    assert sc["F1_documented"] == 0.35


def test_pnl_win_and_lose():
    assert abs(FP.pnl(shares=20.0, gross_trade_cost=10.0, fee=0.35, won=True) - 9.65) < 1e-9
    assert abs(FP.pnl(shares=20.0, gross_trade_cost=10.0, fee=0.35, won=False) - (-10.35)) < 1e-9


def test_settlement_won_maps_from_winning_outcome():
    assert FP.selected_won("Up", "Up") is True
    assert FP.selected_won("Up", "Down") is False
    assert FP.selected_won("Down", None) is None              # unresolved -> unknown
