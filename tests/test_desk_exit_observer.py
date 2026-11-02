import copy
import json
import math

import pytest

from sniper.services import memecoin_desk_exit_observer as O


def row(t, *, q=100_000_000_000, buy=True, actor="alice", signature=None, **extra):
    out = dict(received_at=t, signature=signature or f"sig-{t}", is_buy=buy,
               quote_atomic=1_000_000_000, token_atomic=100, actor=actor,
               virtual_quote=q, virtual_token=1_000_000, real_quote=90_000_000_000,
               fee_bps=100, creator_fee_bps=50, cashback_fee_bps=0,
               slot=1000 + int(t * 10), tx_index=0, ordinal=0)
    out.update(extra)
    return out


def tape():
    return [row(2.5 + 5 * i, q=100_000_000_000 + (i % 2) * 1_000_000_000,
                buy=i % 2 == 0, actor=f"actor-{i % 7}") for i in range(120)]


def evaluate(rows=None, **kw):
    opts = dict(cut=600.0, coverage_from=0.0, opened_at=320.0,
                residual_atomic="1000", own_signatures={"ours"}, own_complete=True)
    opts.update(kw)
    return O.evaluate(tape() if rows is None else rows, **opts)


def test_fixed_grid_json_and_no_action_surface():
    result = evaluate()
    assert result["rule_version"] == "exit-observer-v1"
    assert result["population"] == "own_excluded"
    assert set(result["flags"]) == {"SELL_BURST", "EXODUS", "STRUCT_BREAK", "ONE_SWAP_DROP",
                                   "TOP_DUMP_TAPE_PROXY", "CREATOR_SELL_TAPE_PROXY"}
    for name, grid in result["flags"].items():
        assert set(grid) == ({"0"} if name == "CREATOR_SELL_TAPE_PROXY" else {"2", "3", "4", "6"})
    assert not ({"action", "ANY", "TWO_OF"} & result.keys())
    json.dumps(result, allow_nan=False)


def test_exact_live_curve_value_matches_native_sell_helper_and_individual_fee_rounding():
    from sniper.execution.pump_native_exit import curve_sell_quote
    r = row(599, q=987_654_321, real_quote=987_654_321, virtual_token=1_234_567,
            fee_bps=123, creator_fee_bps=45)
    quoted, error = curve_sell_quote({"virtual_sol_reserves": r["virtual_quote"],
                                    "virtual_token_reserves": r["virtual_token"],
                                    "real_sol_reserves": r["real_quote"]},
                                   1000, 1, protocol_bps=123, creator_bps=45)
    assert error is None
    assert evaluate([r])["value_sol"] == quoted["expected_out"] / 1e9
    r["cashback_fee_bps"] = 7
    expected = quoted["expected_out"] - (quoted["gross_out"] * 7 + 9999) // 10000
    assert evaluate([r])["value_sol"] == expected / 1e9


def test_research_to_live_reserve_identity_does_not_double_count_our_buy():
    q, b, qn, tokens = 30_000_000_000, 1_000_000, 1_000_000_000, 12345
    live = row(599, q=q + qn, virtual_token=b - tokens, fee_bps=0,
               creator_fee_bps=0, cashback_fee_bps=0)
    got = evaluate([live], residual_atomic=str(tokens))["value_sol"]
    assert got == ((q + qn) * tokens // b) / 1e9


@pytest.mark.parametrize("field,value", [("fee_bps", None), ("creator_fee_bps", None),
                                        ("cashback_fee_bps", None), ("virtual_quote", 0),
                                        ("virtual_token", 0), ("real_quote", 0),
                                        ("fee_bps", -1), ("fee_bps", 1.5)])
def test_unknown_fee_or_unsellable_state_is_not_a_zero_value(field, value):
    rows = tape()
    rows[-1][field] = value
    result = evaluate(rows)
    assert result["value_sol"] is None
    assert all(v is None for v in result["flags"]["STRUCT_BREAK"].values())
    assert any(v is not None for v in result["flags"]["SELL_BURST"].values())


@pytest.mark.parametrize("qty", [None, 0, -1, 1.0, True, "1.0", "-1", " 1", "01"])
def test_residual_atoms_are_positive_canonical_integer_text(qty):
    result = evaluate(residual_atomic=qty)
    assert result["value_sol"] is None
    assert result["residual_atomic"] is None
    assert result["peak_covered_from_fill"] is False


def test_closed_cut_excludes_exact_boundary_and_future_rows():
    rows = tape()
    expected = evaluate(rows)
    rows += [row(600, q=1), row(601, is_buy="wrong", fee_bps=None)]
    assert evaluate(rows) == expected


def test_own_completeness_is_required_for_every_candidate():
    result = evaluate(own_complete=False)
    assert result["population"] is None
    assert result["value_sol"] is not None
    assert all(v is None for grid in result["flags"].values() for v in grid.values())
    assert result["noise_5"] is None


@pytest.mark.parametrize("changes", [{"coverage_from": None}, {"coverage_from": 601},
                                     {"rows": []}, {"own_signatures": []}])
def test_unproven_source_or_own_membership_never_claims_own_excluded_population(changes):
    result = evaluate(**changes)
    assert result["population"] is None
    assert all(v is None for grid in result["flags"].values() for v in grid.values())


def test_noise_has_full_covered_quiet_carries_and_needs_twelve_returns():
    result = evaluate()
    assert result["noise_5"] == pytest.approx(math.log(101 / 100))
    result = evaluate(coverage_from=550)
    assert result["noise_5"] is None
    assert result["flags"]["SELL_BURST"]["2"] is None
    assert result["peak_covered_from_fill"] is False


def test_zero_noise_is_known_but_scaled_price_flags_are_unknown():
    result = evaluate([row(1), row(599)])
    assert result["noise_5"] == 0
    for name in ("STRUCT_BREAK", "ONE_SWAP_DROP"):
        assert result["flags"][name]["2"] is None
        assert result["unknown"][name]["2"] == "noise_zero"


def test_own_swap_is_removed_from_noise_flow_and_activity_but_not_value_path():
    rows = tape()
    base = evaluate(rows)
    rows += [row(599, q=50_000_000_000, buy=False, signature="ours", quote_atomic=90_000_000_000)]
    got = evaluate(rows)
    assert got["own_sig_excluded_count"] == 1
    assert got["noise_5"] == base["noise_5"]
    assert got["flags"]["SELL_BURST"] == base["flags"]["SELL_BURST"]
    assert got["flags"]["ONE_SWAP_DROP"]["2"] is False
    assert got["value_sol"] < base["value_sol"]
    assert got["flags"]["STRUCT_BREAK"]["2"] is True


def test_one_swap_pair_keeps_own_predecessor_state_and_skips_own_destination():
    rows = tape()[:-1]
    rows += [row(597, q=150_000_000_000, signature="ours"),
             row(599, q=120_000_000_000, buy=False)]
    assert evaluate(rows)["flags"]["ONE_SWAP_DROP"]["2"] is True


def test_buy_fee_tier_fall_is_not_single_sell_but_can_break_structure():
    rows = tape()
    rows += [row(599, buy=True, q=101_000_000_000, fee_bps=3000,
                 quote_atomic=0, slot=20000)]
    result = evaluate(rows)
    assert result["flags"]["ONE_SWAP_DROP"] == {str(k): False for k in O.GRID}
    assert result["flags"]["STRUCT_BREAK"] == {str(k): True for k in O.GRID}


@pytest.mark.parametrize("field", ["slot", "tx_index", "ordinal"])
def test_unknown_chain_order_refuses_single_swap_drop(field):
    rows = tape()
    rows[-1][field] = None
    assert evaluate(rows)["flags"]["ONE_SWAP_DROP"]["2"] is None


def test_missing_quantity_cannot_make_a_quiet_single_swap_window_known_false():
    rows = tape()
    for r in rows:
        r["is_buy"] = True
    got = evaluate(rows, residual_atomic=None)
    assert got["flags"]["ONE_SWAP_DROP"]["2"] is None


def test_duplicate_chain_key_refuses_single_swap_drop():
    rows = tape()
    rows[-1].update({field: rows[-2][field] for field in ("slot", "tx_index", "ordinal")})
    assert evaluate(rows)["flags"]["ONE_SWAP_DROP"]["2"] is None


def test_inventory_and_creator_require_capture_from_observed_launch():
    rows = [row(1, actor="creator", token_atomic=100),
            row(580, actor="creator", buy=False, token_atomic=50)]
    for kw in ({}, {"launch_at": 1, "coverage_from": 2}):
        got = evaluate(rows, **kw)
        assert got["flags"]["TOP_DUMP_TAPE_PROXY"]["2"] is None
        assert got["flags"]["CREATOR_SELL_TAPE_PROXY"]["0"] is None
    got = evaluate(rows, launch_at=1)
    assert got["flags"]["TOP_DUMP_TAPE_PROXY"]["2"] is True
    assert got["flags"]["CREATOR_SELL_TAPE_PROXY"]["0"] is True


def test_own_first_swapper_is_not_replaced_by_first_external_actor():
    rows = [row(1, actor="me", signature="ours"), row(2, actor="external"),
            row(590, actor="external", buy=False)]
    got = evaluate(rows, launch_at=1)
    assert got["flags"]["CREATOR_SELL_TAPE_PROXY"]["0"] is False


def test_peak_uses_current_residual_quantity_for_all_states_and_is_left_censored():
    rows = tape()
    got = evaluate(rows, residual_atomic="2000")
    expected = max((r["virtual_quote"] * 2000 // (r["virtual_token"] + 2000)
                    - sum((r["virtual_quote"] * 2000 // (r["virtual_token"] + 2000) * r[k] + 9999) // 10000
                          for k in ("fee_bps", "creator_fee_bps", "cashback_fee_bps"))) / 1e9
                   for r in rows if r["received_at"] >= 320)
    assert got["peak_value_sol"] == expected
    assert got["peak_covered_from_fill"] is True
    late = evaluate(rows, coverage_from=400)
    assert late["peak_covered_from_fill"] is False
    assert late["peak_value_sol"] is not None


def test_invalid_own_signature_proof_and_unplaceable_time_are_unknown():
    assert evaluate(own_signatures="ours")["flags"]["SELL_BURST"]["2"] is None
    malformed = row(1)
    malformed["received_at"] = float("nan")
    rows = tape() + [malformed]
    result = evaluate(rows)
    assert all(v is None for grid in result["flags"].values() for v in grid.values())
    json.dumps(result, allow_nan=False)


def test_evaluate_does_not_mutate_input():
    rows = tape()
    original = copy.deepcopy(rows)
    evaluate(rows)
    assert rows == original


def test_structure_low_includes_covered_quiet_anchor_before_window():
    rows = tape()
    rows[59]["virtual_quote"] = 50_000_000_000  # 297.5, carried through the 300 boundary.
    rows += [row(599, q=80_000_000_000, buy=False)]
    assert evaluate(rows)["flags"]["STRUCT_BREAK"]["2"] is False


def test_delayed_older_chain_state_cannot_create_a_post_fill_peak():
    rows = tape()
    old = row(599, q=500_000_000_000, slot=1)
    assert evaluate(rows + [old])["peak_value_sol"] == evaluate(rows)["peak_value_sol"]


@pytest.mark.parametrize("field", ["quote_atomic", "virtual_quote", "virtual_token", "real_quote"])
def test_malformed_or_out_of_range_atomic_text_never_crashes(field):
    rows = tape()
    rows[-1][field] = "9" * 5000
    result = evaluate(rows)
    json.dumps(result, allow_nan=False)
    assert result["flags"]["SELL_BURST"]["2"] is None if field == "quote_atomic" else result["value_sol"] is None


def test_sell_burst_uses_six_prior_positive_windows_and_does_not_include_current():
    rows = [row(301 + 10 * i, buy=False, quote_atomic=1_000_000_000) for i in range(29)]
    rows += [row(599, buy=False, quote_atomic=4_000_000_000)]
    got = evaluate(rows)
    assert got["flags"]["SELL_BURST"] == {"2": True, "3": True, "4": True, "6": False}
    rows[0]["quote_atomic"] = 0  # zero is known, excluded from the positive-window median.
    assert evaluate(rows)["flags"]["SELL_BURST"]["4"] is True


def test_exodus_requires_low_distinct_buyers_and_sell_dominance():
    rows = [row(301 + 30 * i + j, actor=f"buyer-{j}") for i in range(9) for j in range(6)]
    rows += [row(599, buy=False, quote_atomic=1)]
    assert evaluate(rows)["flags"]["EXODUS"] == {str(k): True for k in O.GRID}
    rows += [row(598, buy=True, actor="current", quote_atomic=2)]
    assert evaluate(rows)["flags"]["EXODUS"] == {str(k): False for k in O.GRID}
