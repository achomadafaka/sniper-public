from sniper.research.memecoin_opportunity.directional import (
    CONFIRMATION_V3_RULES,
    CONFIRMATION_V5_RULES,
    CONFIRMATION_V6_RULES,
    EXTENDED_HORIZON_RULES,
    CREATOR_PRIOR_RULES,
    CONFIRMATION_V4_RULES,
    FORWARD_CANDIDATE_RULES,
    RETIRED_CANDIDATE_RULES,
    RadarZone,
    confirmation_v3_labels,
    evaluate_forward_arm,
    evaluate_confirmation_arms,
    evaluate_confirmation_v3,
    evaluate_confirmation_v5,
    evaluate_confirmation_v6,
    evaluate_venue_confirmation,
    evaluate_creator_prior_arm,
    evaluate_level1,
)
from sniper.research.memecoin_opportunity.state import (
    PUMP_SOL_QUOTE_MINT,
    PUMP_USDC_QUOTE_MINT,
)


def _confirmation_current(**overrides):
    values = {
        "venue_id": "pumpfun_solana",
        "eligible": True,
        "capital_velocity_atomic_per_s": 600_000_000,
        "buyer_velocity_per_s_micros": 600_000,
        "effective_buyer_hhi_bps": 1_500,
        "effective_buyer_count": 7,
        "new_eligible_buyers": 2,
        "creator_sale_since_prior": False,
        "local_exit_exists": True,
        "remote_entry_quote_ok": True,
        "remote_sell_simulation_ok": True,
        "immediate_round_trip_return_bps": -900,
        "effective_fee_bps": 125,
        "progress_bps": 2_500,
        "launch_age_ms": 20_000,
        "sell_pressure_bps": 2_000,
    }
    values.update(overrides)
    return values


def test_confirmation_microscope_emits_paired_truth_and_shadow_arms():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }

    arms = evaluate_confirmation_arms(_confirmation_current(), prior=prior)

    assert [arm.rule_version for arm in arms] == [
        "pump-confirmation-truth-v2",
        "pump-confirmation-independent-v1",
        "pump-confirmation-clean-v1",
        "pump-confirmation-low-friction-v1",
    ]


def test_confirmation_shadow_conditions_fail_independently():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }

    arms = evaluate_confirmation_arms(
        _confirmation_current(
            new_eligible_buyers=1,
            sell_pressure_bps=3_000,
            immediate_round_trip_return_bps=-1_100,
        ),
        prior=prior,
    )

    assert [arm.rule_version for arm in arms] == ["pump-confirmation-truth-v2"]


def test_confirmation_v3_emits_one_receipt_with_immutable_condition_labels():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    current = _confirmation_current()

    verdict = evaluate_confirmation_v3(current, prior=prior)
    labels = confirmation_v3_labels(current, prior=prior)

    assert verdict is not None
    assert verdict.rule_version == "pump-confirmation-truth-v3"
    assert verdict.would_fire is True
    assert labels == {
        "confirmation_independent_pass": True,
        "confirmation_clean_pass": True,
        "confirmation_low_friction_pass": True,
    }


def test_confirmation_v3_labels_fail_closed_without_required_state():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }

    labels = confirmation_v3_labels(
        _confirmation_current(
            effective_buyer_hhi_bps=None,
            sell_pressure_bps=None,
            creator_sale_since_prior=True,
        ),
        prior=prior,
    )

    assert labels == {
        "confirmation_independent_pass": True,
        "confirmation_clean_pass": False,
        "confirmation_low_friction_pass": True,
    }


def test_bags_and_pons_use_separate_confirmation_rules():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    common = dict(
        effective_buyer_count=10,
        new_eligible_buyers=2,
        observed_pair_return_bps=-450,
    )

    bags = evaluate_venue_confirmation(
        _confirmation_current(
            venue_id="bags_robinhood_4663",
            launch_mode=None,
            **common,
        ),
        prior=prior,
    )
    pons = evaluate_venue_confirmation(
        _confirmation_current(
            venue_id="pons_v1_4663",
            launch_mode=None,
            progress_bps=None,
            lifecycle="active_pool",
            **common,
        ),
        prior=prior,
    )

    assert bags is not None
    assert bags.rule_version == "bags-confirmation-v1"
    assert pons is not None
    assert pons.rule_version == "pons-confirmation-v1"


def test_venue_confirmation_keeps_common_hypothesis_and_readiness_fail_closed():
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    common = dict(
        effective_buyer_count=10,
        new_eligible_buyers=2,
        observed_pair_return_bps=-450,
    )

    assert evaluate_venue_confirmation(
        _confirmation_current(
            venue_id="bags_robinhood_4663", progress_bps=1_999, **common
        ),
        prior=prior,
    ) is None
    assert evaluate_venue_confirmation(
        _confirmation_current(
            venue_id="pons_v1_4663", progress_bps=None,
            lifecycle="pre_migration", **common,
        ),
        prior=prior,
    ) is None
    assert evaluate_venue_confirmation(
        _confirmation_current(
            venue_id="pons_v1_4663", progress_bps=None,
            lifecycle="active_pool", observed_pair_return_bps=-501,
            effective_buyer_count=10, new_eligible_buyers=2,
        ),
        prior=prior,
    ) is None


def _state(**overrides):
    values = dict(
        eligible=True,
        blocker_codes=(),
        capital_velocity_atomic_per_s=100,
        buyer_velocity_per_s_micros=100_000,
        effective_buyer_hhi_bps=2_000,
        creator_sale_since_prior=False,
        local_exit_exists=True,
        immediate_round_trip_return_bps=-500,
        remote_entry_quote_ok=False,
        remote_sell_simulation_ok=False,
        data_quality_bps=10_000,
        venue_id="pumpfun_solana",
        launch_age_ms=5_000,
        progress_bps=2_500,
        effective_buyer_count=6,
        quote_mint=PUMP_SOL_QUOTE_MINT,
    )
    values.update(overrides)
    return values


def test_first_eligible_landmark_tracks_but_cannot_invent_approach_velocity():
    result = evaluate_level1(_state(), prior=None)
    assert result.zone is RadarZone.TRACKING
    assert "MISSING_PRIOR_INTERVAL" in result.reason_codes


def test_favorable_kinetics_and_execution_move_inward_by_transparent_rules():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    approaching = evaluate_level1(_state(), prior=prior)
    assert approaching.zone is RadarZone.INNER
    assert approaching.rule_version == "level1-v2"

    center = evaluate_level1(
        _state(remote_entry_quote_ok=True, remote_sell_simulation_ok=True),
        prior=prior,
    )
    assert center.zone is RadarZone.CENTER
    assert center.would_fire is True
    assert center.truth_label == "EXPLORATORY — EDGE NOT YET ESTABLISHED"


def test_creator_sale_concentration_or_route_loss_moves_dot_outward():
    prior = _state(
        effective_buyer_hhi_bps=2_000,
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
    )
    creator_sale = evaluate_level1(_state(creator_sale_since_prior=True), prior=prior)
    assert creator_sale.zone is RadarZone.APPROACHING
    assert "CREATOR_LINKED_SALE" in creator_sale.reason_codes

    concentrated = evaluate_level1(_state(effective_buyer_hhi_bps=2_500), prior=prior)
    assert concentrated.zone is RadarZone.APPROACHING
    assert "CONCENTRATION_WORSENED" in concentrated.reason_codes

    no_route = evaluate_level1(_state(local_exit_exists=False), prior=prior)
    assert no_route.zone is RadarZone.TRACKING


def test_ineligible_or_gapped_state_is_outer_and_never_fires():
    result = evaluate_level1(
        _state(eligible=False, blocker_codes=("EVENT_GAP",)),
        prior=_state(),
    )
    assert result.zone is RadarZone.OUTER
    assert result.would_fire is False
    assert "EVENT_GAP" in result.reason_codes


def test_remote_freshness_blocker_is_preserved_at_inner_gate():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )

    result = evaluate_level1(
        _state(remote_quote_blocker_code="REMOTE_QUOTE_RESULT_STALE"),
        prior=prior,
    )

    assert result.zone is RadarZone.INNER
    assert "REMOTE_QUOTE_RESULT_STALE" in result.reason_codes


def test_forward_arm_classifies_early_pump_ignition():
    prior = _state(
        capital_velocity_atomic_per_s=400_000_000,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    current = _state(
        capital_velocity_atomic_per_s=500_000_000,
        immediate_round_trip_return_bps=-1_200,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
    )

    result = evaluate_forward_arm(current, prior=prior)

    assert result is not None
    assert result.zone is RadarZone.CENTER
    assert result.would_fire is True
    assert result.rule_version == "pump-ignition-v1"


def test_ignition_v1_does_not_pool_raw_usdc_velocity_with_sol():
    prior = _state(
        quote_mint=PUMP_USDC_QUOTE_MINT,
        capital_velocity_atomic_per_s=400_000_000,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    current = _state(
        quote_mint=PUMP_USDC_QUOTE_MINT,
        capital_velocity_atomic_per_s=500_000_000,
        immediate_round_trip_return_bps=-1_200,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
    )

    assert evaluate_forward_arm(current, prior=prior) is None


def test_forward_arm_classifies_low_friction_clean_start_probe():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=4_000,
    )
    current = _state(
        progress_bps=300,
        effective_buyer_count=3,
        effective_buyer_hhi_bps=3_300,
        immediate_round_trip_return_bps=-400,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
    )

    result = evaluate_forward_arm(current, prior=prior)

    assert result is not None
    assert result.rule_version == "pump-clean-start-v1"


def test_two_buyer_curve_break_probe_no_longer_fires():
    """Retired: its drag band is unreachable once drag means modeled cost."""
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=9_500,
    )
    current = _state(
        progress_bps=3_200,
        effective_buyer_count=2,
        effective_buyer_hhi_bps=9_000,
        immediate_round_trip_return_bps=-2_000,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
    )

    assert evaluate_forward_arm(current, prior=prior) is None


def test_forward_arm_classifies_later_buyer_confirmation():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    current = _state(
        launch_age_ms=10_001,
        capital_velocity_atomic_per_s=100,
        effective_buyer_count=5,
        immediate_round_trip_return_bps=-800,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
    )

    result = evaluate_forward_arm(current, prior=prior)

    assert result is not None
    assert result.rule_version == "pump-confirmation-v1"


def test_forward_arm_rejects_wrong_venue_or_low_progress():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    executable = dict(remote_entry_quote_ok=True, remote_sell_simulation_ok=True)

    assert evaluate_forward_arm(
        _state(venue_id="bags_solana", **executable), prior=prior
    ) is None
    assert evaluate_forward_arm(
        _state(progress_bps=1_999, effective_buyer_hhi_bps=8_000, **executable), prior=prior
    ) is None


def test_forward_arm_rejects_uneconomic_or_inconsistent_quote_pairs():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    remote = dict(remote_entry_quote_ok=True, remote_sell_simulation_ok=True)

    assert evaluate_forward_arm(
        _state(immediate_round_trip_return_bps=-1_201, **remote), prior=prior
    ) is None
    assert evaluate_forward_arm(
        _state(immediate_round_trip_return_bps=1, **remote), prior=prior
    ) is None


def test_forward_arm_requires_shared_level1_conditions():
    prior = _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )
    remote = dict(remote_entry_quote_ok=True, remote_sell_simulation_ok=True)

    assert evaluate_forward_arm(_state(local_exit_exists=False, **remote), prior=prior) is None
    assert evaluate_forward_arm(
        _state(effective_buyer_hhi_bps=3_000, **remote), prior=prior
    ) is None
    assert evaluate_forward_arm(
        _state(creator_sale_since_prior=True, **remote), prior=prior
    ) is None
    assert evaluate_forward_arm(
        _state(buyer_velocity_per_s_micros=50_000, **remote), prior=prior
    ) is None


def test_curve_break_arm_is_retired_under_modeled_cost_semantics():
    """`pump-curve-break-v1` gated on a paced price move; modeled cost can never enter it."""
    current = {
        "venue_id": "pumpfun_solana",
        "eligible": True,
        "capital_velocity_atomic_per_s": 600_000_000,
        "buyer_velocity_per_s_micros": 600_000,
        "local_exit_exists": True,
        "effective_buyer_hhi_bps": 1_500,
        "effective_buyer_count": 2,
        "creator_sale_since_prior": False,
        "remote_entry_quote_ok": True,
        "remote_sell_simulation_ok": True,
        "progress_bps": 3_000,
        "launch_age_ms": 5_000,
        "immediate_round_trip_return_bps": -2_000,
        "quote_mint": PUMP_SOL_QUOTE_MINT,
    }
    prior = {
        "capital_velocity_atomic_per_s": 100_000_000,
        "buyer_velocity_per_s_micros": 100_000,
        "effective_buyer_hhi_bps": 2_000,
    }

    verdict = evaluate_forward_arm(current, prior=prior)

    assert verdict is None
    assert "pump-curve-break-v1" not in FORWARD_CANDIDATE_RULES


def test_confirmation_arms_read_eligible_buyer_fields_only():
    """No decision may read a field that claims uncomputed entity independence."""
    current = {
        "venue_id": "pumpfun_solana",
        "eligible": True,
        "capital_velocity_atomic_per_s": 600_000_000,
        "buyer_velocity_per_s_micros": 600_000,
        "local_exit_exists": True,
        "effective_buyer_hhi_bps": 1_500,
        "effective_buyer_count": 6,
        "new_eligible_buyers": 3,
        "creator_sale_since_prior": False,
        "remote_entry_quote_ok": True,
        "remote_sell_simulation_ok": True,
        "progress_bps": 3_000,
        "launch_age_ms": 20_000,
        "sell_pressure_bps": 1_000,
        "effective_fee_bps": 125,
        "immediate_round_trip_return_bps": -250,
    }
    prior = {
        "capital_velocity_atomic_per_s": 100_000_000,
        "buyer_velocity_per_s_micros": 100_000,
        "effective_buyer_hhi_bps": 2_000,
    }

    assert evaluate_confirmation_v3(current, prior=prior) is not None
    assert confirmation_v3_labels(current, prior=prior) == {
        "confirmation_independent_pass": True,
        "confirmation_clean_pass": True,
        "confirmation_low_friction_pass": True,
    }


def test_the_arm_registry_is_frozen():
    """Nine arms have been tested against the control and none separated.

    Each additional arm costs multiple-testing burden and slows every verdict: a
    matched-control read needs ~25 pairs per arm and they accrue at a few an hour.
    Until one of the open levers lands -- exit policy on the retained ladder, or the
    creator prior -- no new firing arm should appear.

    This test is the gate. Changing it is allowed; changing it silently is not.
    Adding an arm means recording the decision and accepting that the matched-control
    clock restarts for it.
    """
    assert FORWARD_CANDIDATE_RULES == (
        "pump-clean-start-v1",
        "pump-ignition-v1",
        "pump-confirmation-v1",
    )
    assert CONFIRMATION_V3_RULES == (
        "pump-confirmation-truth-v3",
        "pump-confirmation-delayed-v2",
    )
    assert CONFIRMATION_V4_RULES == (
        "pump-confirmation-truth-v4",
        "route-control-v2",
    )
    assert RETIRED_CANDIDATE_RULES == ("pump-curve-break-v1",)
    # Registry change recorded 2026-07-30: `pump-creator-prior-v1` added on operator
    # approval, on evidence RETRACTED the same day for target leakage. The retrospective
    # split ordered priors by alert time, so outcomes that resolved after the decision
    # still counted; requiring resolution first leaves +392 bps at t=0.70 -- no effect.
    # The arm's forward store is causal, so it was an untested hypothesis with a zero
    # clock, NOT an evidence-backed arm.
    # Registry change recorded 2026-08-07: RETIRED (operator approved). The clean-
    # universe rescan confirmed the null (t=0.5, n=196 covered), and the slot diversion
    # consumed ~half of the control supply while structurally excluding positive-prior
    # creators from the control cohort. The engine no longer produces the verdict; the
    # rule stays registered so its history keeps its identity, and the creator_prior_*
    # features keep accruing as decision-time facts.
    assert CREATOR_PRIOR_RULES == ("pump-creator-prior-v1",)
    # Registry change recorded 2026-07-30: `pump-confirmation-truth-v5` REPLACES v4 as
    # the firing confirmation arm, raising the breadth gate from 5 to 10 eligible
    # buyers. v4 is not mutated -- it keeps its history and stops firing -- because
    # changing a live rule in place silently redefines what its accrued evidence meant.
    # v5 starts a fresh matched-control clock and did NOT clear the gate retrospectively
    # (26 pairs, +206, CI [-324,+807], decaying to +49 as the band widens).
    assert CONFIRMATION_V5_RULES == ("pump-confirmation-truth-v5",)
    live_firing_arms = (
        set(FORWARD_CANDIDATE_RULES)
        | set(CREATOR_PRIOR_RULES)
        | set(CONFIRMATION_V5_RULES)
    )
    assert len(live_firing_arms) == 5
    assert "pump-confirmation-truth-v4" not in live_firing_arms
    assert "pump-curve-break-v1" not in live_firing_arms


def _creator_prior_current(**overrides):
    values = _state(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        remote_entry_quote_ok=True,
        remote_sell_simulation_ok=True,
        immediate_round_trip_return_bps=-250,
        creator_prior_samples=3,
        creator_prior_mean_bps=900,
    )
    values.update(overrides)
    return values


def _rising_prior():
    return _state(
        capital_velocity_atomic_per_s=50,
        buyer_velocity_per_s_micros=50_000,
        effective_buyer_hhi_bps=2_500,
    )


def test_creator_prior_arm_fires_only_on_a_positive_prior_record():
    """Preregistered exactly as measured: >=1 scored prior outcome and a positive mean.

    No threshold tuning -- the split was discovered on the sign of the mean and is
    forward-tested on the sign of the mean.
    """
    fires = evaluate_creator_prior_arm(_creator_prior_current(), prior=_rising_prior())
    assert fires is not None
    assert fires.rule_version == "pump-creator-prior-v1"
    assert fires.zone is RadarZone.CENTER

    assert evaluate_creator_prior_arm(
        _creator_prior_current(creator_prior_mean_bps=-1), prior=_rising_prior()
    ) is None
    assert evaluate_creator_prior_arm(
        _creator_prior_current(creator_prior_mean_bps=0), prior=_rising_prior()
    ) is None
    # no record at all is not a positive record
    assert evaluate_creator_prior_arm(
        _creator_prior_current(creator_prior_samples=0, creator_prior_mean_bps=None),
        prior=_rising_prior(),
    ) is None


def test_creator_prior_arm_still_requires_a_verified_route():
    """The prior is a filter on top of the permissive base, never a bypass of it."""
    assert evaluate_creator_prior_arm(
        _creator_prior_current(remote_sell_simulation_ok=False), prior=_rising_prior()
    ) is None
    assert evaluate_creator_prior_arm(
        _creator_prior_current(eligible=False), prior=_rising_prior()
    ) is None
    assert evaluate_creator_prior_arm(_creator_prior_current(), prior=None) is None


def test_creator_prior_arm_is_in_the_frozen_registry():
    assert CREATOR_PRIOR_RULES == ("pump-creator-prior-v1",)


def test_confirmation_v5_requires_ten_eligible_buyers_and_replaces_v4():
    """Preregistered forward experiment: raise the breadth gate from 5 to 10.

    Descriptively, buyers >=10 ran +660 against -259 in the permissive control
    population (t=+3.68), while curve size alone did not separate (t=+1.50). But
    restricting v4 retrospectively gave only 26 matched pairs at +206 with a creator
    interval of [-324,+807], decaying to +49 as the band widens. It does NOT clear the
    gate, so this is a new hypothesis on a fresh clock -- a new version, never a mutation
    of v4, whose evidence would otherwise be silently redefined.
    """
    prior = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    nine = _confirmation_current(effective_buyer_count=9)
    ten = _confirmation_current(effective_buyer_count=10)

    assert evaluate_confirmation_v5(nine, prior=prior) is None
    fired = evaluate_confirmation_v5(ten, prior=prior)
    assert fired is not None
    assert fired.rule_version == "pump-confirmation-truth-v5"

    # v4 still describes its own history exactly; raising its bar in place would have
    # rewritten what its accrued evidence means.
    assert evaluate_confirmation_v3(nine, prior=prior) is not None
    assert CONFIRMATION_V5_RULES == ("pump-confirmation-truth-v5",)


def _v6_current(**overrides):
    values = _confirmation_current(
        effective_buyer_count=10,
        launch_mode="standard",
        observed_pair_return_bps=-400,
    )
    values.update(overrides)
    return values


_V6_PRIOR = {
    "capital_velocity_atomic_per_s": 500_000_000,
    "buyer_velocity_per_s_micros": 400_000,
    "effective_buyer_hhi_bps": 1_800,
}


def test_confirmation_v6_rejects_the_cashback_tier():
    """The cheap-fee belief is refuted by the measured route.

    The local model prices cashback at -191 bps against standard at -250, so it reads as
    the cheaper tier. Over 5,026 alerts carrying both numbers the MEASURED median round
    trip is -456 bps for cashback and -448 for standard. Cashback is not cheaper. It was
    60% of v5 fires and carried the loss under every fill contract.
    """
    assert evaluate_confirmation_v6(_v6_current(), prior=_V6_PRIOR) is not None
    assert evaluate_confirmation_v6(
        _v6_current(launch_mode="cashback"), prior=_V6_PRIOR
    ) is None
    assert evaluate_confirmation_v6(
        _v6_current(launch_mode="mayhem"), prior=_V6_PRIOR
    ) is None


def test_confirmation_v6_refuses_a_position_that_starts_below_the_stop():
    """A solvency floor, not a forecast.

    The decision-time round trip does not predict the 5-second route mark (r=-0.03 on
    standard), so this band buys no edge. It refuses an entry that already sits below the
    -800 bps stop, which is what made 26 of 51 v1 results stop on their first quote.
    """
    assert evaluate_confirmation_v6(
        _v6_current(observed_pair_return_bps=-500), prior=_V6_PRIOR
    ) is not None
    assert evaluate_confirmation_v6(
        _v6_current(observed_pair_return_bps=-501), prior=_V6_PRIOR
    ) is None
    assert evaluate_confirmation_v6(
        _v6_current(observed_pair_return_bps=None), prior=_V6_PRIOR
    ) is None


def test_confirmation_v6_reads_the_measured_pair_not_the_modeled_cost():
    """`immediate_round_trip_return_bps` carries the model on the v4 receipt path.

    The model prices the round trip 198 to 1,566 bps too cheap. A permissive modeled cost
    must not admit a launch whose measured round trip is below the floor.
    """
    assert evaluate_confirmation_v6(
        _v6_current(immediate_round_trip_return_bps=-100, observed_pair_return_bps=-900),
        prior=_V6_PRIOR,
    ) is None


def test_confirmation_v5_keeps_its_history_and_stops_firing():
    assert CONFIRMATION_V6_RULES == ("pump-confirmation-truth-v6",)
    # v5 still describes its own cohort exactly.
    assert evaluate_confirmation_v5(_v6_current(), prior=_V6_PRIOR) is not None
    assert set(CONFIRMATION_V5_RULES) & set(CONFIRMATION_V6_RULES) == set()


def test_extended_horizon_rules_are_one_list():
    """`outcome_tracker` and `retention` drifted; v5 was in one copy only."""
    from sniper.research.memecoin_opportunity import outcome_tracker, retention

    assert outcome_tracker._EXTENDED_RULES == EXTENDED_HORIZON_RULES
    assert set(CONFIRMATION_V5_RULES + CONFIRMATION_V6_RULES) <= set(EXTENDED_HORIZON_RULES)
    assert retention.EXTENDED_HORIZON_RULES == EXTENDED_HORIZON_RULES


def test_matched_control_sampling_is_deterministic_over_real_alert_ids():
    """`_paper_cohort_eligible` parses `alert_id` as hex; it is a sha256 digest.

    Only the control branch reaches that parse, so nothing else covers it. The sample
    must also land near 1-in-5 or the control accrues at the wrong rate.
    """
    from sniper.research.memecoin_opportunity.engine import _paper_cohort_eligible
    from sniper.research.memecoin_opportunity.paper_adapter import (
        PAPER_CONTROL_RULE_VERSION,
        PAPER_CONTROL_SAMPLE_DENOMINATOR,
        PAPER_RULE_VERSION,
    )
    from sniper.research.memecoin_opportunity.projection import forward_alert_id

    in_band = {"launch_mode": "standard", "observed_pair_return_bps": -400}
    ids = [
        forward_alert_id(f"launch-{index}", PAPER_CONTROL_RULE_VERSION, 25_000_000)
        for index in range(500)
    ]
    sampled = [i for i in ids if _paper_cohort_eligible(PAPER_CONTROL_RULE_VERSION, in_band, i)]
    assert 60 < len(sampled) < 140  # 1-in-5 of 500, binomial slack
    # Deterministic: the same id decides the same way every time.
    assert all(
        _paper_cohort_eligible(PAPER_CONTROL_RULE_VERSION, in_band, i) for i in sampled
    )
    # The control never joins outside v6's own band, whatever the sample says.
    assert not any(
        _paper_cohort_eligible(
            PAPER_CONTROL_RULE_VERSION, {**in_band, "launch_mode": "cashback"}, i
        )
        for i in sampled
    )
    assert not any(
        _paper_cohort_eligible(
            PAPER_CONTROL_RULE_VERSION, {**in_band, "observed_pair_return_bps": -501}, i
        )
        for i in sampled
    )
    # The arm is never sampled.
    assert all(_paper_cohort_eligible(PAPER_RULE_VERSION, in_band, i) for i in ids)
    # Any other rule stays out.
    assert not _paper_cohort_eligible("pump-ignition-v1", in_band, ids[0])
    assert PAPER_CONTROL_SAMPLE_DENOMINATOR == 5
