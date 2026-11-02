"""Watch v22 grammar (operator 2026-10-23): WATCH verdict, price triggers in % of the sheet price, a position plan.
Only standing cohorts accept it; older cohorts are unchanged."""
import pytest

from test_memecoin_desk_contract import api, judge, verdict
from sniper.services import memecoin_desk_contract as contract

STANDING = {"version": "watch-v1", "watch_max_s": 1800, "graduation_guard_pct": 90, "dead_tape_noise_k": 3, "px_hold_max_noise": 2, "px_hold_hold_s": 5, "px_hold_max_wait_s": 60}
PLAN = {"inv": ["support", None], "h_s": 300, "tz": [20, 60]}


def sj(raw, **kw):
    return judge(raw, standing=STANDING, bars_available=True, **kw)


def test_old_cohort_rejects_the_new_grammar():
    assert judge(verdict(v="H", r=None))["reason"] == "verdict_enum"
    assert judge({**verdict(), "p": PLAN})["reason"] == "verdict_keys"
    assert "plan" not in judge(verdict())                       # older results stay byte-identical


def test_enter_and_pullback_wait_carry_the_plan():
    out = sj({**verdict(), "p": PLAN})
    assert out["action"] == "ENTER" and out["plan"] == PLAN
    w = sj({**verdict(v="W", e=["px_hold", "<=", -8, 2, 600]), "p": PLAN})
    assert w["action"] == "WAIT" and w["condition"] == ["px_hold", "<=", -8, 2, 600]


def test_watch_has_no_rails_plan_or_condition():
    out = sj({**verdict(v="H", r=None), "p": None})
    assert out["action"] == "WATCH" and out["rails"] is None and out["plan"] is None
    assert sj({**verdict(v="H", r=None), "p": PLAN})["reason"] == "unexpected_plan"


@pytest.mark.parametrize("cond", [
    ["px_up", ">=", -5, 1, 600], ["px_hold", "<=", 5, 1, 600], ["px_up", "<=", 5, 1, 600],
    ["px_up", ">=", 250, 1, 600], ["px_hold", "<=", -8, 4, 600], ["px_hold", "<=", -8, 2, 1801],
    ["px_hold", "<=", -8, 2, 9]])
def test_price_trigger_bounds(cond):
    assert sj({**verdict(v="W", e=cond), "p": PLAN})["reason"] == "cond_out_of_range"


@pytest.mark.parametrize("plan", [None, {"inv": ["px", 5], "h_s": 300, "tz": [20, 60]},
                                  {"inv": ["support", 3], "h_s": 300, "tz": [20, 60]},
                                  {"inv": ["px", -10], "h_s": 30, "tz": [20, 60]},
                                  {"inv": ["px", -10], "h_s": 300, "tz": [60, 20]}])
def test_enter_needs_a_bounded_plan(plan):
    assert sj({**verdict(), "p": plan})["reason"] == "plan_out_of_range"


def test_price_trigger_without_bars_is_unavailable():
    out = judge({**verdict(v="W", e=["px_up", ">=", 6, 1, 600]), "p": PLAN}, standing=STANDING, bars_available=False)
    assert out["reason"] == "cond_unavailable"


@pytest.mark.parametrize("bad", [None, {}, {**STANDING, "watch_max_s": 30}, {**STANDING, "version": "x"},
                                 {**STANDING, "extra": 1}])
def test_standing_rule_is_validated(bad):
    with pytest.raises(ValueError):
        api().validate_standing(bad)


@pytest.mark.parametrize("extra, ok", [({}, True), ({"noise_basis": "true_range", "drift_k": 2.0}, True), ({"noise_basis": "median"}, False),
                                       ({"drift_k": 0}, False), ({"drift_k": "2"}, False), ({"extra": 1}, False)])
def test_standing_policy_optional_noise_basis_and_drift_k_are_validated(extra, ok):
    if ok:
        assert contract.validate_standing({**STANDING, **extra}) == {**STANDING, **extra}
    else:
        with pytest.raises(ValueError):
            contract.validate_standing({**STANDING, **extra})
