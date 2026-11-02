"""Slice 6 Task 10 — pipeline driver tests.

`run_analysis_pipeline(wallet, window, ledger_root, *, supabase=None,
code_version=None) -> RunResult`:

* Always materializes via Slice 5's `materialize_wallet_data` and runs
  `analyze_wallet` against the bundle.
* Derives findings via Task 4's deriver.
* When `supabase` is None: dry-run — the result envelope is returned
  unchanged; no writers fire.
* When `supabase` is provided: results + findings + warehouse-artifact
  rows are written via Tasks 3 / 5 / 6 writers.
* `RunResult` carries `report`, `findings`, `artifact_paths`,
  `replay_surface_meta` so callers can inspect without re-materializing.

The unit tests stub the materializer (Slice 5 already covers the real
path end-to-end) so the focus stays on the orchestration contract.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
)
from sniper.analysis.wallet_protocol.ledger.run_pipeline import (
    RunResult,
    run_analysis_pipeline,
)
import sniper.analysis.wallet_protocol.ledger.run_pipeline as run_pipeline_module
from sniper.analysis.wallet_protocol.ledger.pipeline_lock import (
    PipelineLockBusyError,
    acquire_pipeline_lock,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import WalletData


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PIPELINE_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "run_pipeline.py"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        window_id="w1",
        capture_run_ids=("run-a",),
        start_ts=1_700_000_000,
        end_ts=1_700_086_400,
        source_versions=(("price_v", "v1"),),
    )


def _bundle(wallet: str = "0xa") -> MaterializedWalletData:
    wallet_data = WalletData(
        wallet=wallet,
        closed_positions=[],  # data_insufficient
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_086_000,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
        subgraph_fallback_used=False,
        price_history_lookups={},
        leafs_by_cid=None,
        nav_lookup=None,
        peer_baselines=None,
        replay_surface_meta={"events": {"row_count": 0}},
    )

    def _phf(token_id: str, ts: int) -> float | None:
        return None

    return MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=_phf,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=_window(),
        replay_surface_meta={"events": {"row_count": 0}},
        manifest_hashes={},
        row_counts={"events": 0},
    )


@pytest.fixture
def patched_materialize():
    """Replace the real materializer with a synthetic-bundle factory.

    The pipeline imports `materialize_wallet_data` from
    `sniper.analysis.wallet_protocol.ledger.materialize`; the patch
    target is on the pipeline module so we intercept at the callsite
    without touching the materializer module's internals.
    """
    with patch(
        "sniper.analysis.wallet_protocol.ledger.run_pipeline.materialize_wallet_data",
        side_effect=lambda **kwargs: _bundle(kwargs.get("wallet") or "0xa"),
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Dry-run shape
# ---------------------------------------------------------------------------


def test_dry_run_returns_run_result_without_writing(
    tmp_path: Path, patched_materialize
) -> None:
    result = run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=None,
        run_id="r1",
    )
    assert isinstance(result, RunResult)
    assert result.report.wallet == "0xa"
    assert result.report.overall_verdict == "data_insufficient"
    assert len(result.findings) >= 1
    # Dry run -> no artifact paths persisted (no writers fired).
    assert isinstance(result.artifact_paths, list)


def test_run_result_carries_replay_surface_meta(
    tmp_path: Path, patched_materialize
) -> None:
    result = run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=None,
        run_id="r1",
    )
    assert isinstance(result.replay_surface_meta, dict)
    assert "events" in result.replay_surface_meta


# ---------------------------------------------------------------------------
# Writer integration (stubbed)
# ---------------------------------------------------------------------------


def test_pipeline_writes_results_and_findings_when_supabase_provided(
    tmp_path: Path, patched_materialize
) -> None:
    stub = StubSupabaseHttp()
    result = run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    assert result.report.wallet == "0xa"
    # Results row.
    results_rows = stub.rows_by_table.get("wallet_analysis_results", [])
    assert len(results_rows) == 1
    assert results_rows[0]["run_id"] == "r1"
    # At least one finding row.
    findings_rows = stub.rows_by_table.get("wallet_findings", [])
    assert len(findings_rows) >= 1
    assert {r["run_id"] for r in findings_rows} == {"r1"}


def test_pipeline_is_idempotent_against_stub(
    tmp_path: Path, patched_materialize
) -> None:
    """Re-running with the same (wallet, run_id, window_id) produces a
    full no-op against the stub: results row count stays 1, findings
    row count stays the same, and the call log grows."""
    stub = StubSupabaseHttp()
    run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    results_after_first = len(stub.rows_by_table["wallet_analysis_results"])
    findings_after_first = len(stub.rows_by_table["wallet_findings"])
    calls_after_first = len(stub.calls)

    run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    assert (
        len(stub.rows_by_table["wallet_analysis_results"])
        == results_after_first
    )
    assert (
        len(stub.rows_by_table["wallet_findings"]) == findings_after_first
    )
    assert len(stub.calls) > calls_after_first


# ---------------------------------------------------------------------------
# Phase 7 D9 — RunResult.write_counts
# ---------------------------------------------------------------------------


def test_dry_run_write_counts_is_empty_dict(
    tmp_path: Path, patched_materialize
) -> None:
    """Dry-run never fires writers, so `write_counts` is an empty dict
    (the additive default per D9)."""
    result = run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=None,
        run_id="r1",
    )
    assert result.write_counts == {}


def test_first_write_counts_match_writer_returns_no_artifact(
    tmp_path: Path, patched_materialize
) -> None:
    """When supabase is provided AND no warehouse file exists at
    `ledger_root/<wallet>.duckdb`, the artifact writer must not fire
    (the existing pipeline guard); `write_counts` for artifact_registry
    is 0."""
    stub = StubSupabaseHttp()
    result = run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,  # no <wallet>.duckdb on disk
        supabase=stub,
        run_id="r1",
    )
    assert result.write_counts["wallet_analysis_results"] == 1
    assert result.write_counts["wallet_findings"] == len(result.findings)
    assert result.write_counts["wallet_findings"] >= 1
    assert result.write_counts["artifact_registry"] == 0


_VALID_WALLET = "0x000000000000000000000000000000000000aaaa"


def test_first_write_counts_match_writer_returns_with_artifact(
    tmp_path: Path, patched_materialize
) -> None:
    """When the wallet warehouse file exists on disk, the artifact
    writer fires and `write_counts["artifact_registry"]` reports 1."""
    # Pre-create the warehouse file so `_wallet_warehouse_path(...).exists()`
    # is True. The patched materializer ignores the file's contents.
    # Wallet must be a valid 0x + 40 hex per ledger.paths.normalize_wallet.
    (tmp_path / f"{_VALID_WALLET}.duckdb").write_bytes(b"warehouse-bytes")
    stub = StubSupabaseHttp()
    result = run_analysis_pipeline(
        wallet=_VALID_WALLET,
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    assert result.write_counts["wallet_analysis_results"] == 1
    assert result.write_counts["wallet_findings"] == len(result.findings)
    assert result.write_counts["wallet_findings"] >= 1
    assert result.write_counts["artifact_registry"] == 1


def test_second_write_counts_are_all_zero(
    tmp_path: Path, patched_materialize
) -> None:
    """Re-running the pipeline with the same natural keys against the
    same stub returns zeros across all three writer keys — that is
    the user-facing signal that idempotency held."""
    (tmp_path / f"{_VALID_WALLET}.duckdb").write_bytes(b"warehouse-bytes")
    stub = StubSupabaseHttp()
    first = run_analysis_pipeline(
        wallet=_VALID_WALLET,
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    assert first.write_counts["wallet_analysis_results"] == 1

    second = run_analysis_pipeline(
        wallet=_VALID_WALLET,
        window=_window(),
        ledger_root=tmp_path,
        supabase=stub,
        run_id="r1",
    )
    assert second.write_counts == {
        "wallet_analysis_results": 0,
        "wallet_findings": 0,
        "artifact_registry": 0,
    }


# ---------------------------------------------------------------------------
# Phase 7 review M2 — inter-process writer lock
# ---------------------------------------------------------------------------


def test_pipeline_uses_ledger_root_lock_by_default(
    tmp_path: Path, patched_materialize, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict = {}
    real_acquire = acquire_pipeline_lock

    def _recording_lock(*, lock_path=None, block=False):
        observed["lock_path"] = Path(lock_path)
        observed["block"] = block
        return real_acquire(lock_path=lock_path, block=block)

    monkeypatch.setattr(
        run_pipeline_module, "acquire_pipeline_lock", _recording_lock
    )

    run_analysis_pipeline(
        wallet="0xa",
        window=_window(),
        ledger_root=tmp_path,
        supabase=None,
        run_id="r1",
    )

    assert observed["lock_path"] == tmp_path / ".sniper-ledger-pipeline.lock"
    assert observed["block"] is False


def test_pipeline_lock_is_fail_fast_when_already_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "pipeline.lock"
    with acquire_pipeline_lock(lock_path=lock_path, block=False):
        with pytest.raises(PipelineLockBusyError):
            with acquire_pipeline_lock(lock_path=lock_path, block=False):
                pass


def test_pipeline_lock_releases_after_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "pipeline.lock"
    with pytest.raises(RuntimeError):
        with acquire_pipeline_lock(lock_path=lock_path, block=False):
            raise RuntimeError("boom")

    with acquire_pipeline_lock(lock_path=lock_path, block=False):
        assert lock_path.exists()


# ---------------------------------------------------------------------------
# Module guard
# ---------------------------------------------------------------------------


def test_run_pipeline_module_does_not_import_live_apis() -> None:
    tree = ast.parse(RUN_PIPELINE_FILE.read_text())
    forbidden_roots = {"bot", "requests", "httpx"}
    forbidden_modules = {"urllib.request"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden_roots
                assert alias.name not in forbidden_modules
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in forbidden_roots
            assert node.module not in forbidden_modules
