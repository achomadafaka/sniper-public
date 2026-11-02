"""Strategy Pattern Audit adapter tests for wallet dossiers."""
from __future__ import annotations

import json


def _dossier(
    wallet: str,
    *,
    thesis: dict | None = None,
    mechanism: dict | None = None,
    gap: dict | None = None,
) -> dict:
    grain_key = {
        "category": "sports",
        "category_source": "tag_nav_category",
        "market_type": "binary",
        "outcome_role": "binary_named_outcome",
        "event_family": "sports_head_to_head",
        "favorite_status": "favorite",
        "entry_price_bucket": "mid",
        "exit_mode": "held",
        "position_pattern": "front_loaded",
    }
    return {
        "dossier_meta": [
            {"key": "wallet", "value": wallet},
            {"key": "schema_version", "value": "v4_thesis_lifecycle"},
        ],
        "result_semantics": [
            {
                "grain_id": "grain-1",
                "grain_key": json.dumps(grain_key, sort_keys=True),
                "n_positions": 12,
                "n_episodes": 44,
                "grain_quality": "interpretable",
            }
        ],
        "mechanism_evidence": [
            mechanism
            or {
                "grain_id": "grain-1",
                "mechanism": "prediction_driven",
                "evidence_strength": "metric_supported",
                "candidate_thesis_evidence": None,
                "supporting_metrics": json.dumps({"outcome_win_rate_held": 0.62}),
            }
        ],
        "thesis_candidates": [thesis] if thesis else [],
        "data_gaps": [gap] if gap else [],
    }


def _thesis(text: str = "Sports favorites held to resolution show candidate evidence") -> dict:
    return {
        "thesis_id": "t1",
        "grain_id": "grain-1",
        "mechanism": "prediction_driven",
        "status": "candidate",
        "thesis_text": text,
        "supporting_metrics": json.dumps({"outcome_win_rate_held": 0.62}),
        "required_test_data": json.dumps(["resolution_outcomes", "price_history"]),
        "simulation_plan": json.dumps({"status": "simulation_ready_after_dataset_build"}),
        "source_schema_version": "v4_thesis_lifecycle",
    }


def test_dossier_audit_returns_none_for_zero_dossiers() -> None:
    from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
        compute_dossier_strategy_pattern_audit,
    )

    assert compute_dossier_strategy_pattern_audit([]) is None


def test_dossier_audit_promotes_repeated_thesis_candidate_grain() -> None:
    from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
        compute_dossier_strategy_pattern_audit,
    )

    result = compute_dossier_strategy_pattern_audit(
        [_dossier("0x1", thesis=_thesis()), _dossier("0x2", thesis=_thesis())],
        audit_run_id="audit-dossier-1",
        min_supporting_wallets=2,
    )

    assert result["surface_version"] == "strategy_pattern_audit_dossier_v1"
    assert result["audit_run_id"] == "audit-dossier-1"
    assert len(result["theses"]) == 1
    row = result["theses"][0]
    assert row["mechanism"] == "prediction_driven"
    assert row["grain_key"]["category"] == "sports"
    assert row["grain_key"]["event_family"] == "sports_head_to_head"
    assert row["n_supporting_wallets"] == 2
    assert row["supporting_wallets"] == ["0x1", "0x2"]
    assert row["required_test_data"] == ["price_history", "resolution_outcomes"]
    assert "trade" not in row


def test_dossier_audit_rejects_single_wallet_candidate() -> None:
    from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
        compute_dossier_strategy_pattern_audit,
    )

    result = compute_dossier_strategy_pattern_audit(
        [_dossier("0x1", thesis=_thesis()), _dossier("0x2")],
        min_supporting_wallets=2,
    )

    assert result["theses"] == []
    assert len(result["rejected_theses"]) == 1
    assert result["rejected_theses"][0]["honesty_bar"]["fail_reasons"] == ["support_floor"]


def test_dossier_audit_does_not_promote_raw_metric_supported_rows() -> None:
    from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
        compute_dossier_strategy_pattern_audit,
    )

    result = compute_dossier_strategy_pattern_audit(
        [_dossier("0x1"), _dossier("0x2")],
        min_supporting_wallets=2,
    )

    assert result["theses"] == []
    assert result["rejected_theses"] == []
    assert "NO_THESIS_CANDIDATES_IN_DOSSIERS" in result["top_level_caveats"]


def test_dossier_audit_consolidates_data_gaps() -> None:
    from sniper.analysis.strategy_pattern_audit.dossier_adapter import (
        compute_dossier_strategy_pattern_audit,
    )

    gap = {
        "gap_name": "market_selection_universe_baseline_missing",
        "affected_grain_count": 4,
        "severity": "high",
        "affected_columns": json.dumps(["mechanism_evidence.market_selection_driven"]),
    }
    result = compute_dossier_strategy_pattern_audit(
        [_dossier("0x1", gap=gap), _dossier("0x2", gap=gap)],
        min_supporting_wallets=2,
    )

    assert result["data_gaps"] == [
        {
            "gap_name": "market_selection_universe_baseline_missing",
            "affected_wallets": ["0x1", "0x2"],
            "n_affected_wallets": 2,
            "severity": "high",
            "affected_grain_count": 8,
        }
    ]
