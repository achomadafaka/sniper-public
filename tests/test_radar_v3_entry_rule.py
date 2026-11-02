import pytest

from sniper.research.memecoin_opportunity.directional import (
    ADAPTIVE_V3_BY_VENUE,
    evaluate_adaptive_v3,
)

PRIOR = {
    "effective_buyer_count": 8,
    "capital_velocity_atomic_per_s": 100,
    "buyer_velocity_per_s_micros": 100,
    "effective_buyer_hhi_bps": 2_000,
}
CURRENT = {
    "venue_id": "pumpfun_solana",
    "eligible": True,
    "lifecycle": "pre_migration",
    "effective_buyer_count": 12,
    "capital_velocity_atomic_per_s": 200,
    "buyer_velocity_per_s_micros": 200,
    "effective_buyer_hhi_bps": 1_500,
    "new_eligible_buyers": 4,
    "sell_pressure_bps": 1_000,
    "creator_sale_since_prior": False,
    "local_exit_exists": True,
    "remote_entry_quote_ok": True,
    "remote_sell_simulation_ok": True,
    "launch_age_ms": 20_000,
    "observed_pair_return_bps": -300,
    "liquidity_removed_fraction_bps": 100,
    "capital_acceleration_atomic_per_s2": 500,
    "frenzy_ceiling_atomic_per_s2": 900,
    "quote_age_ms": 800,
    "progress_bps": 3_000,
}


def verdict(**overrides):
    return evaluate_adaptive_v3({**CURRENT, **overrides}, prior=PRIOR)


def test_the_baseline_case_accepts():
    result = verdict()
    assert result is not None
    assert result.rule_version == ADAPTIVE_V3_BY_VENUE["pumpfun_solana"]


@pytest.mark.parametrize(
    "age_ms,accepted",
    [(14_900, False), (15_100, True), (34_900, True), (35_100, False)],
)
def test_the_age_window_is_15_to_35_seconds(age_ms, accepted):
    assert (verdict(launch_age_ms=age_ms) is not None) is accepted


def test_a_material_lp_loss_rejects():
    assert verdict(liquidity_removed_fraction_bps=500) is None


def test_a_missing_lp_feature_rejects():
    assert verdict(liquidity_removed_fraction_bps=None) is None


def test_acceleration_at_or_below_the_ceiling_accepts():
    assert verdict(capital_acceleration_atomic_per_s2=900) is not None
    assert verdict(capital_acceleration_atomic_per_s2=901) is None


def test_a_missing_ceiling_rejects():
    assert verdict(frenzy_ceiling_atomic_per_s2=None) is None


@pytest.mark.parametrize("quote_age_ms,accepted", [(1_999, True), (2_000, False)])
def test_the_quote_must_be_under_two_seconds(quote_age_ms, accepted):
    assert (verdict(quote_age_ms=quote_age_ms) is not None) is accepted


def test_no_prior_observation_rejects():
    assert evaluate_adaptive_v3(CURRENT, prior=None) is None


def test_the_solvency_floor_still_applies():
    assert verdict(observed_pair_return_bps=-501) is None


def test_a_creator_sale_rejects():
    assert verdict(creator_sale_since_prior=True) is None


def test_rising_concentration_rejects():
    assert verdict(effective_buyer_hhi_bps=2_500) is None


@pytest.mark.parametrize(
    "venue,lifecycle,accepted",
    [
        ("pumpfun_solana", "pre_migration", True),
        # bags_robinhood_4663 retired 2026-08-19 (HISTORICAL_ONLY)
        ("pons_v1_4663", "active_pool", True),
    ],
)
def test_every_scoreable_venue_uses_the_same_predicate(venue, lifecycle, accepted):
    result = verdict(venue_id=venue, lifecycle=lifecycle)
    assert (result is not None) is accepted
