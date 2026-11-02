import pytest

from sniper.research.memecoin_opportunity.frenzy import (
    FRENZY_MIN_SAMPLES,
    FrenzyCeiling,
)
from sniper.research.memecoin_opportunity.state import LaunchState
from sniper.research.memecoin_opportunity.types import LifecycleState, VenueId


@pytest.fixture
def make_state():
    def build(**overrides):
        values = {
            "launch_id": "launch-1",
            "venue_id": VenueId.PUMPFUN_SOLANA,
            "token_id": "token-1",
            "curve_id": "curve-1",
            "creator_id": "creator-1",
            "launched_at": "2026-08-11T00:00:00Z",
            "observed_at": "2026-08-11T00:00:05Z",
            "lifecycle": LifecycleState.PRE_MIGRATION,
            "event_count": 3,
            "buy_count": 2,
            "sell_count": 0,
            "capital_in_atomic": 1_000_000,
            "capital_out_atomic": 0,
            "unique_buyers": ("a", "b"),
            "buyer_token_balances": (("a", 10), ("b", 10)),
            "creator_sale_count": 0,
            "virtual_token_reserves": 1_000_000,
            "virtual_quote_reserves": 1_000_000,
            "real_token_reserves": 1_000_000,
            "real_quote_reserves": 1_000_000,
            "initial_real_token_reserves": 1_000_000,
            "progress_bps": 0,
            "blocker_codes": (),
        }
        values.update(overrides)
        return LaunchState(**values)

    return build


def test_liquidity_removed_fraction_is_the_reserve_drop_in_bps(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features

    prior = make_state(
        real_quote_reserves=1_000_000,
        observed_at="2026-08-11T00:00:00Z",
    )
    current = make_state(
        real_quote_reserves=940_000,
        observed_at="2026-08-11T00:00:05Z",
    )
    features = point_in_time_features(current, prior=prior)
    assert features["liquidity_removed_fraction_bps"] == 600


def test_liquidity_removed_fraction_is_none_without_a_prior_reserve(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features

    prior = make_state(
        real_quote_reserves=None,
        observed_at="2026-08-11T00:00:00Z",
    )
    current = make_state(
        real_quote_reserves=940_000,
        observed_at="2026-08-11T00:00:05Z",
    )
    features = point_in_time_features(current, prior=prior)
    assert features["liquidity_removed_fraction_bps"] is None


def test_liquidity_growth_reads_as_zero_removal(make_state):
    from sniper.research.memecoin_opportunity.state import point_in_time_features

    prior = make_state(
        real_quote_reserves=1_000_000,
        observed_at="2026-08-11T00:00:00Z",
    )
    current = make_state(
        real_quote_reserves=1_500_000,
        observed_at="2026-08-11T00:00:05Z",
    )
    assert point_in_time_features(current, prior=prior)[
        "liquidity_removed_fraction_bps"
    ] == 0


def test_frenzy_ceiling_is_none_below_the_minimum_sample():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES - 1):
        ceiling.observe("pumpfun_solana", 17_000, index)
    assert ceiling.ceiling("pumpfun_solana", 17_000) is None


def test_frenzy_ceiling_is_the_p75_of_its_own_age_bucket():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, index)
        ceiling.observe("pumpfun_solana", 33_000, 1_000_000 + index)
    low = ceiling.ceiling("pumpfun_solana", 17_400)
    high = ceiling.ceiling("pumpfun_solana", 33_400)
    assert low is not None and high is not None and high > low


def test_frenzy_ceiling_separates_venues():
    ceiling = FrenzyCeiling()
    for index in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, index)
    assert ceiling.ceiling("pons_v1_4663", 17_000) is None


def test_frenzy_ceiling_ignores_a_missing_acceleration():
    ceiling = FrenzyCeiling()
    for _ in range(FRENZY_MIN_SAMPLES):
        ceiling.observe("pumpfun_solana", 17_000, None)
    assert ceiling.ceiling("pumpfun_solana", 17_000) is None
