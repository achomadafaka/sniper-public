"""Slice 6 Task 4 — findings deriver tests.

Pure-function coverage of D8's matrix. Hand-built `ProtocolReport`
inputs, hand-checked finding outputs. The deriver does no I/O.

D8 matrix:

  Stage 1 reject (any reason)         -> data_insufficient (info)
  overall_verdict='no_edge'           -> no_edge (info)
  overall_verdict='underpowered'      -> underpowered (info)
  overall_verdict='operator_review'   -> operator_review (warn)
  overall_verdict='edge_detected'     -> 1 finding per supported hypothesis (insight)
  any branch hypothesis underpowered with reason='market_structure_unsupported'
                                      -> unsupported_market_structure (warn)
  any branch hypothesis underpowered with reason='outcome_index_missing'
                                      -> outcome_index_missing (warn)

Every analysis emits >= 1 finding. The matrix rows compose (a wallet
that is `edge_detected` AND has a market_structure_unsupported
underpowered hypothesis emits both an insight finding and a warn
finding).

`finding_uid = sha256(wallet | run_id | window_id | finding_type |
source_hypothesis_id)` is stable across re-runs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from sniper.analysis.wallet_protocol.ledger.findings import (
    Finding,
    derive_findings_from_report,
)
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _report(
    *,
    wallet: str = "0xa",
    overall: str = "underpowered",
    gate_result: str = "pass",
    n_closed: int = 50,
    held_branch: BranchReport | None = None,
    flipper_branch: BranchReport | None = None,
) -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        n_closed_positions=n_closed,
        overall_verdict=overall,
        gate_result=gate_result,
        trading_mode="held",
        held_analyst_branch=held_branch,
        flipper_branch=flipper_branch,
    )


def _branch(*hypotheses: HypothesisResult) -> BranchReport:
    return BranchReport(
        branch="held_analyst",
        is_active=True,
        hypotheses=list(hypotheses),
    )


def _supported(name: str, **details: Any) -> HypothesisResult:
    return HypothesisResult(
        name=name,
        verdict="supported",
        n=200,
        ci_lower=0.05,
        ci_upper=0.20,
        p_value_bh_adjusted=0.02,
        details=dict(details),
    )


def _underpowered(
    name: str, *, reason: str | None = None
) -> HypothesisResult:
    return HypothesisResult(
        name=name,
        verdict="underpowered",
        n=8,
        underpowered_reason=reason,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# D8 matrix — happy paths
# ---------------------------------------------------------------------------


def test_stage1_reject_emits_data_insufficient_info_finding() -> None:
    report = _report(
        overall="data_insufficient",
        gate_result="reject_insufficient_sample",
        n_closed=2,
    )
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.finding_type == "data_insufficient"
    assert f.severity == "info"
    assert f.source_hypothesis_id is None
    # numeric_evidence carries indexed summary fields.
    assert f.numeric_evidence_json["gate_result"] == (
        "reject_insufficient_sample"
    )
    assert f.numeric_evidence_json["n_closed_positions"] == 2


def test_no_edge_emits_no_edge_info_finding() -> None:
    report = _report(overall="no_edge")
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert [f.finding_type for f in findings] == ["no_edge"]
    assert findings[0].severity == "info"


def test_underpowered_emits_underpowered_info_finding() -> None:
    report = _report(overall="underpowered")
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert [f.finding_type for f in findings] == ["underpowered"]
    assert findings[0].severity == "info"


def test_operator_review_emits_warn_finding() -> None:
    report = _report(overall="operator_review")
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert [f.finding_type for f in findings] == ["operator_review"]
    assert findings[0].severity == "warn"


def test_edge_detected_emits_one_finding_per_supported_hypothesis() -> None:
    held = _branch(
        _supported("H2", excess_pp=12.0),
        _supported("H3", excess_pp=8.5),
    )
    flipper = BranchReport(
        branch="flipper",
        is_active=True,
        hypotheses=[_supported("FH2", excess_pp=4.2)],
    )
    report = _report(
        overall="edge_detected",
        held_branch=held,
        flipper_branch=flipper,
    )
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    types = [f.finding_type for f in findings]
    severities = {f.severity for f in findings}
    assert types.count("supported_hypothesis") == 3
    assert severities == {"insight"}
    sources = sorted(f.source_hypothesis_id for f in findings)
    assert sources == ["FH2", "H2", "H3"]


def test_market_structure_unsupported_emits_warn_finding() -> None:
    """Market-structure guard fires when a binary-only hypothesis sees
    no binary rows. The finding rides alongside whatever overall
    verdict the report carries."""
    held = _branch(
        _underpowered("H2", reason="market_structure_unsupported"),
    )
    report = _report(overall="underpowered", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    types = sorted(f.finding_type for f in findings)
    assert types == ["underpowered", "unsupported_market_structure"]
    structure = next(
        f for f in findings if f.finding_type == "unsupported_market_structure"
    )
    assert structure.severity == "warn"
    assert structure.source_hypothesis_id == "H2"


def test_outcome_index_missing_emits_warn_finding() -> None:
    held = _branch(
        _underpowered("H3", reason="outcome_index_missing"),
    )
    report = _report(overall="underpowered", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    types = sorted(f.finding_type for f in findings)
    assert types == ["outcome_index_missing", "underpowered"]
    oi = next(f for f in findings if f.finding_type == "outcome_index_missing")
    assert oi.severity == "warn"
    assert oi.source_hypothesis_id == "H3"


def test_market_structure_in_flipper_branch_is_also_emitted() -> None:
    flipper = BranchReport(
        branch="flipper",
        is_active=True,
        hypotheses=[
            _underpowered("FH2", reason="market_structure_unsupported"),
            _underpowered("FH3", reason="outcome_index_missing"),
        ],
    )
    report = _report(overall="underpowered", flipper_branch=flipper)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    structure = [
        f for f in findings if f.finding_type == "unsupported_market_structure"
    ]
    oi = [
        f for f in findings if f.finding_type == "outcome_index_missing"
    ]
    assert {f.source_hypothesis_id for f in structure} == {"FH2"}
    assert {f.source_hypothesis_id for f in oi} == {"FH3"}


def test_other_underpowered_reasons_do_not_create_warn_findings() -> None:
    """Only `market_structure_unsupported` and `outcome_index_missing`
    trigger a structure-level warn; n_below_threshold is already
    captured by the top-level `underpowered` finding."""
    held = _branch(_underpowered("H2", reason="n_below_threshold"))
    report = _report(overall="underpowered", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    types = sorted(f.finding_type for f in findings)
    assert types == ["underpowered"]


# ---------------------------------------------------------------------------
# Coverage invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overall",
    [
        "data_insufficient",
        "no_edge",
        "underpowered",
        "operator_review",
    ],
)
def test_every_analysis_emits_at_least_one_finding(overall: str) -> None:
    report = _report(overall=overall)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert len(findings) >= 1


# ---------------------------------------------------------------------------
# finding_uid stability
# ---------------------------------------------------------------------------


def test_finding_uid_is_sha256_hex_and_stable_across_reruns() -> None:
    held = _branch(_supported("H2"))
    report = _report(overall="edge_detected", held_branch=held)

    first = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    second = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    assert [f.finding_uid for f in first] == [f.finding_uid for f in second]
    for f in first:
        # SHA256 hex is 64 chars of [0-9a-f].
        assert len(f.finding_uid) == 64
        int(f.finding_uid, 16)  # raises ValueError if not hex


def test_finding_uid_distinguishes_finding_type_and_source() -> None:
    held = _branch(
        _supported("H2"),
        _supported("H3"),
    )
    report = _report(overall="edge_detected", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    uids = {f.finding_uid for f in findings}
    assert len(uids) == len(findings)


def test_finding_uid_matches_documented_recipe() -> None:
    """`finding_uid = sha256(wallet | run_id | window_id | finding_type
    | source_hypothesis_id)`. Pin the literal recipe so re-derived IDs
    stay stable across releases."""
    held = _branch(_supported("H2"))
    report = _report(overall="edge_detected", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xabc", run_id="r1", window_id="w1"
    )
    f = findings[0]
    expected = hashlib.sha256(
        "0xabc|r1|w1|supported_hypothesis|H2".encode("utf-8")
    ).hexdigest()
    assert f.finding_uid == expected


# ---------------------------------------------------------------------------
# Serialisability
# ---------------------------------------------------------------------------


def test_numeric_evidence_json_is_json_serialisable() -> None:
    held = _branch(_supported("H2", excess_pp=11.0))
    report = _report(overall="edge_detected", held_branch=held)
    findings = derive_findings_from_report(
        report, wallet="0xa", run_id="r1", window_id="w1"
    )
    for f in findings:
        # round-trips cleanly through json
        json.loads(json.dumps(f.numeric_evidence_json))


def test_finding_dataclass_carries_required_fields() -> None:
    findings = derive_findings_from_report(
        _report(overall="no_edge"),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
    )
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.wallet == "0xa"
    assert f.run_id == "r1"
    assert f.window_id == "w1"
    assert f.finding_type == "no_edge"
    assert f.severity in ("info", "warn", "insight")
    assert isinstance(f.numeric_evidence_json, dict)
    # Schema version pin per D8.
    assert f.finding_schema_version == 1
