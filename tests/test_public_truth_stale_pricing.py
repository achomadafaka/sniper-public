import pytest

from sniper.analysis.public_truth_stale.pricing import (
    InsufficientDepthError,
    depth_weighted_buy_price,
    net_edge,
)


def test_depth_weighted_buy_price_walks_ask_depth_for_dollar_stake():
    asks = [
        {"price": "0.50", "size": "10"},
        {"price": "0.75", "size": "30"},
    ]

    fill = depth_weighted_buy_price(asks, stake_usdc=20.0)

    assert fill.cost_usdc == pytest.approx(20.0)
    assert fill.shares == pytest.approx(30.0)
    assert fill.avg_price == pytest.approx(20.0 / 30.0)
    assert fill.levels_consumed == 2
    assert fill.unfilled_usdc == pytest.approx(0.0)


def test_depth_weighted_buy_price_rejects_insufficient_depth():
    asks = [{"price": "0.50", "size": "10"}]

    with pytest.raises(InsufficientDepthError):
        depth_weighted_buy_price(asks, stake_usdc=20.0)


def test_depth_weighted_buy_price_sorts_asks_by_price():
    asks = [
        {"price": "0.75", "size": "30"},
        {"price": "0.50", "size": "10"},
    ]

    fill = depth_weighted_buy_price(asks, stake_usdc=20.0)

    assert fill.shares == pytest.approx(30.0)
    assert fill.avg_price == pytest.approx(20.0 / 30.0)


def test_net_edge_uses_expected_payout_minus_entry_and_costs():
    assert net_edge(
        expected_payout=1.0,
        entry_price=0.91,
        modeled_cost_per_share=0.01,
    ) == pytest.approx(0.08)
