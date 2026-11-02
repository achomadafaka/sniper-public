"""Deterministic validation workflow for promoted strategy theses."""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping, Sequence


SURFACE_VERSION = "strategy_thesis_validation_workflow_v1"


def build_thesis_validation_workflow(
    audit_catalog: Mapping[str, Any],
    *,
    dataset_inventory: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Name the offline validation work required for promoted theses.

    This is a workflow planner, not a simulator. It does not claim a thesis is
    deployable and it never emits live signals, triggers, sizing, or expected
    PnL.
    """

    inventory = {str(item) for item in (dataset_inventory or [])}
    theses = list(audit_catalog.get("theses") or [])
    plans = [_plan_for_thesis(thesis, inventory) for thesis in theses]
    return {
        "surface_version": SURFACE_VERSION,
        "source_surface_version": audit_catalog.get("surface_version"),
        "validation_plans": plans,
        "data_gaps": list(audit_catalog.get("data_gaps") or []),
        "top_level_caveats": []
        if plans
        else ["NO_PROMOTED_THESES_TO_VALIDATE"],
    }


def _plan_for_thesis(
    thesis: Mapping[str, Any],
    dataset_inventory: set[str],
) -> dict[str, Any]:
    required = sorted(_as_list(thesis.get("required_test_data")))
    missing = sorted(item for item in required if item not in dataset_inventory)
    ready = not missing
    thesis_key = _thesis_key(thesis)
    return {
        "thesis_key": thesis_key,
        "mechanism": thesis.get("mechanism"),
        "grain_key": thesis.get("grain_key") or {},
        "supporting_wallets": sorted(_as_list(thesis.get("supporting_wallets"))),
        "required_test_data": required,
        "missing_test_data": missing,
        "status": "simulation_ready" if ready else "needs_data",
        "workflow_steps": [
            {
                "name": "dataset_build",
                "status": "ready" if ready else "blocked",
                "requires": required,
            },
            {
                "name": "offline_replay_simulation",
                "status": "ready" if ready else "blocked_until_dataset_build",
                "requires": ["timestamp_valid_replay", "fees_slippage_model"],
            },
            {
                "name": "paper_validation",
                "status": "blocked_until_simulation_review",
                "requires": ["operator_reviewed_simulation"],
            },
            {
                "name": "live_validation",
                "status": "out_of_scope",
                "requires": ["separate_operator_authorization"],
            },
        ],
    }


def _thesis_key(thesis: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {
            "mechanism": thesis.get("mechanism"),
            "grain_key": thesis.get("grain_key") or {},
            "supporting_wallets": sorted(_as_list(thesis.get("supporting_wallets"))),
        },
        sort_keys=True,
    )
    return sha256(raw.encode("utf-8")).hexdigest()[:24]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]
