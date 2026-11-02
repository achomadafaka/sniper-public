from sniper.services.memecoin_v6_engine import (
    EvidenceIdentity,
    RuleStatus,
    V6Action,
    V6PositionState,
    evaluate_take_profit,
    evaluate_trail,
    make_observation,
)
from sniper.services.memecoin_v6_policy_v3 import POLICY_V3


BASE = {
    "is_break": False,
    "held_s": 30.0,
    "arrival_rate": 0.0,
    "buy_share": 0.1,
    "realized_vol": 0.0,
    "prior_low": 1.0,
    "price": 1.0,
    "net": 1.0,
    "chain_vol_p90": 1.0,
    "route_degraded": False,
    "source_wallet_exited": False,
    "wallet_tier_adjustment": 0,
    "creator_sale_since_entry": False,
    "quote_status": "healthy",
    "tape_complete": True,
    "depth_pull_flag": False,
}


def observation(price, *, quote="q1", market="m1", **overrides):
    payload = {**BASE, "executable_price": price, **overrides}
    return make_observation(
        observation_id=f"o-{quote}-{price}",
        observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana",
        payload=payload,
        evidence=EvidenceIdentity(market=market, quote=quote),
    )


def state(*, high_water=None, entry="1.0", filled_tranches=0):
    return V6PositionState(
        position_id="p1",
        remaining_qty_atomic=1_000_000,
        entry_unit_price=entry,
        executable_high_water=high_water,
        filled_tranches=filled_tranches,
        opened_at="2026-08-11T00:00:00+00:00",
    )


def test_take_profit_derisks_half_at_plus_1200_bps():
    result = evaluate_take_profit(state(), observation(1.12), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.DERISK
    assert result.fraction_ppm == 500_000


def test_take_profit_does_not_trigger_below_the_threshold():
    result = evaluate_take_profit(state(), observation(1.1199), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_is_inert_before_the_take_profit_fill():
    result = evaluate_trail(state(high_water=1.20), observation(1.104), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_exits_after_the_take_profit_fill():
    result = evaluate_trail(
        state(high_water=1.20, filled_tranches=1), observation(1.104), POLICY_V3)
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL
    assert result.fraction_ppm == 1_000_000


def test_trail_holds_one_tick_above_the_stop_line():
    result = evaluate_trail(
        state(high_water=1.20, filled_tranches=1), observation(1.105), POLICY_V3)
    assert result.status is RuleStatus.NOT_TRIGGERED


def test_trail_needs_a_healthy_quote():
    result = evaluate_trail(
        state(high_water=1.20, filled_tranches=1),
        observation(1.104, quote="q2", quote_status="provider_unavailable"),
        POLICY_V3,
    )
    assert result.status is RuleStatus.INSUFFICIENT


def test_exact_stop_needs_two_distinct_healthy_quotes():
    from sniper.services.memecoin_v6_engine import evaluate_exact_stop

    first = state()
    assert evaluate_exact_stop(
        first, observation(0.90, quote="q1"), POLICY_V3,
    ).status is RuleStatus.NOT_TRIGGERED
    confirmed = V6PositionState(
        position_id="p1",
        remaining_qty_atomic=1_000_000,
        entry_unit_price="1.0",
        catastrophe_candidate_price=0.90,
        catastrophe_confirmations=2,
        opened_at="2026-08-11T00:00:00+00:00",
    )
    result = evaluate_exact_stop(
        confirmed, observation(0.90, quote="q2"), POLICY_V3,
    )
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL


def test_provider_failure_never_confirms_the_stop():
    from sniper.services.memecoin_v6_engine import _advance_executable_quote

    before = V6PositionState(
        position_id="p1",
        remaining_qty_atomic=1_000_000,
        entry_unit_price="1.0",
        catastrophe_confirmations=1,
        catastrophe_candidate_price=0.90,
    )
    after = _advance_executable_quote(
        before,
        observation(0.90, quote="q9", quote_status="provider_unavailable"),
        True,
        POLICY_V3,
    )
    assert after.catastrophe_confirmations == 1


def test_creator_sale_exits_full():
    from sniper.services.memecoin_v6_engine import evaluate_creator_sale

    result = evaluate_creator_sale(
        state(), observation(1.0, creator_sale_since_entry=True), POLICY_V3,
    )
    assert result.status is RuleStatus.TRIGGERED
    assert result.action is V6Action.EXIT_FULL
    assert result.priority == 95


def test_creator_sale_is_insufficient_when_absent():
    from sniper.services.memecoin_v6_engine import evaluate_creator_sale

    payload = dict(BASE)
    payload.pop("creator_sale_since_entry")
    payload["executable_price"] = 1.0
    obs = make_observation(
        observation_id="o-nc",
        observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana",
        payload=payload,
        evidence=EvidenceIdentity(market="m1", quote="q1"),
    )
    assert evaluate_creator_sale(
        state(), obs, POLICY_V3,
    ).status is RuleStatus.INSUFFICIENT


def test_time_cap_holds_a_strong_position_to_300_s():
    from sniper.services.memecoin_v6_engine import evaluate_time_cap

    strong = observation(1.05, held_s=125.0, net=1.0, is_break=False)
    assert evaluate_time_cap(
        state(), strong, POLICY_V3,
    ).status is RuleStatus.NOT_TRIGGERED
    weak = observation(
        0.98, held_s=125.0, net=-1.0, is_break=False, quote="q3",
    )
    assert evaluate_time_cap(
        state(), weak, POLICY_V3,
    ).status is RuleStatus.TRIGGERED
    late = observation(
        1.05, held_s=301.0, net=1.0, is_break=False, quote="q4",
    )
    assert evaluate_time_cap(
        state(), late, POLICY_V3,
    ).status is RuleStatus.TRIGGERED


def test_v3_step_takes_profit_then_exits_the_remainder_on_a_break():
    from sniper.services.memecoin_v6_engine import step

    opened = state()
    first = step(opened, observation(1.15, quote="qa"), policy=POLICY_V3)
    assert first.action is V6Action.DERISK
    assert first.fraction_ppm == 500_000
    assert first.policy_hash == POLICY_V3.hash()
    after = first.state_if_filled
    assert after.filled_tranches == 1
    broken = observation(
        1.10,
        quote="qb",
        market="m2",
        is_break=True,
        prior_low=1.10,
        net=-5.0,
        arrival_rate=0.5,
        buy_share=0.9,
        realized_vol=9.0,
    )
    second = step(after, broken, policy=POLICY_V3)
    assert second.action is V6Action.EXIT_FULL
    assert second.fraction_ppm == 1_000_000
    assert "budget_exhausted" in second.reason_codes


def test_v3_missing_evidence_holds_with_sorted_reason_codes():
    from sniper.services.memecoin_v6_engine import step

    payload = dict(BASE)
    payload.pop("creator_sale_since_entry")
    payload.pop("chain_vol_p90")
    payload["executable_price"] = 1.0
    obs = make_observation(
        observation_id="o-missing",
        observed_at="2026-08-11T00:00:30+00:00",
        network_id="solana",
        payload=payload,
        evidence=EvidenceIdentity(market="m9", quote="q9"),
    )
    decision = step(state(), obs, policy=POLICY_V3)
    assert decision.action is V6Action.HOLD
    codes = [
        code for code in decision.reason_codes
        if code.startswith("creator_sale:insufficient")
    ]
    assert codes
    assert decision.reason_codes == tuple(sorted(decision.reason_codes))


def test_v2_and_v3_hashes_differ():
    from sniper.services.memecoin_v6_engine import POLICY_V2

    assert POLICY_V2.hash() != POLICY_V3.hash()
