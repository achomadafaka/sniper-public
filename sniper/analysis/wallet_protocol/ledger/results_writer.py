"""Slice 6 Task 3 — `wallet_analysis_results` insert-ignore writer.

One function: `insert_wallet_analysis_result`. It serialises a
`ProtocolReport` plus the materialization provenance into the row
shape spec'd in §15 / migration `supabase_016_wallet_analysis_results.sql`,
then issues a single `insert_ignore` call against the injected
`SupabaseHttp`.

Idempotent on the natural key `(wallet, run_id, window_id)` (D2). The
writer never PATCHes; corrective writes go through
`mark_superseded(...)` (Task 5).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp
from sniper.analysis.wallet_protocol.types import PROTOCOL_VERSION, ProtocolReport


_TABLE = "wallet_analysis_results"
_NATURAL_KEY = ("wallet", "run_id", "window_id")


def _report_to_json(report: ProtocolReport) -> dict[str, Any]:
    """Convert the dataclass tree to a JSON-serialisable dict.

    `dataclasses.asdict` recursively unwraps nested dataclasses
    (`BranchReport`, `CapitalLedger`, …), which is what the durable
    row needs.
    """
    return dataclasses.asdict(report)


def insert_wallet_analysis_result(
    *,
    report: ProtocolReport,
    wallet: str,
    run_id: str,
    window_id: str,
    wallet_warehouse_path: str | None,
    replay_surface_meta: Mapping[str, Any] | None,
    manifest_hashes: Mapping[str, Any] | None,
    artifact_hashes: Mapping[str, Any] | None,
    code_version: str | None,
    supabase: SupabaseHttp,
) -> int:
    """Insert one `wallet_analysis_results` row, ignoring duplicates.

    Returns 1 on insert, 0 on conflict. The conflict resolution is
    PostgREST `Prefer: resolution=ignore-duplicates` (D1); the row
    payload is never overwritten.
    """
    row: dict[str, Any] = {
        # Natural key.
        "wallet": wallet,
        "run_id": run_id,
        "window_id": window_id,
        # Status defaults to 'active' per the migration; we set it
        # explicitly so the writer's payload matches the eventual row
        # shape one-for-one (no implicit-default surprises).
        "status": "active",
        # Headline summary (indexed columns).
        "overall_verdict": report.overall_verdict,
        "gate_result": report.gate_result,
        "trading_mode": report.trading_mode,
        "primary_edge_type": report.primary_edge_type,
        "n_closed_positions": report.n_closed_positions,
        "n_held_to_resolution": report.n_held_to_resolution,
        "net_pnl": report.net_pnl,
        "dw_roi_point": report.dw_roi_point,
        "ew_roi_point": report.ew_roi_point,
        # Full report + provenance.
        "report_json": _report_to_json(report),
        "replay_surface_meta": (
            dict(replay_surface_meta) if replay_surface_meta is not None else None
        ),
        "manifest_hashes": (
            dict(manifest_hashes) if manifest_hashes is not None else None
        ),
        "artifact_hashes": (
            dict(artifact_hashes) if artifact_hashes is not None else None
        ),
        "wallet_warehouse_path": wallet_warehouse_path,
        # Schema/code versions for the row's payload.
        "protocol_version": PROTOCOL_VERSION,
        "code_version": code_version,
    }
    return supabase.insert_ignore(
        _TABLE, [row], natural_key_columns=_NATURAL_KEY
    )


__all__ = ["insert_wallet_analysis_result"]
