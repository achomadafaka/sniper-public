"""Opt-in Supabase memo emission for wallet protocol runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from dataclasses import asdict
from typing import Any

from sniper.analysis.wallet_protocol.types import ProtocolReport


@dataclass(frozen=True)
class MemoEmissionResult:
    status: str
    message: str = ""


def _has_supabase_env() -> bool:
    return bool(os.getenv("SUPABASE_URL")) and bool(
        os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )


def _memo_content(report: ProtocolReport) -> str:
    payload: dict[str, Any] = {
        "wallet": report.wallet,
        "analyzed_at": report.analyzed_at,
        "overall_verdict": report.overall_verdict,
        "primary_edge_type": report.primary_edge_type,
        "gate_result": report.gate_result,
        "trading_mode": report.trading_mode,
        "n_closed_positions": report.n_closed_positions,
        "n_held_to_resolution": report.n_held_to_resolution,
        "data_quality_flags": report.data_quality_flags,
        "applicable_limitations": report.applicable_limitations,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def maybe_emit_memo(
    report: ProtocolReport,
    *,
    persist_memo: bool,
) -> MemoEmissionResult:
    if not persist_memo:
        return MemoEmissionResult("skipped_disabled")
    if not _has_supabase_env():
        return MemoEmissionResult(
            "skipped_missing_env",
            "memo skipped: missing Supabase env",
        )
    if "PYTEST_CURRENT_TEST" in os.environ:
        return MemoEmissionResult("skipped_pytest", "memo skipped under pytest")

    from bot.supabase_helper import upsert_memory

    name = f"wallet_protocol_run:{report.wallet}:{report.analyzed_at}"
    upsert_memory(
        "project",
        name,
        f"Wallet protocol run for {report.wallet}",
        _memo_content(report),
        source="local",
    )
    return MemoEmissionResult("emitted", name)


__all__ = ["MemoEmissionResult", "maybe_emit_memo"]
