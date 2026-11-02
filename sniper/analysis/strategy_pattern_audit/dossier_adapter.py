"""Strategy Pattern Audit adapter for wallet dossier tables."""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


SURFACE_VERSION = "strategy_pattern_audit_dossier_v1"
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def compute_dossier_strategy_pattern_audit(
    dossiers: Sequence[Any],
    *,
    audit_run_id: str | None = None,
    min_supporting_wallets: int = 2,
) -> dict[str, Any] | None:
    """Aggregate durable per-wallet dossier thesis candidates.

    This adapter intentionally ignores raw metric-supported mechanism rows.
    A cross-wallet thesis can only start from a per-wallet
    ``thesis_candidates`` row that already passed dossier honesty gates.
    """

    dossier_rows = [_as_tables(d) for d in dossiers]
    if not dossier_rows:
        return None

    min_supporting_wallets = max(int(min_supporting_wallets), 2)
    candidate_groups = _candidate_groups(dossier_rows)
    theses: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in candidate_groups:
        passed = row["n_supporting_wallets"] >= min_supporting_wallets
        row["honesty_bar"] = {
            "min_supporting_wallets": min_supporting_wallets,
            "passed": passed,
            "fail_reasons": [] if passed else ["support_floor"],
        }
        if passed:
            theses.append(row)
        else:
            rejected.append(row)

    return {
        "audit_run_id": audit_run_id,
        "surface_version": SURFACE_VERSION,
        "generated_from": {
            "n_dossiers": len(dossier_rows),
            "schema_versions": _schema_versions(dossier_rows),
        },
        "theses": theses,
        "rejected_theses": rejected,
        "data_gaps": _consolidate_data_gaps(dossier_rows),
        "top_level_caveats": []
        if candidate_groups
        else ["NO_THESIS_CANDIDATES_IN_DOSSIERS"],
    }


def _as_tables(dossier: Any) -> Mapping[str, Any]:
    if isinstance(dossier, Mapping):
        return dossier
    return {
        "dossier_meta": getattr(dossier, "dossier_meta", []),
        "result_semantics": getattr(dossier, "result_semantics", []),
        "mechanism_evidence": getattr(dossier, "mechanism_evidence", []),
        "thesis_candidates": getattr(dossier, "thesis_candidates", []),
        "data_gaps": getattr(dossier, "data_gaps", []),
    }


def _meta(tables: Mapping[str, Any], key: str) -> str | None:
    for row in tables.get("dossier_meta") or []:
        if row.get("key") == key:
            return row.get("value")
    return None


def _schema_versions(dossiers: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for tables in dossiers:
        version = _meta(tables, "schema_version") or "unknown"
        out[version] = out.get(version, 0) + 1
    return dict(sorted(out.items()))


def _candidate_groups(dossiers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for tables in dossiers:
        wallet = _meta(tables, "wallet") or "unknown"
        grains = {
            str(r.get("grain_id")): _load_json_object(r.get("grain_key"))
            for r in tables.get("result_semantics") or []
        }
        for row in tables.get("thesis_candidates") or []:
            grain_id = str(row.get("grain_id"))
            mechanism = str(row.get("mechanism"))
            grain_key = grains.get(grain_id) or {}
            group_key = json.dumps(
                {"mechanism": mechanism, "grain_key": grain_key},
                sort_keys=True,
            )
            entry = groups.setdefault(
                group_key,
                {
                    "mechanism": mechanism,
                    "grain_key": grain_key,
                    "n_supporting_wallets": 0,
                    "supporting_wallets": [],
                    "thesis_texts": [],
                    "required_test_data": [],
                    "source_thesis_ids": [],
                },
            )
            if wallet not in entry["supporting_wallets"]:
                entry["supporting_wallets"].append(wallet)
                entry["n_supporting_wallets"] += 1
            text = row.get("thesis_text")
            if text and text not in entry["thesis_texts"]:
                entry["thesis_texts"].append(text)
            for item in _load_json_list(row.get("required_test_data")):
                if item not in entry["required_test_data"]:
                    entry["required_test_data"].append(item)
            thesis_id = row.get("thesis_id")
            if thesis_id:
                entry["source_thesis_ids"].append(thesis_id)

    out = []
    for entry in groups.values():
        entry["supporting_wallets"] = sorted(entry["supporting_wallets"])
        entry["thesis_texts"] = sorted(entry["thesis_texts"])
        entry["required_test_data"] = sorted(entry["required_test_data"])
        entry["source_thesis_ids"] = sorted(entry["source_thesis_ids"])
        out.append(entry)
    return sorted(
        out,
        key=lambda row: (
            row["mechanism"],
            json.dumps(row["grain_key"], sort_keys=True),
        ),
    )


def _consolidate_data_gaps(dossiers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    gaps: dict[str, dict[str, Any]] = {}
    for tables in dossiers:
        wallet = _meta(tables, "wallet") or "unknown"
        for row in tables.get("data_gaps") or []:
            name = row.get("gap_name")
            if not name:
                continue
            gap = gaps.setdefault(
                str(name),
                {
                    "gap_name": str(name),
                    "affected_wallets": [],
                    "n_affected_wallets": 0,
                    "severity": "low",
                    "affected_grain_count": 0,
                },
            )
            if wallet not in gap["affected_wallets"]:
                gap["affected_wallets"].append(wallet)
                gap["n_affected_wallets"] += 1
            severity = str(row.get("severity") or "low")
            if _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER.get(gap["severity"], 0):
                gap["severity"] = severity
            gap["affected_grain_count"] += int(row.get("affected_grain_count") or 0)
    for gap in gaps.values():
        gap["affected_wallets"] = sorted(gap["affected_wallets"])
    return sorted(
        gaps.values(),
        key=lambda row: (-row["n_affected_wallets"], row["gap_name"]),
    )


def _load_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    parsed = json.loads(str(value))
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _load_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed]
