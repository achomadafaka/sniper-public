"""Code-built evidence gates: literal facts, independent thresholds, no model or I/O.

Boundaries are inclusive for >=, <= and code 8's [0, k*n5] target interval.
Buy fading is strictly < its threshold. Zero noise is unknown, not a zero margin.
Root owns RED/GREEN execution; this file does not exercise funded state.
"""
from copy import deepcopy

import pytest

from sniper.services import memecoin_desk_risk_gate as G


LN2 = 0.6931471805599453
BAD_NUMBERS = [None, float("nan"), float("inf"), float("-inf"), True, False]


def block(**facts):
    ev = {
        "fresh": True, "n5": 0.02, "last_price": 1.0, "entry_price": 1.0,
        "high_30s_post_fill": 1.0, "high_120s_post_fill": 1.0,
        "new_high_60s": False, "new_high_120s": False, "held_s": 300.0,
        "buy_sol_30s": 1.0, "sell_sol_30s": 2.0, "distinct_sellers_30s": 3,
        "buy_sol_prev_30s": 3.0, "swaps_60s": 2,
        "swaps_median_60s_prior_300s": 10.0,
    }
    ev.update(facts)
    return {"ev": ev, "sb": 0, "rails": {"trail_pct": 15.0, "tp_pct": 60.0, "sl_pct": 20.0}}


def check(b, action, code, want, *, params=None):
    proposed = ({6: {"trail_pct": 10.0}, 7: {"sl_pct": 12.0}, 8: {"tp_pct": 40.0},
                 9: {"trail_pct": 10.0}}.get(code, {"trail_pct": 10.0}) if action == "TIGHTEN" else None)
    result = G.backing(b, action, proposed, code, params=params)
    assert isinstance(result, tuple) and len(result) == 2
    backed, failed = result
    assert backed is want
    assert isinstance(failed, list)
    assert all(isinstance(fact, str) and fact for fact in failed)
    assert failed == [] if want else bool(failed)


@pytest.mark.parametrize("buy,sell,sellers,want", [
    (2.0, 3.0, 3, True),                    # exact 1.5x flow and three sellers
    (2.0, 2.999999, 3, False),
    (2.0, 3.000001, 3, True),
    (2.0, 3.0, 2, False),
    (0.0, 1.0, 3, True),                    # known zero buying, positive selling
    (0.0, 0.0, 3, False),                   # 0 >= 1.5*0 is insufficient
    (-1.0, 1.0, 3, False),
    (1.0, -1.0, 3, False),
])
def test_exit_11_requires_positive_dominant_sell_flow_and_known_sellers(buy, sell, sellers, want):
    check(block(buy_sol_30s=buy, sell_sol_30s=sell, distinct_sellers_30s=sellers), "EXIT", 11, want)


@pytest.mark.parametrize("last,want", [(1.0, True), (0.999999, True), (1.000001, False)])
def test_exit_12_failed_high_drop_includes_the_exact_one_noise_boundary(last, want):
    # high=2 and n5=ln(2): the one-noise lower boundary is exactly 1.
    check(block(n5=LN2, high_120s_post_fill=2.0, last_price=last), "EXIT", 12, want)


@pytest.mark.parametrize("flag", [True, None, 0, 1, "false"])
def test_exit_12_needs_an_explicit_false_new_high_flag(flag):
    check(block(n5=LN2, high_120s_post_fill=2.0, last_price=0.9, new_high_60s=flag), "EXIT", 12, False)


@pytest.mark.parametrize("held,want", [(299.999999, False), (300.0, True), (300.000001, True)])
def test_exit_13_default_horizon_is_300_seconds_inclusive(held, want):
    check(block(held_s=held), "EXIT", 13, want)


@pytest.mark.parametrize("held,horizon,want", [
    (300.0, 600.0, False), (600.0, 600.0, True), (600.000001, 600.0, True),
    (300.0, None, False), (300.0, 0.0, False), (300.0, -1.0, False),
])
def test_exit_13_present_plan_horizon_is_authoritative_and_cannot_default(held, horizon, want):
    check(block(held_s=held, plan_horizon_s=horizon), "EXIT", 13, want)


@pytest.mark.parametrize("flag", [True, None, 0, 1, "false"])
def test_exit_13_needs_an_explicit_false_120_second_high_flag(flag):
    check(block(new_high_120s=flag), "EXIT", 13, False)


@pytest.mark.parametrize("high,buy,sell,want", [
    (8.0, 2.0, 3.0, True),                  # ln(8) = 3*ln(2), sell share = .6
    (7.999999, 2.0, 3.0, False),
    (8.000001, 2.0, 3.0, True),
    (8.0, 2.0, 2.999999, False),
    (8.0, 2.0, 3.000001, True),
    (8.0, 0.0, 1.0, True),
    (8.0, 0.0, 0.0, False),
])
def test_exit_14_needs_both_three_noise_collapse_and_sixty_percent_sell_share(high, buy, sell, want):
    check(block(n5=LN2, high_30s_post_fill=high, last_price=1.0, buy_sol_30s=buy, sell_sol_30s=sell), "EXIT", 14, want)


@pytest.mark.parametrize("swaps,baseline,want", [
    (2, 8.0, True), (3, 8.0, False), (1, 8.0, True),
    (0, 8.0, True),                         # known inactivity against a positive baseline
    (0, 0.0, False), (0, -1.0, False), (-1, 8.0, False),
])
def test_exit_15_inactivity_includes_quarter_baseline_but_needs_a_positive_baseline(swaps, baseline, want):
    check(block(swaps_60s=swaps, swaps_median_60s_prior_300s=baseline), "EXIT", 15, want)


def test_exit_16_is_never_backed_even_with_every_other_exit_fact_present():
    b = block(last_price=0.1, high_30s_post_fill=2.0, high_120s_post_fill=2.0, held_s=1000,
              buy_sol_30s=0.0, sell_sol_30s=100.0, distinct_sellers_30s=100, swaps_60s=0)
    b["sb"] = 1
    check(b, "EXIT", 16, False)


@pytest.mark.parametrize("last,want", [(1.999999, False), (2.0, True), (2.000001, True)])
def test_tighten_6_lock_gain_uses_entry_and_includes_one_noise_unit(last, want):
    check(block(n5=LN2, last_price=last), "TIGHTEN", 6, want)


@pytest.mark.parametrize("buy,sell,want", [(45.0, 55.0, True), (45.0, 54.999999, False),
                                         (0.0, 1.0, True), (0.0, 0.0, False)])
def test_tighten_7_sell_share_is_inclusive_and_does_not_need_noise(buy, sell, want):
    check(block(n5=None, high_120s_post_fill=None, buy_sol_30s=buy, sell_sol_30s=sell), "TIGHTEN", 7, want)


def test_tighten_7_failed_high_branch_can_back_when_flow_is_unknown():
    check(block(n5=LN2, high_120s_post_fill=2.0, last_price=1.0, buy_sol_30s=None,
                sell_sol_30s=None), "TIGHTEN", 7, True)


@pytest.mark.parametrize("last,want", [(1.6, True), (0.8, True), (1.600001, False), (0.799999, False)])
def test_tighten_8_target_interval_includes_both_ends_but_not_a_target_already_below_price(last, want):
    # Current TP60 means target1.6; .8 is exactly ln(2) below that target.
    check(block(n5=LN2, last_price=last, buy_sol_30s=1.0,
                buy_sol_prev_30s=1.0), "TIGHTEN", 8, want)


@pytest.mark.parametrize("buy,prior,want", [(0.499999, 1.0, True), (0.5, 1.0, False),
                                          (0.500001, 1.0, False), (0.0, 1.0, True),
                                          (0.0, 0.0, False), (0.0, -1.0, False)])
def test_tighten_8_buy_fade_is_strict_and_can_back_without_price_or_noise(buy, prior, want):
    check(block(n5=None, last_price=None, entry_price=None, buy_sol_30s=buy,
                buy_sol_prev_30s=prior), "TIGHTEN", 8, want)


@pytest.mark.parametrize("trail,want", [(None, True), (0, True), (0.0, True),
                                       (15.0, False), (True, False), (False, False),
                                       (float("nan"), False), (float("inf"), False)])
def test_tighten_9_only_backs_an_explicitly_known_missing_trail_without_fresh_evidence(trail, want):
    check({"ev": None, "sb": None, "rails": {"trail_pct": trail}}, "TIGHTEN", 9, want)


def test_tighten_9_absent_trail_key_is_unknown_not_an_explicit_disabled_trail():
    check({"ev": None, "rails": {}}, "TIGHTEN", 9, False)


@pytest.mark.parametrize("sb,want", [(1, True), (0, False), (None, False), (True, False), (False, False)])
def test_exit_10_requires_the_confirmed_structure_break_fact_but_no_noise_fact(sb, want):
    check({"ev": {"fresh": True}, "sb": sb, "rails": {}}, "EXIT", 10, want)


@pytest.mark.parametrize("action,code", [("TIGHTEN", 6), ("TIGHTEN", 7), ("TIGHTEN", 8),
                                         ("EXIT", 10), ("EXIT", 11), ("EXIT", 12),
                                         ("EXIT", 13), ("EXIT", 14), ("EXIT", 15)])
@pytest.mark.parametrize("fresh", [None, False, 0, 1, "true"])
def test_stale_or_unknown_evidence_cannot_back_any_tape_reason(action, code, fresh):
    b = block(fresh=fresh, n5=LN2, last_price=2.0, high_30s_post_fill=16.0, high_120s_post_fill=4.0,
              buy_sol_30s=0.0, sell_sol_30s=3.0, swaps_60s=0)
    b["sb"] = 1
    check(b, action, code, False)


# Each case has exactly one active backing branch, so an unknown fact cannot hide
# behind another OR branch. Missing, nonfinite and bool are not numeric evidence.
@pytest.mark.parametrize("action,code,changes,field", [
    ("EXIT", 11, {}, "buy_sol_30s"),
    ("EXIT", 11, {}, "sell_sol_30s"),
    ("EXIT", 11, {}, "distinct_sellers_30s"),
    ("EXIT", 12, {"last_price": 0.8}, "high_120s_post_fill"),
    ("EXIT", 12, {"last_price": 0.8}, "last_price"),
    ("EXIT", 13, {}, "held_s"),
    ("EXIT", 14, {"high_30s_post_fill": 2.0}, "high_30s_post_fill"),
    ("EXIT", 15, {}, "swaps_60s"),
    ("EXIT", 15, {}, "swaps_median_60s_prior_300s"),
    ("TIGHTEN", 6, {"last_price": 1.1}, "entry_price"),
    ("TIGHTEN", 7, {"n5": None}, "sell_sol_30s"),
    ("TIGHTEN", 8, {"n5": None}, "buy_sol_prev_30s"),
])
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_unknown_nonfinite_and_bool_numeric_facts_never_back_the_active_branch(action, code, changes, field, bad):
    b = block(**changes)
    b["ev"][field] = bad
    check(b, action, code, False)


@pytest.mark.parametrize("action,code,changes", [
    ("TIGHTEN", 6, {"last_price": 1.1}),
    ("TIGHTEN", 8, {"last_price": 1.59, "buy_sol_30s": 1.0, "buy_sol_prev_30s": 1.0}),
    ("EXIT", 12, {"last_price": 0.8}),
    ("EXIT", 14, {"high_30s_post_fill": 2.0}),
])
@pytest.mark.parametrize("noise", [None, 0.0, -0.02, float("nan"), float("inf"), True, False])
def test_noise_based_reasons_need_positive_finite_nonbool_noise(action, code, changes, noise):
    check(block(**{**changes, "n5": noise}), action, code, False)


@pytest.mark.parametrize("action,code", [("TIGHTEN", 10), ("TIGHTEN", 11), ("EXIT", 6),
                                         ("EXIT", 9), ("EXIT", 17), ("HOLD", 11),
                                         ("LOOSEN_TRAIL", 17), ("L", 17)])
def test_other_action_reason_pairs_are_unbacked_and_do_not_claim_loosen_authority(action, code):
    check(block(), action, code, False)


def test_explicit_flow_parameter_changes_the_threshold_without_weakening_other_facts():
    b = block(buy_sol_30s=2.0, sell_sol_30s=3.0)
    check(b, "EXIT", 11, True)
    check(b, "EXIT", 11, False, params={"flow_sell_multiple": 2.0})
    b["ev"]["sell_sol_30s"] = 4.0
    check(b, "EXIT", 11, True, params={"flow_sell_multiple": 2.0})
    b["ev"]["distinct_sellers_30s"] = 2
    check(b, "EXIT", 11, False, params={"flow_sell_multiple": 2.0})


def test_run_horizon_parameter_only_applies_when_the_plan_horizon_is_absent():
    b = block(held_s=300.0)
    check(b, "EXIT", 13, False, params={"run_horizon_s": 600.0})
    b["ev"]["plan_horizon_s"] = 120.0
    check(b, "EXIT", 13, True, params={"run_horizon_s": 600.0})


def test_backing_does_not_mutate_evidence_rails_or_parameters():
    b = block()
    proposed = {"sl_pct": 10.0}
    params = {"weak_sell_share": 0.55}
    before = deepcopy((b, proposed, params))
    G.backing(b, "TIGHTEN", proposed, 7, params=params)
    assert (b, proposed, params) == before


def test_material_first_observation_is_a_baseline_not_a_market_move():
    assert G.material(block(), None) == (False, "baseline")


@pytest.mark.parametrize("changes", [{"fresh": False}, {"fresh": None}, {"n5": None},
                                     {"n5": 0.0}, {"n5": float("nan")},
                                     {"n5": float("inf")}, {"n5": True}])
def test_material_requires_fresh_evidence_and_positive_known_noise(changes):
    assert G.material(block(**changes), block()) == (False, "unknown")


def test_material_identical_prices_and_rails_are_unchanged():
    previous = block()
    assert G.material(deepcopy(previous), previous) == (False, "unchanged")


@pytest.mark.parametrize("last,want", [(2.0, (True, "move")), (0.5, (True, "move")),
                                      (1.999999, (False, "unchanged")),
                                      (0.500001, (False, "unchanged"))])
def test_material_current_price_move_includes_exactly_one_noise_unit_in_both_directions(last, want):
    assert G.material(block(n5=LN2, last_price=last), block(n5=LN2)) == want


@pytest.mark.parametrize("field,value,want", [
    ("high_since_fill", 2.0, (True, "move")),
    ("high_since_fill", 1.999999, (False, "unchanged")),
    ("low_since_fill", 0.5, (True, "move")),
    ("low_since_fill", 0.500001, (False, "unchanged")),
])
def test_material_new_high_or_low_can_wake_when_the_last_price_has_recovered(field, value, want):
    previous = block(n5=LN2, high_since_fill=1.0, low_since_fill=1.0)
    current = deepcopy(previous)
    current["ev"][field] = value
    assert G.material(current, previous) == want


def test_material_move_multiple_uses_the_current_noise_and_explicit_parameter():
    previous = block(n5=0.02)
    current = block(n5=LN2, last_price=2.0)
    assert G.material(current, previous) == (True, "move")
    assert G.material(current, previous, params={"material_move_n5": 2.0}) == (False, "unchanged")
    current["ev"]["last_price"] = 4.0
    assert G.material(current, previous, params={"material_move_n5": 2.0}) == (True, "move")


def test_material_rail_proximity_only_changes_between_two_known_signatures():
    previous = block()
    previous["rail_proximity"] = {"trail": False, "tp": False, "sl": False}
    current = deepcopy(previous)
    current["rail_proximity"]["trail"] = True
    assert G.material(current, previous) == (True, "rail_proximity")
    previous["rail_proximity"] = None
    assert G.material(current, previous) == (False, "unchanged")
    current["rail_proximity"] = None
    assert G.material(current, block()) == (False, "unchanged")


def test_a_no_trail_fact_cannot_justify_a_stop_or_take_profit_change():
    b = {"rails": {"trail_pct": None}}
    for proposed in ({"sl_pct": 12.}, {"tp_pct": 40.}, {"trail_pct": 10., "sl_pct": 12.}):
        assert G.backing(b, "TIGHTEN", proposed, 9)[0] is False


@pytest.mark.parametrize("scale", [1e-12, 1e-10, 1e-9, 1e-8, 1e8])
def test_inclusive_gain_boundary_does_not_depend_on_token_price_scale(scale):
    check(block(n5=LN2, entry_price=scale, last_price=2*scale), "TIGHTEN", 6, True)


@pytest.mark.parametrize("scale", [1e-12, 1e-10, 1e-9, 1e-8, 1e8])
def test_inclusive_material_boundary_does_not_depend_on_token_price_scale(scale):
    assert G.material(block(n5=LN2, last_price=2*scale), block(n5=LN2, last_price=scale)) == (True, "move")
