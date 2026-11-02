"""Slice 6 Task 9 — gated live Supabase smoke test.

Runs ONLY when both `CALIBRATION_LIVE_SUPABASE_URL` and
`CALIBRATION_LIVE_SUPABASE_KEY` are present in the environment. The
fixtures construct `RestSupabaseHttp(url=..., key=...)` from those
values and inject it into the writers under test. They do NOT mutate
`os.environ` and do NOT touch `bot.supabase_helper`'s globals.

Test-namespace identity (Slice 6 Task 9):

* Wallet: `0x0000000000000000000000000000000000005006`
* run_id: `pytest-slice6-{uuid4().hex}` per run
* window_id: `pytest-slice6-window-v1`
* artifact paths prefixed with `pytest-slice6/{run_id}/...`

Cleanup respects D3:

* `wallet_analysis_results` and `wallet_findings` flip to
  `status='superseded'` via `mark_superseded(...)`.
* `artifact_registry` rows stay (append-only). The synthetic-namespace
  paths prefix is the only cleanup signal.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.artifacts import (
    upsert_artifact_registry_entry,
)
from sniper.analysis.wallet_protocol.ledger.findings import (
    insert_findings,
    mark_superseded,
)
from sniper.analysis.wallet_protocol.ledger.results_writer import (
    insert_wallet_analysis_result,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import (
    RestSupabaseHttp,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport


SYNTHETIC_WALLET = "0x0000000000000000000000000000000000005006"
WINDOW_ID = "pytest-slice6-window-v1"


def _live_or_skip() -> RestSupabaseHttp:
    url = os.environ.get("CALIBRATION_LIVE_SUPABASE_URL")
    key = os.environ.get("CALIBRATION_LIVE_SUPABASE_KEY")
    if not url or not key:
        pytest.skip(
            "CALIBRATION_LIVE_SUPABASE_URL / "
            "CALIBRATION_LIVE_SUPABASE_KEY not set; "
            "Slice 6 live smoke test is skipped by design (D4)."
        )
    return RestSupabaseHttp(url=url, key=key)


def _synthetic_report() -> ProtocolReport:
    return ProtocolReport(
        wallet=SYNTHETIC_WALLET,
        analyzed_at=1_700_000_000,
        n_closed_positions=0,
        overall_verdict="data_insufficient",
        gate_result="reject_insufficient_sample",
        trading_mode="n/a_routing",
    )


def test_live_supabase_round_trip(tmp_path: Path) -> None:
    """End-to-end: write results + findings + artifact, read back, then
    cleanup via supersede (results / findings) and leave the artifact."""
    supabase = _live_or_skip()
    run_id = f"pytest-slice6-{uuid.uuid4().hex}"
    report = _synthetic_report()

    # Stage an artifact under a per-run path so the synthetic-namespace
    # invariant holds.
    artifact_dir = tmp_path / "pytest-slice6" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    blob = artifact_dir / "report.json"
    blob.write_bytes(f"slice6-live-test-{run_id}".encode("utf-8"))

    try:
        # 1. Results writer.
        n_results = insert_wallet_analysis_result(
            report=report,
            wallet=SYNTHETIC_WALLET,
            run_id=run_id,
            window_id=WINDOW_ID,
            wallet_warehouse_path=str(blob),
            replay_surface_meta={"events": {"row_count": 0}},
            manifest_hashes={},
            artifact_hashes={},
            code_version=run_id,
            supabase=supabase,
        )
        assert n_results == 1

        # 2. Findings writer.
        n_findings = insert_findings(
            report,
            wallet=SYNTHETIC_WALLET,
            run_id=run_id,
            window_id=WINDOW_ID,
            supabase=supabase,
        )
        assert n_findings >= 1

        # 3. Artifact registry.
        n_artifact = upsert_artifact_registry_entry(
            blob,
            kind="report_blob",
            supabase=supabase,
        )
        assert n_artifact == 1

        # Round-trip: read the result row back.
        rows = supabase.select(
            "wallet_analysis_results",
            where={
                "wallet": SYNTHETIC_WALLET,
                "run_id": run_id,
                "window_id": WINDOW_ID,
            },
        )
        assert len(rows) == 1
        assert rows[0]["overall_verdict"] == "data_insufficient"

        finding_rows = supabase.select(
            "wallet_findings",
            where={
                "wallet": SYNTHETIC_WALLET,
                "run_id": run_id,
                "window_id": WINDOW_ID,
            },
        )
        assert len(finding_rows) == n_findings

    finally:
        # Cleanup — D3-respecting. Results + findings supersede; the
        # artifact stays (append-only).
        try:
            mark_superseded(
                table="wallet_analysis_results",
                natural_key={
                    "wallet": SYNTHETIC_WALLET,
                    "run_id": run_id,
                    "window_id": WINDOW_ID,
                },
                superseded_by="pytest-slice6-cleanup",
                supabase=supabase,
            )
        except Exception:  # pragma: no cover — best-effort cleanup
            pass
        try:
            mark_superseded(
                table="wallet_findings",
                natural_key={
                    "wallet": SYNTHETIC_WALLET,
                    "run_id": run_id,
                    "window_id": WINDOW_ID,
                },
                superseded_by="pytest-slice6-cleanup",
                supabase=supabase,
            )
        except Exception:  # pragma: no cover — best-effort cleanup
            pass

    # Final assertion: the run's results/findings are no longer active.
    active_results = supabase.select(
        "wallet_analysis_results",
        where={
            "wallet": SYNTHETIC_WALLET,
            "run_id": run_id,
            "status": "active",
        },
    )
    active_findings = supabase.select(
        "wallet_findings",
        where={
            "wallet": SYNTHETIC_WALLET,
            "run_id": run_id,
            "status": "active",
        },
    )
    assert active_results == []
    assert active_findings == []


def test_live_smoke_skips_cleanly_without_env() -> None:
    """Sanity: when env is unset the test skips, never raises."""
    if os.environ.get("CALIBRATION_LIVE_SUPABASE_URL") and os.environ.get(
        "CALIBRATION_LIVE_SUPABASE_KEY"
    ):
        pytest.skip("env IS set — round-trip test exercises the live path")
    with pytest.raises(pytest.skip.Exception):
        _live_or_skip()
