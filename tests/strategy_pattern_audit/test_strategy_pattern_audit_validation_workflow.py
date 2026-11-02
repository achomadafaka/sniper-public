"""Validation workflow tests for cross-wallet thesis candidates."""
from __future__ import annotations


def _audit() -> dict:
    return {
        "surface_version": "strategy_pattern_audit_dossier_v1",
        "theses": [
            {
                "mechanism": "prediction_driven",
                "grain_key": {
                    "category": "sports",
                    "market_type": "binary",
                    "event_family": "sports_head_to_head",
                },
                "required_test_data": ["price_history", "resolution_outcomes"],
                "supporting_wallets": ["0x1", "0x2"],
            }
        ],
        "data_gaps": [
            {
                "gap_name": "market_selection_universe_baseline_missing",
                "n_affected_wallets": 2,
                "severity": "high",
            }
        ],
    }


def test_validation_workflow_returns_no_plans_without_theses() -> None:
    from sniper.analysis.strategy_pattern_audit.validation_workflow import (
        build_thesis_validation_workflow,
    )

    workflow = build_thesis_validation_workflow({"theses": [], "data_gaps": []})

    assert workflow["validation_plans"] == []
    assert workflow["top_level_caveats"] == ["NO_PROMOTED_THESES_TO_VALIDATE"]


def test_validation_workflow_marks_missing_dataset_requirements() -> None:
    from sniper.analysis.strategy_pattern_audit.validation_workflow import (
        build_thesis_validation_workflow,
    )

    workflow = build_thesis_validation_workflow(
        _audit(),
        dataset_inventory={"resolution_outcomes"},
    )

    plan = workflow["validation_plans"][0]
    assert plan["status"] == "needs_data"
    assert plan["missing_test_data"] == ["price_history"]
    assert plan["workflow_steps"][0] == {
        "name": "dataset_build",
        "status": "blocked",
        "requires": ["price_history", "resolution_outcomes"],
    }
    assert plan["workflow_steps"][-1]["name"] == "live_validation"
    assert plan["workflow_steps"][-1]["status"] == "out_of_scope"


def test_validation_workflow_marks_simulation_ready_when_data_exists() -> None:
    from sniper.analysis.strategy_pattern_audit.validation_workflow import (
        build_thesis_validation_workflow,
    )

    workflow = build_thesis_validation_workflow(
        _audit(),
        dataset_inventory={"price_history", "resolution_outcomes"},
    )

    plan = workflow["validation_plans"][0]
    assert plan["status"] == "simulation_ready"
    assert plan["missing_test_data"] == []
    assert plan["workflow_steps"][0]["status"] == "ready"
    assert plan["workflow_steps"][1] == {
        "name": "offline_replay_simulation",
        "status": "ready",
        "requires": ["timestamp_valid_replay", "fees_slippage_model"],
    }
    assert plan["workflow_steps"][2] == {
        "name": "paper_validation",
        "status": "blocked_until_simulation_review",
        "requires": ["operator_reviewed_simulation"],
    }


def test_validation_workflow_preserves_audit_data_gaps() -> None:
    from sniper.analysis.strategy_pattern_audit.validation_workflow import (
        build_thesis_validation_workflow,
    )

    workflow = build_thesis_validation_workflow(_audit())

    assert workflow["data_gaps"] == _audit()["data_gaps"]
