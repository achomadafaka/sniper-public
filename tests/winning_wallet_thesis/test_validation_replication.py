from sniper.analysis.winning_wallet_thesis.validation.replication import replication_required_summary
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower, ValidationPlan


def test_replication_summary_counts_required_plans():
    plans = [
        ValidationPlan("a", "replication_required", ForwardPower(3, 30, False)),
        ValidationPlan("b", "descriptive_only", ForwardPower(0, 30, False)),
    ]
    summary = replication_required_summary(plans)
    assert summary["replication_required"] == 1
    assert summary["thesis_ids"] == ["a"]
    assert summary["state"] == "not_run"


def test_replication_summary_not_required_when_none():
    plans = [ValidationPlan("b", "descriptive_only", ForwardPower(0, 30, False))]
    assert replication_required_summary(plans)["state"] == "not_required"
