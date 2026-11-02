import math
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import signal_payoff_extrema as X

F, D = 0.00804, 0.20            # fixed cost and spend, SOL


def returns(entry_price, marks):
    """net return when the entry is bought at entry_price and sold at each mark price (price ratio)."""
    return [(D * m / entry_price - D - F) / D for m in marks]


def test_highest_priced_entry_is_not_automatically_the_worst():
    # Codex counterexample: entries 100 or 110; later marks 96 then 50
    e110 = ([returns(110, [96]), returns(110, [50])], returns(110, [50]))
    e100 = ([returns(100, [96]), returns(100, [50])], returns(100, [50]))
    lo110, _, _ = X.policy_extrema_for_entry(*e110, take=0.20, stop=0.12)
    lo100, _, _ = X.policy_extrema_for_entry(*e100, take=0.20, stop=0.12)
    assert math.isclose(lo110, -0.16747, abs_tol=2e-4)
    assert math.isclose(lo100, -0.5402, abs_tol=2e-4)       # holds at 96 (-8.02 %), stops at 50
    lo, hi, _ = X.policy_extrema([e110, e100])
    assert math.isclose(lo, -0.5402, abs_tol=2e-4)            # the minimum is over BOTH entries
    assert lo < lo110


def test_in_slot_round_trip_is_not_bounded_by_its_endpoints():
    # prices 100 -> 200 -> 100 inside one slot: states {1.0 (flat), 2.0}; endpoints both flat
    slot = returns(100, [100, 200, 100])
    lo, hi, amb = X.policy_extrema_for_entry([slot], slot, take=0.20, stop=0.12)
    assert hi > 0.9 and lo >= 0.20 and not amb                # a take state exists, so the policy exits >= +20 %


def test_two_barriers_in_one_slot_are_ambiguous_and_bound_both_ways():
    slot = returns(100, [150, 50])
    lo, hi, amb = X.policy_extrema_for_entry([slot], slot, take=0.20, stop=0.12)
    assert amb and lo < -0.4 and hi > 0.4


def test_no_crossing_uses_the_end_slot_set():
    slot = returns(100, [101, 99])
    lo, hi, amb = X.policy_extrema_for_entry([slot], slot, take=0.20, stop=0.12)
    assert not amb and lo == min(slot) and hi == max(slot)


def test_lower_support_formula():
    assert math.isclose(X.lower_support(0.20, F), -1.0402, abs_tol=1e-9)
    assert math.isclose(X.lower_support(0.40, F), -1.0201, abs_tol=1e-9)


@pytest.mark.parametrize("entries", [
    [],                                               # no admissible entry
    [([[math.nan], [0.3]], [0.0])],                   # an unknown state is never skipped
    [([[math.inf]], [0.0])],
    [([[], [0.3]], [0.0])],                           # an empty inner slot
    [([[0.01]], [])],                                 # an empty end set
    [([[0.01]], [None])],
    [([[True]], [0.0])],
    [([], [0.1])],                                    # no slot after the entry slot: an empty required prefix
    [([[0.01], []], [0.0])],                          # a required (reached) slot is empty
    [([[0.4]], [0.0]), ([[math.nan]], [0.0])],        # a later NaN entry cannot hide behind a valid first one
])
def test_unknown_or_empty_inputs_raise_instead_of_returning_a_finite_extremum(entries):
    with pytest.raises(X.Uncomputable):
        X.policy_extrema(entries)
    lo, hi, amb, censored = X.censored_extrema(entries, D, F)
    assert censored and hi == math.inf and math.isclose(lo, -1.0402, abs_tol=1e-9)


@pytest.mark.parametrize("args", [(0.0, 0.2, F), (-1.0, 0.2, F), (math.nan, 0.2, F), (D, math.nan, F), (D, 0.2, math.inf), (D, -0.1, F)])
def test_net_rejects_an_invalid_spend_or_input_but_accepts_zero_proceeds(args):
    proceeds, spend, fixed = args[1], args[0], args[2]
    with pytest.raises(X.Uncomputable):
        X.net(proceeds, spend, fixed)
    assert math.isclose(X.net(0.0, D, F), -1.0402, abs_tol=1e-9)        # zero proceeds is a measured value


def test_take_and_stop_must_be_finite_positive():
    with pytest.raises(X.Uncomputable):
        X.policy_extrema([([[0.3]], [0.0])], take=0.0)
    with pytest.raises(X.Uncomputable):
        X.policy_extrema([([[0.3]], [0.0])], stop=math.nan)


def test_unknown_states_after_a_proven_exit_are_irrelevant():
    # Codex: [[0.4], []] with end [] legitimately returns (0.4, 0.4): the exit at 0.4 is proven first
    assert X.policy_extrema_for_entry([[0.4], []], [], take=0.20, stop=0.12) == (0.4, 0.4, False)
    assert X.policy_extrema([([[0.4], [math.nan]], [])]) == (0.4, 0.4, False)


def test_no_crossing_requires_the_end_set():
    with pytest.raises(X.Uncomputable):
        X.policy_extrema_for_entry([[0.01]], [], take=0.20, stop=0.12)
