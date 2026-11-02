import json
from dataclasses import replace

import pytest

from sniper.services.memecoin_v6_engine import (
    POLICY_ID, ActionIntent, BreakEpisode, EvidenceIdentity, RouteAbsenceState, RuleStatus,
    V6Action, V6PositionState, canonical_json, make_observation, policy_document,
    semantic_observation_id, state_document, state_from_document, step,
)
from sniper.services.memecoin_v6_position_policy import V6State


def _state(**changes):
    base = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000,
    )
    return replace(base, **changes)


def complete_payload(**changes):
    base = {
        "executable_price": 0.00042,
        "chain_vol_p90": 0.18,
        "route_degraded": False,
        "source_wallet_exited": False,
        "wallet_tier_adjustment": 0,
        "is_break": False,
        "held_s": 60.0,
        "arrival_rate": 0.02,
        "buy_share": 0.60,
        "realized_vol": 0.05,
        "prior_low": 0.00040,
        "price": 0.00042,
        "net": 2.0,
    }
    base.update(changes)
    return base


def _observation(observation_id="o", **changes):
    return make_observation(
        observation_id=observation_id,
        observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana",
        payload=complete_payload(**changes),
    )


def _market_observation(market_id, *, observed_at="2026-07-26T12:00:00+00:00", **changes):
    evidence = EvidenceIdentity(market=market_id, quote="quote-1")
    return make_observation(
        observation_id=semantic_observation_id("p1", evidence), observed_at=observed_at,
        network_id="solana", payload=complete_payload(**changes), evidence=evidence,
    )


def test_missing_optional_capture_only_abstains_affected_rules():
    decision = step(_state(), make_observation(
        observation_id="o1", observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana",
        payload=complete_payload(executable_price=None, chain_vol_p90=None),
    ))
    assert decision.action is V6Action.HOLD
    assert decision.fraction_ppm == 0
    assert decision.reason_codes == ("volatility:insufficient",)
    by_rule = {result.rule_id: result for result in decision.rule_results}
    assert by_rule["catastrophe"].status is RuleStatus.INSUFFICIENT
    assert by_rule["staged_break"].status is RuleStatus.NOT_TRIGGERED


def test_missing_chain_p90_does_not_block_trusted_lp_loss():
    decision = step(_state(), _observation(
        chain_vol_p90=None, executable_price=None, depth_pull_flag=True,
        tape_complete=True, depth_provenance="trusted", capture_version="v6-direct-v1",
        liquidity_removed_fraction=0.40,
    ))
    assert decision.action is V6Action.EXIT_FULL
    assert "lp_loss" in decision.reason_codes
    assert "volatility:insufficient" in decision.reason_codes


def test_incremental_capture_can_authorize_trusted_lp_loss():
    decision = step(_state(), _observation(
        chain_vol_p90=None, executable_price=None, depth_pull_flag=True,
        tape_complete=True, depth_provenance="trusted", capture_version="v6-incremental-v4",
        market_event_kind="v2_burn", liquidity_removed_fraction=0.40,
    ))
    assert decision.action is V6Action.EXIT_FULL
    assert "lp_loss" in decision.reason_codes


def test_negative_lp_pull_requires_complete_trusted_tape():
    decision = step(_state(), _observation(
        depth_pull_flag=False, tape_complete=False, depth_provenance=None,
        capture_version="v6-incremental-v4",
    ))
    lp_loss = next(result for result in decision.rule_results if result.rule_id == "lp_loss")
    assert lp_loss.status is RuleStatus.INSUFFICIENT
    assert lp_loss.missing_fields == ("tape_complete", "depth_provenance")


def test_non_swap_market_event_cannot_advance_staged_break_hysteresis():
    before = _state(
        break_state=V6State(break_level=0.00040, neg_streak=1),
        last_market_evidence_id="market-swap-1",
    )
    decision = step(before, _market_observation(
        "market-sync-1", market_event_kind="v2_sync", is_break=True,
        held_s=20_000.0, arrival_rate=0.0, buy_share=0.0, realized_vol=0.01,
        prior_low=0.00040, price=0.00030,
    ))
    assert decision.action is V6Action.HOLD
    assert decision.state_if_filled.break_state.neg_streak == 1
    assert decision.state_if_filled.break_episode is None


def test_quote_only_wake_preserves_missing_break_evidence():
    state = _state(last_market_evidence_id="market-1", last_quote_evidence_id="quote-1")
    payload = complete_payload()
    payload["is_break"] = None
    observation = make_observation(
        observation_id="quote-only", observed_at="2026-07-26T12:00:01+00:00",
        network_id="solana", payload=payload,
        evidence=EvidenceIdentity(market="market-1", quote="quote-2"),
    )

    decision = step(state, observation)

    staged = next(result for result in decision.rule_results if result.rule_id == "staged_break")
    assert staged.status is RuleStatus.INSUFFICIENT
    assert staged.missing_fields == ("is_break",)


def test_all_terminal_and_staged_rules_report_status():
    decision = step(_state(), _observation())
    assert {result.rule_id for result in decision.rule_results} == {
        "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break",
    }


def test_executable_catastrophe_uses_entry_not_chart_price():
    state = _state(entry_unit_price="1.0", entry_quote_asset="USDC",
                   executable_high_water=1.0)
    first = step(state, _market_observation(
        "market-cat-1", executable_price=0.2, price=0.9, quote_status="healthy",
        quote_out_asset="USDC"))
    assert first.action is V6Action.HOLD
    evidence = EvidenceIdentity(market="market-cat-2", quote="quote-cat-2")
    decision = step(first.state_if_filled, make_observation(
        observation_id=semantic_observation_id("p1", evidence),
        observed_at="2026-07-26T12:00:01+00:00", network_id="solana",
        payload=complete_payload(executable_price=0.19, price=0.9,
                                 quote_status="healthy", quote_out_asset="USDC"),
        evidence=evidence))
    assert decision.action is V6Action.EXIT_FULL
    assert "catastrophe" in decision.reason_codes


def test_single_implausible_adverse_quote_is_quarantined():
    state = _state(entry_unit_price="1.0", entry_quote_asset="USDC",
                   executable_high_water=1.1)
    decision = step(state, _market_observation(
        "market-bad-quote", executable_price=1e-9, quote_status="healthy",
        quote_out_asset="USDC"))
    assert decision.action is V6Action.HOLD
    assert "catastrophe:confirmation_pending" in decision.reason_codes
    assert "catastrophe:quote_quarantined" in decision.reason_codes
    assert decision.state_if_filled.executable_high_water == 1.1
    assert decision.state_if_filled.catastrophe_confirmations == 1


def test_minor_lp_burn_holds_and_material_burn_derisks():
    common = dict(depth_pull_flag=True, tape_complete=True, depth_provenance="trusted",
                  capture_version="v6-incremental-v4", market_event_kind="v2_burn")
    minor = step(_state(), _observation(**common, liquidity_removed_fraction=0.0065))
    assert minor.action is V6Action.HOLD
    material = step(_state(), _observation(**common, liquidity_removed_fraction=0.10))
    assert material.action is V6Action.DERISK
    assert material.fraction_ppm == 500_000


def test_lp_burn_without_magnitude_is_insufficient_not_a_cut():
    decision = step(_state(), _observation(
        depth_pull_flag=True, tape_complete=True, depth_provenance="trusted",
        capture_version="v6-incremental-v4", liquidity_removed_fraction=None))
    assert decision.action is V6Action.HOLD
    result = next(r for r in decision.rule_results if r.rule_id == "lp_loss")
    assert result.status is RuleStatus.INSUFFICIENT
    assert result.missing_fields == ("liquidity_removed_fraction",)


def test_route_loss_requires_four_distinct_checks_and_sixty_seconds():
    state = _state(route_absence=RouteAbsenceState(
        first_absent_at="2026-07-26T11:59:00+00:00", checks=3,
        last_quote_evidence_id="quote-3",
    ))
    evidence = EvidenceIdentity(market="market-route", quote="quote-4")
    observation = make_observation(
        observation_id=semantic_observation_id("p1", evidence),
        observed_at="2026-07-26T12:00:00+00:00", network_id="solana",
        payload=complete_payload(quote_status="route_absent", executable_price=None),
        evidence=evidence,
    )
    decision = step(state, observation)
    assert decision.action is V6Action.EXIT_FULL
    assert "route_loss" in decision.reason_codes


def test_provider_failure_does_not_advance_or_reset_route_absence():
    route = RouteAbsenceState(
        first_absent_at="2026-07-26T11:59:00+00:00", checks=3,
        last_quote_evidence_id="quote-3")
    state = _state(route_absence=route)
    evidence = EvidenceIdentity(market="market-provider", quote="quote-provider")
    observation = make_observation(
        observation_id=semantic_observation_id("p1", evidence),
        observed_at="2026-07-26T12:00:00+00:00", network_id="solana",
        payload=complete_payload(
            quote_status="provider_unavailable", executable_price=None),
        evidence=evidence)
    decision = step(state, observation)
    assert decision.state_if_filled.route_absence == route
    assert decision.action is V6Action.HOLD


def test_time_expiry_is_inside_v6_and_can_be_unfilled():
    decision = step(_state(), _observation(held_s=43_200, executable_price=None))
    assert decision.action is V6Action.EXIT_FULL
    assert decision.state_if_unfilled.remaining_qty_atomic > \
        decision.state_if_filled.remaining_qty_atomic


def test_unfilled_derisk_preserves_inventory_and_partial_budget():
    before = _state(remaining_qty_atomic=1_000_000)
    decision = step(before, make_observation(
        observation_id="o2",
        observed_at="2026-07-26T12:00:01+00:00",
        network_id="4663",
        payload=complete_payload(is_break=True, arrival_rate=0.01,
                                 buy_share=0.10, realized_vol=0.01),
    ))
    assert decision.action is V6Action.DERISK
    assert decision.state_if_unfilled.remaining_qty_atomic == 1_000_000
    assert decision.state_if_unfilled.break_state.tranches == before.break_state.tranches
    assert decision.state_if_unfilled.pending_action_token == decision.action_token
    assert decision.state_if_filled.break_state.tranches == before.break_state.tranches + 1


def test_transition_is_byte_stable():
    assert step(_state(), _observation()) == step(_state(), _observation())


def test_v6action_values_are_uppercase():
    # Locked public contract: V6Action(str, Enum) values are the persisted/compared strings.
    assert V6Action.HOLD.value == "HOLD"
    assert V6Action.DERISK.value == "DERISK"
    assert V6Action.EXIT_FULL.value == "EXIT_FULL"


def test_v6positionstate_has_last_action_outcome_default_none():
    # Locked public contract: worker bookkeeping field, defaults None, engine must not invent one.
    assert _state().last_action_outcome is None


def test_v1_state_document_is_bounded_and_round_trips():
    samples = tuple((f"market-{i}", float(i)) for i in range(64))
    state = _state(volatility_samples=samples, last_market_evidence_id="market-63")
    encoded = canonical_json(state_document(state))
    assert len(encoded.encode()) <= 16 * 1024
    assert state_from_document(json.loads(encoded)) == state


def test_v1_state_rejects_more_than_64_volatility_samples():
    with pytest.raises(ValueError, match="64"):
        _state(volatility_samples=tuple((str(i), 0.1) for i in range(65)))


def test_policy_identity_is_v2():
    assert POLICY_ID == "v6-position-engine-v2"


def test_v1_identity_and_counter_types_are_strict():
    with pytest.raises(ValueError, match="market"):
        EvidenceIdentity(market=123)
    with pytest.raises(ValueError, match="neg_streak"):
        BreakEpisode("episode", 1.0, "market-1", neg_streak=0.5)
    with pytest.raises(ValueError, match="fraction_ppm"):
        ActionIntent("token", V6Action.DERISK, 250_000.5, 1, "market-1")


def test_same_break_evidence_at_10hz_cannot_consume_multiple_tranches():
    first = step(_state(), _market_observation(
        "market-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01,
    ))
    assert first.action is V6Action.DERISK
    state = first.state_if_filled
    for tenth in range(1, 20):
        again = step(state, _market_observation(
            "market-1", observed_at=f"2026-07-26T12:00:{tenth / 10:04.1f}+00:00",
            is_break=True, held_s=20_000.0, arrival_rate=0.0,
            buy_share=0.0, realized_vol=0.01,
        ))
        assert again.action is V6Action.HOLD
        assert again.semantic_transition is False
        assert again.state_if_filled == state
    assert state.filled_tranches == 1


def test_recovery_only_closes_the_current_break_episode():
    first = step(_state(), _market_observation(
        "market-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01, prior_low=0.00040, price=0.00030,
    )).state_if_filled
    recovered = step(first, _market_observation(
        "market-2", is_break=False, held_s=20_001.0, net=1.0,
        prior_low=0.00040, price=0.00041,
    )).state_if_filled
    assert recovered.break_episode.status == "recovered"
    later = step(recovered, _market_observation(
        "market-3", is_break=True, held_s=20_002.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01, prior_low=0.00030, price=0.00020,
    ))
    assert later.action is V6Action.DERISK
    assert later.state_if_filled.break_episode.episode_id != first.break_episode.episode_id


def test_exact_source_full_exit_confirms_second_tranche():
    first = step(_state(), _market_observation(
        "market-source-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01, prior_low=0.00040, price=0.00030,
    )).state_if_filled
    evidence = EvidenceIdentity(
        market="market-source-1", quote="quote-1", source="source-close-1")
    observation = make_observation(
        observation_id=semantic_observation_id("p1", evidence),
        observed_at="2026-07-26T12:00:01+00:00", network_id="solana",
        payload=complete_payload(
            is_break=False, price=0.00030, net=0.0, source_status="exited",
            source_wallet_exited=True),
        evidence=evidence)
    decision = step(first, observation)
    assert decision.action is V6Action.DERISK
    assert "source_full_exit" in decision.reason_codes


def test_material_route_degradation_confirms_second_tranche():
    first = step(_state(), _market_observation(
        "market-route-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01, prior_low=0.00040, price=0.00030,
    )).state_if_filled
    decision = step(first, _market_observation(
        "market-route-2", is_break=False, held_s=20_001.0, net=0.0,
        prior_low=0.00040, price=0.00030, route_degraded=True,
    ))
    assert decision.action is V6Action.DERISK
    assert "route_degraded" in decision.reason_codes


@pytest.mark.parametrize("override", [
    {"route_degraded": True},
    {"source_wallet_exited": True, "source_status": "exited"},
])
def test_route_or_source_exit_overrides_strong_positive_wallet_prior(override):
    decision = step(_state(), _market_observation(
        "market-prior-override", is_break=True, held_s=20_000.0,
        arrival_rate=0.2, buy_share=0.0, realized_vol=0.01,
        wallet_tier_adjustment=-1, **override,
    ))
    assert decision.action is V6Action.EXIT_FULL
    assert "tier:2" in decision.reason_codes


def test_one_hundred_thousand_market_transitions_keep_state_bounded():
    state = _state()
    for i in range(100_000):
        decision = step(state, _market_observation(
            f"market-{i}", is_break=False, held_s=float(i % 1000),
            realized_vol=(i % 100) / 100,
        ))
        state = decision.state_if_filled
    assert len(state.volatility_samples) == 64
    assert len(canonical_json(state_document(state)).encode()) <= 16 * 1024


def test_equivalent_action_reuses_the_active_intent_token():
    first = step(_state(), _market_observation(
        "market-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01,
    ))
    pending = first.state_if_unfilled
    assert pending.active_intent.token == first.action_intent_token
    repeated = step(pending, _market_observation(
        "market-1", observed_at="2026-07-26T12:00:00.100+00:00",
        is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01,
    ))
    assert repeated.action_intent_token == first.action_intent_token
    assert repeated.semantic_transition is False


def test_distinct_persisted_observations_cannot_reuse_an_action_token():
    evidence = EvidenceIdentity(market="market-A", quote="quote-A")
    first = make_observation(
        observation_id="obs-A-1", observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana", payload=complete_payload(), evidence=evidence,
    )
    later = make_observation(
        observation_id="obs-A-3", observed_at="2026-07-26T12:00:02+00:00",
        network_id="solana", payload=complete_payload(), evidence=evidence,
    )

    assert step(_state(), first).action_token != step(_state(), later).action_token


def test_exit_full_supersedes_one_pending_partial():
    partial = step(_state(), _market_observation(
        "market-1", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01,
    )).state_if_unfilled
    terminal = step(partial, _market_observation(
        "market-2", is_break=True, held_s=60.0, arrival_rate=0.2,
        buy_share=0.5, realized_vol=0.5,
    ))
    assert terminal.action is V6Action.EXIT_FULL
    assert terminal.supersedes_token == partial.active_intent.token
    assert terminal.state_if_unfilled.active_intent.token == terminal.action_intent_token


def test_recovery_cancels_an_unfilled_break_intent():
    partial = step(_state(), _market_observation(
        "market-break", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01, prior_low=0.00040, price=0.00030,
    )).state_if_unfilled
    token = partial.active_intent.token

    recovered = step(partial, _market_observation(
        "market-recovery", observed_at="2026-07-26T12:00:01+00:00",
        is_break=False, held_s=20_001.0, net=1.0,
        prior_low=0.00040, price=0.00041, quote_status="healthy",
    ))

    assert recovered.action is V6Action.HOLD
    assert "recovery" in recovered.reason_codes
    assert "active_intent_cancelled" in recovered.reason_codes
    assert recovered.supersedes_token == token
    assert recovered.state_if_filled.active_intent is None


def test_suppressed_partial_does_not_consume_a_tranche():
    active = ActionIntent("strong", V6Action.EXIT_FULL, 1_000_000, 2, "market-prior")
    state = _state(active_intent=active)
    decision = step(state, _market_observation(
        "market-new", is_break=True, held_s=20_000.0, arrival_rate=0.0,
        buy_share=0.0, realized_vol=0.01))
    assert decision.action is V6Action.HOLD
    assert "active_intent_retained" in decision.reason_codes
    assert decision.state_if_filled.break_state.tranches == 0
    assert decision.state_if_filled.break_state.awaiting is False


def test_early_is_not_a_free_full_cut_point():
    decision = step(_state(), _market_observation(
        "market-early", is_break=True, held_s=60.0, arrival_rate=0.2,
        buy_share=0.1, realized_vol=0.01, chain_vol_p90=0.5))
    assert decision.action is V6Action.DERISK
    assert "early" in decision.reason_codes


def test_reason_codes_are_unique():
    decision = step(_state(), _market_observation(
        "market-unique", is_break=True, held_s=20_000.0, arrival_rate=0.2,
        buy_share=0.5, realized_vol=0.01, chain_vol_p90=None))
    assert len(decision.reason_codes) == len(set(decision.reason_codes))


def test_hold_preserves_pending_token_and_filled_equals_unfilled():
    before = _state(pending_action_token="prior-token")
    decision = step(before, _observation())
    assert decision.action is V6Action.HOLD
    assert decision.state_if_filled == decision.state_if_unfilled
    assert decision.state_if_filled.pending_action_token == "prior-token"


def test_action_successors_record_the_modeled_fill_outcome():
    decision = step(_state(), _observation(
        is_break=True, arrival_rate=0.01, buy_share=0.10, realized_vol=0.01,
    ))
    assert decision.action is V6Action.DERISK
    assert decision.state_if_filled.last_action_outcome == "filled"
    assert decision.state_if_unfilled.last_action_outcome == "unfilled"


def test_missing_seed_field_is_a_data_insufficient_hold():
    payload = complete_payload()
    del payload["is_break"]
    decision = step(_state(), make_observation(
        observation_id="missing-is-break",
        observed_at="2026-07-26T12:00:00+00:00",
        network_id="solana",
        payload=payload,
    ))
    assert decision.action is V6Action.HOLD
    assert decision.reason_codes == ("data_insufficient:is_break",)


def test_derisk_of_one_atomic_unit_requests_one_unit():
    decision = step(_state(remaining_qty_atomic=1), _observation(
        is_break=True, arrival_rate=0.01, buy_share=0.10, realized_vol=0.01,
    ))
    assert decision.action is V6Action.DERISK
    assert decision.state_if_filled.remaining_qty_atomic == 0


def test_policy_document_freezes_every_seed_threshold_and_action_fraction():
    document = policy_document()
    assert document["seed_observation_fields"] == [
        "is_break", "held_s", "arrival_rate", "buy_share", "realized_vol",
        "prior_low", "price", "net",
    ]
    seed = document["seed_parameters"]
    assert seed == {
        "early_s": 10800.0,
        "frenzy_rate": 0.10,
        "crowded_share": 1.0 / 3.0,
        "vol_percentile": 90,
        "vol_min_observations": 12,
        "max_tranches": 2,
        "negative_streak_confirm": 2,
        "first_derisk_fraction_by_tier": {"0": 0.25, "1": 0.5},
        "confirmation_derisk_fraction": 0.5,
        "terminal_fraction": 1.0,
    }
    assert document["ordered_rules"] == [
        "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break"]
    assert document["terminal_parameters"]["route_absent_min_checks"] == 4


@pytest.mark.parametrize(("field", "bad"), [
    ("is_break", "false"),
    ("route_degraded", 0),
    ("source_wallet_exited", "no"),
    ("wallet_tier_adjustment", 0.5),
    ("held_s", -1.0),
    ("arrival_rate", -0.1),
    ("buy_share", 1.1),
    ("realized_vol", -0.1),
    ("prior_low", 0.0),
    ("price", -1.0),
    ("chain_vol_p90", -0.1),
    ("executable_price", 0.0),
    ("net", "negative"),
])
def test_malformed_observation_values_are_rejected(field, bad):
    with pytest.raises(ValueError, match=field):
        _observation(**{field: bad})
