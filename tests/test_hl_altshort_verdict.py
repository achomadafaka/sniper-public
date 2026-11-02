import pytest

from sniper.hyperlive.altshort_evidence import altshort_verdict


@pytest.fixture
def passing_evidence():
    return {
        "provenance_valid": True,
        "causal_features": True,
        "next_tradeable_entry": True,
        "residual_ci_low_bps": 10,
        "fixed_slot_base_bps": 100,
        "fixed_slot_stress_bps": 50,
        "total_book_bps": 80,
        "leave_top_coin_bps": 20,
        "leave_top_week_bps": 10,
        "top_coin_share": 0.30,
        "top_week_share": 0.40,
        "coverage": 0.90,
        "exit_rule_valid": True,
        "day_clusters": 20,
        "stale_open_marks": 0,
        "regime_episode_count": 2,
        "min_episode_closes": 5,
        "min_episode_residual_bps": 10,
        "leave_one_episode_out_residual_bps": 10,
    }


def test_verdict_promotes_only_when_every_gate_passes(passing_evidence):
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_promote"
    assert verdict["failed"] == []


def test_verdict_rejects_negative_edge(passing_evidence):
    passing_evidence["residual_ci_low_bps"] = -1
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_reject"
    assert "residual_ci" in verdict["failed"]


def test_verdict_blocks_invalid_provenance(passing_evidence):
    passing_evidence["provenance_valid"] = False
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "blocked_missing_feed"
    assert "provenance" in verdict["blocked"]


def test_verdict_continues_when_sample_or_marks_are_incomplete(passing_evidence):
    passing_evidence["day_clusters"] = 5
    assert altshort_verdict(passing_evidence)["status"] == "paper_continue"
    passing_evidence["day_clusters"] = 20
    passing_evidence["stale_open_marks"] = 1
    assert altshort_verdict(passing_evidence)["status"] == "paper_continue"


def test_verdict_rejects_concentration_and_cost_stress(passing_evidence):
    passing_evidence["top_coin_share"] = 0.36
    passing_evidence["fixed_slot_stress_bps"] = -1
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_reject"
    assert set(verdict["failed"]) >= {"top_coin_concentration", "cost_stress"}


def test_verdict_rejects_unfrozen_or_invalid_exit_rule(passing_evidence):
    passing_evidence["exit_rule_valid"] = False
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_reject"
    assert "exit_rule" in verdict["failed"]


def test_verdict_continues_after_many_days_in_one_bearish_episode(passing_evidence):
    passing_evidence["day_clusters"] = 46
    passing_evidence["regime_episode_count"] = 1
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_continue"
    assert "regime_episodes" in verdict["incomplete"]


def test_verdict_requires_five_closes_per_episode(passing_evidence):
    passing_evidence["min_episode_closes"] = 4
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_continue"
    assert "episode_sample" in verdict["incomplete"]


def test_verdict_rejects_nonreplicating_episode_after_sample_is_complete(passing_evidence):
    passing_evidence["min_episode_residual_bps"] = -1
    passing_evidence["leave_one_episode_out_residual_bps"] = -2
    verdict = altshort_verdict(passing_evidence)
    assert verdict["status"] == "paper_reject"
    assert set(verdict["failed"]) >= {"episode_residual", "episode_leave_one_out"}
