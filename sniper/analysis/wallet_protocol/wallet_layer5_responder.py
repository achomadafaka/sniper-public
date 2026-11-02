"""Builds the Phase 2 § 6.1 ``WalletDecisionResponse`` from a persisted row.

Pure read-side helper. No side effects. Phase 4 wires the HTTP and MCP
routes in front of this builder; Phase 3 lands the builder + tests so the
§ 8 #7 round-trip acceptance criterion is satisfied immediately.

Status enum:
- ``ok``: latest row for this wallet matches ``CURRENT_ANALYSIS_VERSION``.
- ``stale_version``: a row exists for this wallet at a different version
  (older, never-current after a version bump, or the requested current
  version was bumped after persistence). Body still surfaces so consumers
  can choose to refresh or display.
- ``never_analyzed``: no row at any version for this wallet.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from sniper.analysis.wallet_protocol.persistence import (
    DEFAULT_ANALYSIS_VERSION,
    get_wallet_layer5_decision,
)

CURRENT_ANALYSIS_VERSION: str = DEFAULT_ANALYSIS_VERSION


def build_wallet_decision_response(
    conn: sqlite3.Connection, wallet: str
) -> dict[str, Any]:
    """Return a Phase 2 § 6.1 WalletDecisionResponse dict for ``wallet``.

    Reads the canonical-latest row first (``is_current=1``); if absent or
    if its ``analysis_version`` doesn't match the responder's current
    version, falls back to the most-recent any-version row and downgrades
    ``status`` to ``stale_version``. When no row exists at any version,
    returns ``{"wallet": wallet, "status": "never_analyzed"}``.
    """
    row = get_wallet_layer5_decision(conn, wallet)
    if row is None:
        any_row = conn.execute(
            "SELECT * FROM wallet_layer5_decision WHERE wallet = ? "
            "ORDER BY as_of_ts DESC LIMIT 1",
            (wallet,),
        ).fetchone()
        if any_row is None:
            return {"wallet": wallet, "status": "never_analyzed"}
        return _to_response(dict(any_row), status="stale_version")

    if row["analysis_version"] != CURRENT_ANALYSIS_VERSION:
        return _to_response(row, status="stale_version")

    return _to_response(row, status="ok")


def _to_response(row: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "wallet": row["wallet"],
        "analysis_version": row["analysis_version"],
        "is_current": (status == "ok"),
        "as_of_ts": row["as_of_ts"],

        "action_decision": row["action_decision"],
        "action_reason_codes": json.loads(row["action_reason_codes_json"] or "[]"),
        "confidence_band": row["confidence_band"],

        "overall_verdict": row["overall_verdict"],
        "primary_edge_type": row["primary_edge_type"],
        "trading_mode": row["trading_mode"],
        "gate_result": row["gate_result"],

        "n_closed_positions": row["n_closed_positions"],
        "n_held_to_resolution": row["n_held_to_resolution"],
        "n_open_positions": row["n_open_positions"],
        "n_open_dead": row["n_open_dead"],
        "net_pnl_usdc": row["net_pnl_usdc"],
        "dw_roi_point": row["dw_roi_point"],
        "dw_roi_ci_lower": row["dw_roi_ci_lower"],
        "dw_roi_ci_upper": row["dw_roi_ci_upper"],
        "ew_roi_point": row["ew_roi_point"],
        "activity_cap_hit": bool(row["activity_cap_hit"]),
        "archetype_layer1": row["archetype_layer1"],

        "data_quality_flags": json.loads(row["data_quality_flags_json"] or "[]"),
        "applicable_limitations": json.loads(
            row["applicable_limitations_json"] or "[]"
        ),
        "open_questions": json.loads(row["open_questions_json"] or "[]"),
        "copyability_note": row["copyability_note"],

        "report_markdown_url": f"/api/wallet-decision/{row['wallet']}/report.md",
    }


__all__ = [
    "CURRENT_ANALYSIS_VERSION",
    "build_wallet_decision_response",
]
