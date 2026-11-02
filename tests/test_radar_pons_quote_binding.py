from dataclasses import replace

from sniper.research.memecoin_opportunity.engine import (
    _completion_time_is_valid,
    _post_request_evm_risk,
)
from sniper.research.memecoin_opportunity.pons_4663 import PONS_WETH
from sniper.research.memecoin_opportunity.state import LaunchState
from sniper.research.memecoin_opportunity.types import LifecycleState, VenueId


def _state(**changes):
    state = LaunchState(
        launch_id="pons-launch",
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
        token_id="0x1111111111111111111111111111111111111111",
        curve_id=None,
        creator_id="0x3333333333333333333333333333333333333333",
        launched_at="2026-08-11T00:00:00Z",
        observed_at="2026-08-11T00:00:20Z",
        lifecycle=LifecycleState.ACTIVE_POOL,
        event_count=12,
        buy_count=11,
        sell_count=0,
        capital_in_atomic=1_000,
        capital_out_atomic=0,
        unique_buyers=("a", "b"),
        buyer_token_balances=(("a", 10), ("b", 10)),
        creator_sale_count=0,
        virtual_token_reserves=None,
        virtual_quote_reserves=None,
        real_token_reserves=None,
        real_quote_reserves=None,
        initial_real_token_reserves=None,
        progress_bps=None,
        blocker_codes=(),
        quote_mint=PONS_WETH,
        pool_id="0x2222222222222222222222222222222222222222",
        v3_token_is_token0=True,
        v3_sqrt_price_x96=2**96,
        v3_liquidity=1_000_000,
        v3_tick=0,
    )
    return replace(state, **changes)


def test_a_normal_trade_after_the_request_is_safe():
    requested = _state()
    current = _state(
        observed_at="2026-08-11T00:00:20.060000Z",
        event_count=13,
        buy_count=12,
        capital_in_atomic=1_001,
    )
    assert _post_request_evm_risk(current, requested) is False


def test_a_creator_sale_after_the_request_rejects():
    assert _post_request_evm_risk(
        _state(creator_sale_count=1), _state(),
    ) is True


def test_a_route_change_after_the_request_rejects():
    assert _post_request_evm_risk(
        _state(pool_id="0x4444444444444444444444444444444444444444"),
        _state(),
    ) is True


def test_a_material_lp_loss_after_the_request_rejects():
    assert _post_request_evm_risk(
        _state(v3_liquidity=940_000), _state(),
    ) is True


def test_an_over_age_completion_rejects():
    assert _completion_time_is_valid(
        completed_at="2026-08-11T00:00:20.100000Z",
        requested_at="2026-08-11T00:00:20Z",
        observed_at="2026-08-11T00:00:21.101000Z",
        max_age_ms=1_000,
    ) is False
