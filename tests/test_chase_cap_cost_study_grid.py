"""The chase study's threshold is a passing point on its own 0.01 pp grid (2026-10-08 review SF05).

needed_pp bisected to within 0.01 and returned round(hi, 2). Rounding can move a passing bound down
onto a failing cap: the review's case has its true threshold at +5.004 pp, needed_pp said 5.0, and
buy_at counted a slot fill at +5 although the exact signed minimum there (1,000,030) exceeds what
was available (1,000,000). The search now runs on integer hundredths: the reported t passes and
t - 0.01 fails, so `need <= t` on the grid is the exact floor predicate.
"""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "chase_cap_cost_study_grid", Path(__file__).resolve().parents[1] / "scripts/research/chase_cap_cost_study.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

EDGE = dict(expected_tokens_atomic=1350040, cost_usdc_atomic=1350040, source_price_usd=1,
            base_decimals=6, cap_pct=30)
AVAILABLE = 1_000_000


def _passes(spl, available, t, variant):
    return S.min_out(spl, *S.caps_at(float(spl["cap_pct"]), t, variant)) <= available


def test_the_reviews_edge_case_is_not_a_fill_at_plus_5():
    need = S.needed_pp(EDGE, AVAILABLE, "B")
    assert S.min_out(EDGE, *S.caps_at(30, 5, "B")) == 1_000_030 > AVAILABLE
    assert need == 5.01, need
    assert S.buy_at({"need_pp_B": need}, [], None, "M", "S", 5, "B", 0) is None


@pytest.mark.parametrize("variant", ["A", "B"])
@pytest.mark.parametrize("available", [990_000, 999_999, 1_000_000, 1_000_029, 1_000_031, 1_050_000])
def test_the_reported_need_passes_and_one_step_below_fails(variant, available):
    need = S.needed_pp(EDGE, available, variant)
    if need is None:
        assert not _passes(EDGE, available, 400.0, variant)
        return
    assert _passes(EDGE, available, need, variant), need
    if need > 0:
        assert not _passes(EDGE, available, round(need - 0.01, 2), variant), need


def test_a_row_that_already_fills_needs_nothing():
    assert S.needed_pp(EDGE, 10 ** 9, "B") == 0.0
