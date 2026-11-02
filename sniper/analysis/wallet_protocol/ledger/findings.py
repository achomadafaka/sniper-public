"""Slice 6 Task 4 / Task 5 — structured findings derived from each analysis.

Task 4 covers the **deriver** half: a pure function that turns a
`ProtocolReport` into one or more `Finding` records per the D8 matrix.
The writer half (`upsert_findings`, `mark_superseded`,
`select_active_findings`) lands in Task 5.

D8 matrix (every analysis emits >= 1 finding):

| ProtocolReport state                                  | finding_type                    | severity |
|-------------------------------------------------------|---------------------------------|----------|
| Stage 1 reject (any reason)                           | data_insufficient               | info     |
| overall_verdict='no_edge'                             | no_edge                         | info     |
| overall_verdict='underpowered'                        | underpowered                    | info     |
| overall_verdict='operator_review'                     | operator_review                 | warn     |
| overall_verdict='edge_detected'                       | supported_hypothesis (per H)    | insight  |
| underpowered + reason='market_structure_unsupported'  | unsupported_market_structure    | warn     |
| underpowered + reason='outcome_index_missing'         | outcome_index_missing           | warn     |

Structural warns (last two rows) ride alongside whatever overall
verdict the report carries — they describe a per-hypothesis state.

`finding_uid = sha256(wallet | run_id | window_id | finding_type |
source_hypothesis_id)`.  Re-derivation is byte-identical.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping

from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
)


_SCHEMA_VERSION = 1
_SUPERSEDED_NEEDS_INSIGHT = "supported_hypothesis"


Severity = Literal["info", "warn", "insight"]


@dataclass(frozen=True)
class Finding:
    """One structured finding derived from a `ProtocolReport`.

    `finding_uid` is sha256-hex of the natural-key tuple plus
    `finding_type` and `source_hypothesis_id` so re-derivation is a
    no-op.
    """

    finding_uid: str
    wallet: str
    run_id: str
    window_id: str
    finding_type: str
    severity: Severity
    numeric_evidence_json: dict[str, Any]
    source_hypothesis_id: str | None = None
    confidence: str | None = None
    notes: list[str] = field(default_factory=list)
    finding_schema_version: int = _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_findings_from_report(
    report: ProtocolReport,
    *,
    wallet: str,
    run_id: str,
    window_id: str,
) -> list[Finding]:
    """Pure mapping from `ProtocolReport` to one-or-more `Finding`s.

    No I/O. Inputs in, findings out.
    """
    findings: list[Finding] = []

    # Top-level finding driven by overall_verdict / gate_result.
    findings.extend(
        _top_level_findings(report, wallet, run_id, window_id)
    )

    # Per-hypothesis structural findings (warn-level) — orthogonal to
    # the top-level finding. Survives across all overall verdicts so
    # consumers can flag wallets whose underpowered status was
    # structural rather than sample-size driven.
    findings.extend(
        _structural_findings(report, wallet, run_id, window_id)
    )
    findings.extend(
        _data_quality_findings(report, wallet, run_id, window_id)
    )

    return findings


# ---------------------------------------------------------------------------
# Top-level findings
# ---------------------------------------------------------------------------


def _top_level_findings(
    report: ProtocolReport,
    wallet: str,
    run_id: str,
    window_id: str,
) -> list[Finding]:
    overall = report.overall_verdict
    summary = _indexed_summary(report)

    if overall == "data_insufficient":
        return [
            _make_finding(
                wallet=wallet,
                run_id=run_id,
                window_id=window_id,
                finding_type="data_insufficient",
                severity="info",
                numeric_evidence={
                    **summary,
                    "gate_reason": report.gate_reason,
                },
            )
        ]
    if overall == "no_edge":
        return [
            _make_finding(
                wallet=wallet,
                run_id=run_id,
                window_id=window_id,
                finding_type="no_edge",
                severity="info",
                numeric_evidence=summary,
            )
        ]
    if overall == "underpowered":
        return [
            _make_finding(
                wallet=wallet,
                run_id=run_id,
                window_id=window_id,
                finding_type="underpowered",
                severity="info",
                numeric_evidence=summary,
            )
        ]
    if overall == "operator_review":
        return [
            _make_finding(
                wallet=wallet,
                run_id=run_id,
                window_id=window_id,
                finding_type="operator_review",
                severity="warn",
                numeric_evidence=summary,
            )
        ]
    if overall == "edge_detected":
        return [
            _make_finding(
                wallet=wallet,
                run_id=run_id,
                window_id=window_id,
                finding_type=_SUPERSEDED_NEEDS_INSIGHT,
                severity="insight",
                numeric_evidence={
                    **summary,
                    "hypothesis": h.name,
                    "ci_lower": h.ci_lower,
                    "ci_upper": h.ci_upper,
                    "p_value_bh_adjusted": h.p_value_bh_adjusted,
                    "n": h.n,
                    "details": dict(h.details),
                },
                source_hypothesis_id=h.name,
            )
            for h in _supported_hypotheses(report)
        ]

    # Defensive fallthrough — every overall_verdict literal is enumerated
    # above. If a new literal is added without a matching matrix row
    # the deriver should still emit one finding so the row is auditable.
    return [
        _make_finding(
            wallet=wallet,
            run_id=run_id,
            window_id=window_id,
            finding_type=f"unknown_overall_verdict:{overall}",
            severity="warn",
            numeric_evidence=summary,
        )
    ]


# ---------------------------------------------------------------------------
# Structural per-hypothesis findings
# ---------------------------------------------------------------------------


_STRUCTURAL_REASONS: dict[str, str] = {
    "market_structure_unsupported": "unsupported_market_structure",
    "outcome_index_missing": "outcome_index_missing",
}


def _structural_findings(
    report: ProtocolReport,
    wallet: str,
    run_id: str,
    window_id: str,
) -> list[Finding]:
    out: list[Finding] = []
    for branch in _branches(report):
        for h in branch.hypotheses:
            if h.verdict != "underpowered" or h.underpowered_reason is None:
                continue
            finding_type = _STRUCTURAL_REASONS.get(h.underpowered_reason)
            if finding_type is None:
                continue
            out.append(
                _make_finding(
                    wallet=wallet,
                    run_id=run_id,
                    window_id=window_id,
                    finding_type=finding_type,
                    severity="warn",
                    numeric_evidence={
                        "branch": branch.branch,
                        "hypothesis": h.name,
                        "underpowered_reason": h.underpowered_reason,
                        "n": h.n,
                    },
                    source_hypothesis_id=h.name,
                )
            )
    return out


def _data_quality_findings(
    report: ProtocolReport,
    wallet: str,
    run_id: str,
    window_id: str,
) -> list[Finding]:
    out: list[Finding] = []
    for flag in report.data_quality_flags:
        if flag.startswith("ACCOUNTING_TIE_UNSUPPORTED"):
            out.append(
                _make_finding(
                    wallet=wallet,
                    run_id=run_id,
                    window_id=window_id,
                    finding_type="accounting_tie_unsupported",
                    severity="warn",
                    numeric_evidence={
                        **_indexed_summary(report),
                        "flag": flag,
                    },
                )
            )
        elif flag.startswith("PORTFOLIO_VALUE_OPEN_MARK_FAILED"):
            out.append(
                _make_finding(
                    wallet=wallet,
                    run_id=run_id,
                    window_id=window_id,
                    finding_type="portfolio_value_open_mark_failed",
                    severity="warn",
                    numeric_evidence={
                        **_indexed_summary(report),
                        "flag": flag,
                    },
                )
            )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _branches(report: ProtocolReport) -> Iterable[BranchReport]:
    if report.held_analyst_branch is not None:
        yield report.held_analyst_branch
    if report.flipper_branch is not None:
        yield report.flipper_branch


def _supported_hypotheses(
    report: ProtocolReport,
) -> list[HypothesisResult]:
    out: list[HypothesisResult] = []
    for branch in _branches(report):
        for h in branch.hypotheses:
            if h.verdict == "supported":
                out.append(h)
    return out


def _indexed_summary(report: ProtocolReport) -> dict[str, Any]:
    return {
        "overall_verdict": report.overall_verdict,
        "gate_result": report.gate_result,
        "trading_mode": report.trading_mode,
        "n_closed_positions": report.n_closed_positions,
        "n_held_to_resolution": report.n_held_to_resolution,
        "net_pnl": report.net_pnl,
        "dw_roi_point": report.dw_roi_point,
        "ew_roi_point": report.ew_roi_point,
        "primary_edge_type": report.primary_edge_type,
    }


def _finding_uid(
    *,
    wallet: str,
    run_id: str,
    window_id: str,
    finding_type: str,
    source_hypothesis_id: str | None,
) -> str:
    payload = "|".join(
        [
            wallet,
            run_id,
            window_id,
            finding_type,
            source_hypothesis_id or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_finding(
    *,
    wallet: str,
    run_id: str,
    window_id: str,
    finding_type: str,
    severity: Severity,
    numeric_evidence: dict[str, Any],
    source_hypothesis_id: str | None = None,
) -> Finding:
    return Finding(
        finding_uid=_finding_uid(
            wallet=wallet,
            run_id=run_id,
            window_id=window_id,
            finding_type=finding_type,
            source_hypothesis_id=source_hypothesis_id,
        ),
        wallet=wallet,
        run_id=run_id,
        window_id=window_id,
        finding_type=finding_type,
        severity=severity,
        numeric_evidence_json=dict(numeric_evidence),
        source_hypothesis_id=source_hypothesis_id,
    )


# ---------------------------------------------------------------------------
# Writer half — Task 5
# ---------------------------------------------------------------------------


_FINDINGS_TABLE = "wallet_findings"
_RESULTS_TABLE = "wallet_analysis_results"
_SUPERSEDABLE_TABLES = (_FINDINGS_TABLE, _RESULTS_TABLE)
_SUPERSEDED_BY_COLUMN = {
    _FINDINGS_TABLE: "superseded_by_finding_uid",
    _RESULTS_TABLE: "superseded_by_run_id",
}


def insert_findings(
    report: ProtocolReport,
    *,
    wallet: str,
    run_id: str,
    window_id: str,
    supabase: SupabaseHttp,
) -> int:
    """Derive findings from `report` and insert-ignore them.

    Returns the count of NEW rows inserted (the second pass returns 0
    by D2 idempotency).
    """
    findings = derive_findings_from_report(
        report, wallet=wallet, run_id=run_id, window_id=window_id
    )
    if not findings:
        return 0
    rows = [_finding_to_row(f) for f in findings]
    return supabase.insert_ignore(
        _FINDINGS_TABLE, rows, natural_key_columns=("finding_uid",)
    )


def mark_superseded(
    *,
    table: Literal["wallet_analysis_results", "wallet_findings"],
    natural_key: Mapping[str, Any],
    superseded_by: str,
    supabase: SupabaseHttp,
) -> int:
    """PATCH `status='superseded'` on every active row in the natural key.

    `artifact_registry` raises `ValueError` — artifacts are append-only
    (D3) and have no `status` column.
    """
    if table == "artifact_registry":
        raise ValueError(
            "artifact_registry is append-only; mark_superseded does not "
            "apply (D3 — artifact rows have no status column)"
        )
    if table not in _SUPERSEDABLE_TABLES:
        raise ValueError(
            f"mark_superseded does not support table={table!r}; "
            f"valid tables: {_SUPERSEDABLE_TABLES}"
        )

    set_columns: dict[str, Any] = {
        "status": "superseded",
        _SUPERSEDED_BY_COLUMN[table]: superseded_by,
        "updated_at": _utcnow_iso(),
    }
    return supabase.patch(
        table,
        where=dict(natural_key),
        set_columns=set_columns,
    )


def select_active_findings(
    *,
    wallet: str,
    supabase: SupabaseHttp,
) -> list[dict]:
    """Default read — `status='active'` filter applied at the source."""
    return supabase.select(
        _FINDINGS_TABLE,
        where={"wallet": wallet, "status": "active"},
    )


def _finding_to_row(finding: Finding) -> dict[str, Any]:
    row = dataclasses.asdict(finding)
    row["status"] = "active"
    return row


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp matching the Postgres `TIMESTAMPTZ` form
    Supabase accepts in PATCH bodies."""
    # `datetime.now(timezone.utc)` per project rule (no `utcnow()`).
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "Finding",
    "derive_findings_from_report",
    "insert_findings",
    "mark_superseded",
    "select_active_findings",
]
