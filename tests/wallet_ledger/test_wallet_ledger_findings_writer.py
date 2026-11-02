"""Slice 6 Task 5 — findings writer + supersede helper tests.

Three callsites:

* `insert_findings(report, wallet, run_id, window_id, supabase)` —
  derives via Task 4, then issues one `insert_ignore` call carrying
  every finding the deriver returned. Returns the count of NEW rows
  inserted (the second pass returns 0).
* `mark_superseded(table=..., natural_key=..., superseded_by=...,
  supabase=...)` — PATCHes `status` on every active row matching
  `natural_key`. `artifact_registry` is rejected with `ValueError`
  (D3 — artifacts are append-only).
* `select_active_findings(wallet, supabase)` — emits a `select` call
  with `where={"wallet": wallet, "status": "active"}`.

Module guard: writer file does not import `bot.supabase_helper`,
`urllib.request`, `requests`, or `httpx`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.findings import (
    insert_findings,
    mark_superseded,
    select_active_findings,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FINDINGS_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "findings.py"
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _edge_report(wallet: str = "0xa") -> ProtocolReport:
    held = BranchReport(
        branch="held_analyst",
        is_active=True,
        hypotheses=[
            HypothesisResult(
                name="H2",
                verdict="supported",
                n=200,
                ci_lower=0.05,
                ci_upper=0.20,
                p_value_bh_adjusted=0.02,
            ),
            HypothesisResult(
                name="H3",
                verdict="supported",
                n=180,
                ci_lower=0.03,
                ci_upper=0.18,
                p_value_bh_adjusted=0.04,
            ),
        ],
    )
    return ProtocolReport(
        wallet=wallet,
        n_closed_positions=200,
        overall_verdict="edge_detected",
        gate_result="pass",
        trading_mode="held",
        held_analyst_branch=held,
    )


def _no_edge_report(wallet: str = "0xa") -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        n_closed_positions=200,
        overall_verdict="no_edge",
        gate_result="pass",
        trading_mode="held",
    )


def _unsupported_accounting_report(wallet: str = "0xa") -> ProtocolReport:
    report = _no_edge_report(wallet)
    report.data_quality_flags = [
        "PORTFOLIO_VALUE_OPEN_MARK_OK: portfolio_value=$1,372,811.98 "
        "open_mark=$1,372,802.54 implied_idle_usdc=$9.44 "
        "delta_pct=0.00% observed_at=1777673439",
        "ACCOUNTING_TIE_UNSUPPORTED: lifecycle_estimate=$765,130.95 "
        "portfolio_value=$1,372,811.98 delta=$-607,681.03 "
        "delta_pct=44.27% (portfolio_value is current holdings, "
        "not lifetime equity)",
    ]
    return report


# ---------------------------------------------------------------------------
# insert_findings — create path
# ---------------------------------------------------------------------------


def test_insert_findings_writes_one_row_per_derived_finding() -> None:
    stub = StubSupabaseHttp()
    n = insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    rows = stub.rows_by_table["wallet_findings"]
    assert n == 2  # two supported hypotheses → two findings.
    assert len(rows) == 2
    assert {r["finding_type"] for r in rows} == {"supported_hypothesis"}
    assert {r["source_hypothesis_id"] for r in rows} == {"H2", "H3"}
    assert {r["wallet"] for r in rows} == {"0xa"}
    assert {r["run_id"] for r in rows} == {"r1"}
    assert {r["window_id"] for r in rows} == {"w1"}
    assert {r["status"] for r in rows} == {"active"}


def test_insert_findings_records_call_log_entry() -> None:
    stub = StubSupabaseHttp()
    insert_findings(
        _no_edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    insert_calls = [c for c in stub.calls if c[0] == "insert_ignore"]
    assert len(insert_calls) == 1
    _, table, _ = insert_calls[0]
    assert table == "wallet_findings"


def test_insert_findings_is_idempotent_per_natural_key() -> None:
    """Re-running with the same (wallet, run_id, window_id) appends
    another insert_ignore call to `calls` but returns 0 (no new rows)
    and leaves `rows_by_table["wallet_findings"]` length unchanged."""
    stub = StubSupabaseHttp()
    first = insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    second = insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    assert first == 2
    assert second == 0
    assert len(stub.rows_by_table["wallet_findings"]) == 2
    insert_calls = [c for c in stub.calls if c[0] == "insert_ignore"]
    # Stub logs one entry per row in the request body. Two findings
    # × two passes = 4 entries; the row store still only carries 2.
    assert len(insert_calls) == 4


def test_insert_findings_persists_numeric_evidence() -> None:
    stub = StubSupabaseHttp()
    insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    rows = stub.rows_by_table["wallet_findings"]
    h2 = next(r for r in rows if r["source_hypothesis_id"] == "H2")
    assert h2["numeric_evidence_json"]["hypothesis"] == "H2"
    assert h2["numeric_evidence_json"]["n"] == 200
    assert h2["finding_schema_version"] == 1


def test_insert_findings_surfaces_unsupported_accounting_flag() -> None:
    stub = StubSupabaseHttp()
    insert_findings(
        _unsupported_accounting_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    rows = stub.rows_by_table["wallet_findings"]
    accounting = [
        r for r in rows if r["finding_type"] == "accounting_tie_unsupported"
    ]
    assert len(accounting) == 1
    assert accounting[0]["severity"] == "warn"
    assert "ACCOUNTING_TIE_UNSUPPORTED" in accounting[0]["numeric_evidence_json"]["flag"]


# ---------------------------------------------------------------------------
# mark_superseded
# ---------------------------------------------------------------------------


def test_mark_superseded_patches_findings_table() -> None:
    stub = StubSupabaseHttp()
    insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    touched = mark_superseded(
        table="wallet_findings",
        natural_key={"wallet": "0xa", "run_id": "r1", "window_id": "w1"},
        superseded_by="finding-uid-cleanup",
        supabase=stub,
    )
    assert touched == 2
    rows = stub.rows_by_table["wallet_findings"]
    for row in rows:
        assert row["status"] == "superseded"
        assert row["superseded_by_finding_uid"] == "finding-uid-cleanup"

    patch_calls = [c for c in stub.calls if c[0] == "patch"]
    assert patch_calls
    last = patch_calls[-1][2]
    assert last["where"] == {
        "wallet": "0xa",
        "run_id": "r1",
        "window_id": "w1",
    }
    assert last["set"]["status"] == "superseded"
    assert last["set"]["superseded_by_finding_uid"] == "finding-uid-cleanup"
    # `updated_at` is set by the helper — value must be present.
    assert "updated_at" in last["set"]


def test_mark_superseded_patches_results_table() -> None:
    """Same helper handles wallet_analysis_results, with the matching
    `superseded_by_run_id` column instead of `superseded_by_finding_uid`."""
    stub = StubSupabaseHttp()
    stub.insert_ignore(
        "wallet_analysis_results",
        [
            {
                "wallet": "0xa",
                "run_id": "r1",
                "window_id": "w1",
                "status": "active",
            }
        ],
        natural_key_columns=("wallet", "run_id", "window_id"),
    )
    touched = mark_superseded(
        table="wallet_analysis_results",
        natural_key={"wallet": "0xa", "run_id": "r1", "window_id": "w1"},
        superseded_by="r2",
        supabase=stub,
    )
    assert touched == 1
    row = stub.rows_by_table["wallet_analysis_results"][0]
    assert row["status"] == "superseded"
    assert row["superseded_by_run_id"] == "r2"


def test_mark_superseded_rejects_artifact_registry() -> None:
    stub = StubSupabaseHttp()
    with pytest.raises(ValueError):
        mark_superseded(
            table="artifact_registry",
            natural_key={"content_hash": "deadbeef"},
            superseded_by="x",
            supabase=stub,
        )


def test_mark_superseded_rejects_unknown_table() -> None:
    stub = StubSupabaseHttp()
    with pytest.raises(ValueError):
        mark_superseded(
            table="some_other_table",
            natural_key={"wallet": "0xa"},
            superseded_by="x",
            supabase=stub,
        )


# ---------------------------------------------------------------------------
# select_active_findings
# ---------------------------------------------------------------------------


def test_select_active_findings_filters_status_active() -> None:
    stub = StubSupabaseHttp()
    insert_findings(
        _edge_report(),
        wallet="0xa",
        run_id="r1",
        window_id="w1",
        supabase=stub,
    )
    # Supersede one of them via a manual patch by finding_uid.
    rows = stub.rows_by_table["wallet_findings"]
    target_uid = rows[0]["finding_uid"]
    stub.patch(
        "wallet_findings",
        where={"finding_uid": target_uid},
        set_columns={"status": "superseded"},
    )

    active = select_active_findings(wallet="0xa", supabase=stub)
    assert len(active) == 1
    assert active[0]["status"] == "active"

    # Recorded select call uses the documented filter shape.
    select_calls = [c for c in stub.calls if c[0] == "select"]
    last = select_calls[-1][2]
    assert last["where"] == {"wallet": "0xa", "status": "active"}


# ---------------------------------------------------------------------------
# Module guard
# ---------------------------------------------------------------------------


def test_findings_module_does_not_import_live_supabase_modules() -> None:
    tree = ast.parse(FINDINGS_FILE.read_text())
    forbidden_roots = {"bot", "requests", "httpx"}
    forbidden_modules = {"urllib.request"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden_roots, (
                    f"findings imported forbidden root: {alias.name}"
                )
                assert alias.name not in forbidden_modules, (
                    f"findings imported forbidden module: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in forbidden_roots, (
                f"findings imported forbidden root: {node.module}"
            )
            assert node.module not in forbidden_modules, (
                f"findings imported forbidden module: {node.module}"
            )
