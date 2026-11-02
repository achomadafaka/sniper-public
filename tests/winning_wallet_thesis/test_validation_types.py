import pytest

from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower, ValidationPlan


def test_validation_plan_marks_unpowered_as_replication_required():
    power = ForwardPower(expected_events=4, horizon_days=30, powered=False)
    plan = ValidationPlan(
        thesis_id="t1",
        validation_state="replication_required",
        forward_power=power,
        paper_card=None,
        blockers=["underpowered_single_wallet_forward"],
    )
    assert plan.validation_state == "replication_required"
    assert plan.replication == {"state": "not_run"}


def test_unknown_validation_state_rejected():
    with pytest.raises(ValueError):
        ValidationPlan("t", "bogus", ForwardPower(0, 30, False))
