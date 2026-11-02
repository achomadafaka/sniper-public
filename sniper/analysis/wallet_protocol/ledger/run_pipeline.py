"""Slice 6 Task 10 — ledger analysis pipeline driver.

`run_analysis_pipeline(wallet, window, ledger_root, *, supabase=None,
run_id=..., code_version=None) -> RunResult`

* Materializes via Slice 5's `materialize_wallet_data`.
* Runs `analyze_wallet` against the bundle.
* Derives findings via Task 4's deriver.
* When `supabase` is provided: writes results + findings + the
  wallet-warehouse artifact via the Tasks 3 / 5 / 6 writers.
* When `supabase` is None: dry-run — no writers fire, but the result
  envelope is fully populated so callers can inspect.

Idempotent against the stub: re-running with the same `(wallet,
run_id, window.window_id)` is a full no-op on row counts (insert-ignore
semantics on the natural keys; finding deriver is deterministic on
`finding_uid`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.artifacts import (
    upsert_artifact_registry_entry,
)
from sniper.analysis.wallet_protocol.ledger.findings import (
    Finding,
    derive_findings_from_report,
    insert_findings,
)
from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
    materialize_wallet_data,
)
from sniper.analysis.wallet_protocol.ledger.paths import ledger_paths
from sniper.analysis.wallet_protocol.ledger.pipeline_lock import (
    acquire_pipeline_lock,
)
from sniper.analysis.wallet_protocol.ledger.results_writer import (
    insert_wallet_analysis_result,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import ProtocolReport


@dataclass
class RunResult:
    """Result envelope returned from `run_analysis_pipeline`.

    Carries enough state for downstream callers to inspect without
    re-materializing or re-deriving.

    `write_counts` (Phase 7 D9) reports the number of NEW rows each
    writer inserted on this invocation. Empty dict on the dry-run
    path (no writers fired). On the write path, keys are
    ``wallet_analysis_results``, ``wallet_findings``, and
    ``artifact_registry``; values match the writer return values, so
    a second pass against the same natural keys reports zeros — the
    user-facing signal that insert-ignore idempotency held.
    """

    report: ProtocolReport
    findings: list[Finding]
    artifact_paths: list[Path]
    replay_surface_meta: dict[str, Any] = field(default_factory=dict)
    write_counts: dict[str, int] = field(default_factory=dict)


def run_analysis_pipeline(
    *,
    wallet: str,
    window: AnalysisWindow,
    ledger_root: str | Path,
    run_id: str,
    supabase: SupabaseHttp | None = None,
    code_version: str | None = None,
    seed: int = 42,
    n_iter: int = 10_000,
    lock_path: str | Path | None = None,
    lock_block: bool = False,
) -> RunResult:
    """Run the per-wallet ledger analysis pipeline.

    Inter-process lock (Phase 7 review M2): the pipeline acquires a
    `fcntl.flock`-based writer lock for the duration of the run so two
    concurrent invocations cannot corrupt the DuckDB warehouse. Default
    lock path is `<ledger_root>/.sniper-ledger-pipeline.lock`; default
    mode is fail-fast — pass `lock_block=True` to queue.

    Released on normal exit AND on exception via the context manager.
    """
    resolved_lock_path = (
        Path(lock_path)
        if lock_path is not None
        else Path(ledger_root) / ".sniper-ledger-pipeline.lock"
    )
    with acquire_pipeline_lock(lock_path=resolved_lock_path, block=lock_block):
        bundle: MaterializedWalletData = materialize_wallet_data(
            wallet=wallet,
            window=window,
            ledger_root=ledger_root,
        )

        report = analyze_wallet(
            wallet,
            seed=seed,
            n_iter=n_iter,
            **bundle.to_analyze_wallet_kwargs(),
        )

        findings = derive_findings_from_report(
            report,
            wallet=wallet,
            run_id=run_id,
            window_id=window.window_id,
        )

        artifact_paths: list[Path] = []
        write_counts: dict[str, int] = {}
        if supabase is not None:
            wallet_warehouse_path = _wallet_warehouse_path(ledger_root, wallet)

            write_counts["wallet_analysis_results"] = insert_wallet_analysis_result(
                report=report,
                wallet=wallet,
                run_id=run_id,
                window_id=window.window_id,
                wallet_warehouse_path=str(wallet_warehouse_path)
                if wallet_warehouse_path is not None
                else None,
                replay_surface_meta=dict(bundle.replay_surface_meta),
                manifest_hashes=dict(bundle.manifest_hashes),
                artifact_hashes={},
                code_version=code_version,
                supabase=supabase,
            )

            write_counts["wallet_findings"] = insert_findings(
                report,
                wallet=wallet,
                run_id=run_id,
                window_id=window.window_id,
                supabase=supabase,
            )

            write_counts["artifact_registry"] = 0
            if (
                wallet_warehouse_path is not None
                and wallet_warehouse_path.exists()
            ):
                write_counts["artifact_registry"] = upsert_artifact_registry_entry(
                    wallet_warehouse_path,
                    kind="wallet_warehouse",
                    supabase=supabase,
                )
                artifact_paths.append(wallet_warehouse_path)

        return RunResult(
            report=report,
            findings=findings,
            artifact_paths=artifact_paths,
            replay_surface_meta=dict(bundle.replay_surface_meta),
            write_counts=write_counts,
        )


def _wallet_warehouse_path(
    ledger_root: str | Path, wallet: str
) -> Path | None:
    try:
        return ledger_paths(ledger_root, wallet).wallet_path
    except Exception:
        return None


__all__ = [
    "RunResult",
    "run_analysis_pipeline",
]
