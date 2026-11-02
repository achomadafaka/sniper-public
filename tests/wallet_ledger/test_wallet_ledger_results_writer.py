"""Slice 6 Task 3 — wallet_analysis_results writer tests.

Pure offline. Drives a synthetic `ProtocolReport` through
`insert_wallet_analysis_result` against `StubSupabaseHttp` and asserts:

* The headline summary columns are populated from the report.
* The full `report_json` payload survives a round-trip through the stub.
* `replay_surface_meta` from the materialized bundle lands on the row
  for provenance.
* Idempotency contract (D2): re-running with the same `(wallet,
  run_id, window_id)` returns 0 and leaves `rows_by_table` length
  unchanged; `calls` length still grows.
* The writer never PATCHes; corrective writes are
  `mark_superseded(...)`'s job (Task 5).
* Module guard: writer module does not import `bot.supabase_helper`,
  `urllib.request`, `requests`, or `httpx`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.results_writer import (
    insert_wallet_analysis_result,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp
from sniper.analysis.wallet_protocol.types import ProtocolReport


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_WRITER_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "results_writer.py"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _synthetic_report(
    *,
    wallet: str = "0xa",
    overall: str = "underpowered",
) -> ProtocolReport:
    return ProtocolReport(
        wallet=wallet,
        analyzed_at=1717000000,
        n_closed_positions=42,
        n_held_to_resolution=10,
        net_pnl=-12.5,
        overall_verdict=overall,
        gate_result="pass",
        trading_mode="held",
        primary_edge_type=None,
        dw_roi_point=0.075,
        dw_roi_ci_lower=0.01,
        dw_roi_ci_upper=0.18,
        ew_roi_point=0.04,
    )


def _replay_surface_meta() -> dict:
    return {
        "events": {
            "ledger_source": "duckdb_warehouse",
            "wallet_path": "/tmp/0xa.duckdb",
            "window_id": "w1",
            "row_count": 5,
        },
        "positions": {
            "ledger_source": "duckdb_warehouse",
            "wallet_path": "/tmp/0xa.duckdb",
            "window_id": "w1",
            "row_count": 7,
        },
    }


# ---------------------------------------------------------------------------
# Headline fields
# ---------------------------------------------------------------------------


def test_writer_populates_headline_summary_columns() -> None:
    stub = StubSupabaseHttp()
    report = _synthetic_report()

    inserted = insert_wallet_analysis_result(
        report=report,
        wallet=report.wallet,
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={"events": "deadbeef", "positions": "cafebabe"},
        artifact_hashes={"wallet_warehouse": "f00d"},
        code_version="abcdef0",
        supabase=stub,
    )

    assert inserted == 1
    rows = stub.rows_by_table["wallet_analysis_results"]
    assert len(rows) == 1
    row = rows[0]

    # Natural key.
    assert row["wallet"] == "0xa"
    assert row["run_id"] == "run-1"
    assert row["window_id"] == "w1"

    # Indexed summary columns.
    assert row["overall_verdict"] == "underpowered"
    assert row["gate_result"] == "pass"
    assert row["trading_mode"] == "held"
    assert row["primary_edge_type"] is None
    assert row["n_closed_positions"] == 42
    assert row["n_held_to_resolution"] == 10
    assert row["net_pnl"] == pytest.approx(-12.5)
    assert row["dw_roi_point"] == pytest.approx(0.075)
    assert row["ew_roi_point"] == pytest.approx(0.04)

    # Provenance.
    assert row["wallet_warehouse_path"] == "/tmp/0xa.duckdb"
    assert row["replay_surface_meta"]["events"]["ledger_source"] == (
        "duckdb_warehouse"
    )
    assert row["manifest_hashes"]["events"] == "deadbeef"
    assert row["artifact_hashes"]["wallet_warehouse"] == "f00d"

    # Versions.
    assert row["protocol_version"] == "v2"
    assert row["code_version"] == "abcdef0"

    # Default status is active (D3).
    assert row["status"] == "active"


def test_writer_persists_full_report_json() -> None:
    """The full `ProtocolReport` payload must survive in `report_json`
    so the durable record carries enough to reconstruct any field the
    Phase 2D layer might want."""
    stub = StubSupabaseHttp()
    report = _synthetic_report()

    insert_wallet_analysis_result(
        report=report,
        wallet=report.wallet,
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={},
        artifact_hashes={},
        code_version="abcdef0",
        supabase=stub,
    )

    row = stub.rows_by_table["wallet_analysis_results"][0]
    rj = row["report_json"]
    # `report_json` is a JSON-serialisable dict, not a dataclass.
    assert isinstance(rj, dict)
    assert rj["wallet"] == "0xa"
    assert rj["overall_verdict"] == "underpowered"
    assert rj["dw_roi_point"] == pytest.approx(0.075)


def test_writer_records_call_log_entry() -> None:
    stub = StubSupabaseHttp()
    insert_wallet_analysis_result(
        report=_synthetic_report(),
        wallet="0xa",
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={},
        artifact_hashes={},
        code_version="abcdef0",
        supabase=stub,
    )
    assert len(stub.calls) == 1
    method, table, _ = stub.calls[0]
    assert method == "insert_ignore"
    assert table == "wallet_analysis_results"


# ---------------------------------------------------------------------------
# Idempotency (D2)
# ---------------------------------------------------------------------------


def test_writer_is_idempotent_on_natural_key() -> None:
    """Re-running with the same (wallet, run_id, window_id) returns 0
    and leaves the row store unchanged. The request log still grows —
    the log records every request, the state store dedups on the
    natural key."""
    stub = StubSupabaseHttp()
    report = _synthetic_report()
    kwargs = dict(
        report=report,
        wallet=report.wallet,
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={},
        artifact_hashes={},
        code_version="abcdef0",
        supabase=stub,
    )

    first = insert_wallet_analysis_result(**kwargs)
    second = insert_wallet_analysis_result(**kwargs)

    assert first == 1
    assert second == 0
    assert len(stub.rows_by_table["wallet_analysis_results"]) == 1
    assert len(stub.calls) == 2


def test_writer_does_not_emit_patch_calls() -> None:
    """Corrective writes are `mark_superseded(...)`'s job, not the
    create-path writer's. Re-inserting must not PATCH the row in
    place — the existing row is left untouched."""
    stub = StubSupabaseHttp()
    report = _synthetic_report()
    insert_wallet_analysis_result(
        report=report,
        wallet=report.wallet,
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={},
        artifact_hashes={},
        code_version="abcdef0",
        supabase=stub,
    )
    # Re-call with a CHANGED payload — a patch-happy writer would try
    # to update the row in place. We assert the writer never patches.
    mutated = _synthetic_report(overall="edge_detected")
    insert_wallet_analysis_result(
        report=mutated,
        wallet=mutated.wallet,
        run_id="run-1",
        window_id="w1",
        wallet_warehouse_path="/tmp/0xa.duckdb",
        replay_surface_meta=_replay_surface_meta(),
        manifest_hashes={},
        artifact_hashes={},
        code_version="abcdef0",
        supabase=stub,
    )

    patch_calls = [c for c in stub.calls if c[0] == "patch"]
    assert patch_calls == []
    # Row stays the original (insert-ignore semantics).
    assert (
        stub.rows_by_table["wallet_analysis_results"][0]["overall_verdict"]
        == "underpowered"
    )


# ---------------------------------------------------------------------------
# Module guard
# ---------------------------------------------------------------------------


def test_results_writer_does_not_import_live_supabase_modules() -> None:
    """Writer modules import only `SupabaseHttp` from `supabase_io`.
    They never reach into `bot.supabase_helper` (env-coupled) or
    network libs directly."""
    tree = ast.parse(RESULTS_WRITER_FILE.read_text())
    forbidden_roots = {"bot", "requests", "httpx"}
    forbidden_modules = {"urllib.request"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden_roots, (
                    f"results_writer imported forbidden module: {alias.name}"
                )
                assert alias.name not in forbidden_modules, (
                    f"results_writer imported forbidden module: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in forbidden_roots, (
                f"results_writer imported forbidden module: {node.module}"
            )
            assert node.module not in forbidden_modules, (
                f"results_writer imported forbidden module: {node.module}"
            )
