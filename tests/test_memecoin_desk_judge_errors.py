"""Each JUDGE dependency failure is its own SKIP cause (umbrella rule-5 map item 5)."""
import pytest

from sniper.services import memecoin_desk_contract as contract
from test_memecoin_desk_contract import BOUNDS

CAPS = {"enter_ttl_max_s": 30, "boundary_max_s": 120, "wait_max_s": 300}


@pytest.mark.parametrize("slug", sorted(contract.JUDGE_CALL_ERRORS))
def test_known_call_error_keeps_its_own_reason(slug):
    out = contract.validate_verdict(None, opportunity_id="op", bounds=BOUNDS, timing_caps=CAPS, elapsed_ms=10, call_error=slug)
    assert out["action"] == "SKIP" and out["reason"] == "judge_" + slug


def test_unknown_call_error_folds_to_judge_error():
    out = contract.validate_verdict(None, opportunity_id="op", bounds=BOUNDS, timing_caps=CAPS, elapsed_ms=10, call_error="weird")
    assert out["reason"] == "judge_error"


def test_closed_set_names_the_required_causes():
    assert {"refusal", "rate_limited", "auth_expired", "budget_exhausted", "cli_failed", "timeout",
            "lost", "model_result_error"} <= contract.JUDGE_CALL_ERRORS
