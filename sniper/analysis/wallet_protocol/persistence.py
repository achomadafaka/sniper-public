"""Persistence layer for wallet_layer5_decision + analyzer_runs.

Two atomic public writers (Phase 3 plan § A5; spec § 3.1 write-time sequence):

- ``persist_successful_run_with_decision`` — one ``BEGIN IMMEDIATE`` that
  inserts the analyzer_runs row (success=1), flips stale is_current flags
  on other versions for the wallet, and upserts the decision row. The
  atomicity invariant: no success=1 run row exists without its decision;
  no live decision exists without its originating run.
- ``persist_failed_run`` — one ``BEGIN IMMEDIATE`` that inserts a
  success=0 analyzer_runs row only. No decision write.

There is **no** standalone ``persist_analyzer_run()`` that commits a
success=1 row by itself — that primitive caused the prior atomicity
defect. Decision-only writes are similarly disallowed.

Both writers presume the caller's connection has ``PRAGMA foreign_keys = ON``
and ``PRAGMA busy_timeout >= 5000``.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from sniper.analysis.wallet_protocol.types import ProtocolReport
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision

DEFAULT_ANALYSIS_VERSION = "wpv2.0"
LOCKED_RETRY_ATTEMPTS = 3
LOCKED_RETRY_DELAY_SECONDS = 0.25

_T = TypeVar("_T")


def _bool_to_int(v: bool | None) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def _run_transaction_with_locked_retry(
    conn: sqlite3.Connection,
    fn: Callable[[], _T],
    *,
    op_name: str,
    max_attempts: int = LOCKED_RETRY_ATTEMPTS,
    delay_seconds: float = LOCKED_RETRY_DELAY_SECONDS,
) -> _T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if not _is_locked(exc) or attempt >= max_attempts:
                raise
            time.sleep(delay_seconds)
        except Exception:
            conn.rollback()
            raise
    raise RuntimeError(f"{op_name} retry loop exhausted unexpectedly")


def persist_successful_run_with_decision(
    conn: sqlite3.Connection,
    *,
    pr: ProtocolReport,
    decision: Layer5Decision,
    started_ts: int,
    finished_ts: int,
    duration_ms: int,
    invocation_source: str,
    args_json: str,
    fetch_quality_flags_json: str,
    activity_rows_fetched: int | None,
    activity_cap_hit: bool | None,
    subgraph_fallback_used: bool,
    analysis_version: str = DEFAULT_ANALYSIS_VERSION,
    report_markdown: str = "",
) -> str:
    """Atomic: analyzer_runs insert + flip stale is_current + decision upsert.

    Returns the new run_id. On any exception inside the transaction, rolls
    back the entire sequence — no analyzer_runs row, no decision row, no
    is_current side-effects.
    """
    run_id = str(uuid.uuid4())
    def _transaction() -> str:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """
            INSERT INTO analyzer_runs (
                run_id, analysis_version, wallet, started_ts, finished_ts,
                duration_ms, success, error_class, error_message,
                invocation_source, args_json, fetch_quality_flags_json,
                activity_rows_fetched, activity_cap_hit, subgraph_fallback_used
            ) VALUES (?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                analysis_version,
                pr.wallet,
                started_ts,
                finished_ts,
                duration_ms,
                invocation_source,
                args_json,
                fetch_quality_flags_json,
                activity_rows_fetched,
                _bool_to_int(activity_cap_hit),
                1 if subgraph_fallback_used else 0,
            ),
        )

        conn.execute(
            "UPDATE wallet_layer5_decision SET is_current = 0, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE wallet = ? AND analysis_version != ?",
            (pr.wallet, analysis_version),
        )

        conn.execute(
            """
            INSERT INTO wallet_layer5_decision (
                wallet, analysis_version, analyzer_run_id,
                as_of_ts, gate_result, trading_mode, overall_verdict,
                primary_edge_type, flip_rate,
                n_closed_positions, n_held_to_resolution,
                n_open_positions, n_open_dead,
                net_pnl_usdc, dw_roi_point, dw_roi_ci_lower,
                dw_roi_ci_upper, ew_roi_point,
                activity_cap_hit, archetype_layer1,
                action_decision, action_reason_codes_json,
                confidence_band, is_current,
                held_branch_summary_json, flipper_branch_summary_json,
                tag_grain_summary_json, capital_ledger_summary_json,
                data_quality_flags_json, applicable_limitations_json,
                open_questions_json, copyability_note,
                report_markdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, analysis_version) DO UPDATE SET
                analyzer_run_id = excluded.analyzer_run_id,
                as_of_ts = excluded.as_of_ts,
                gate_result = excluded.gate_result,
                trading_mode = excluded.trading_mode,
                overall_verdict = excluded.overall_verdict,
                primary_edge_type = excluded.primary_edge_type,
                flip_rate = excluded.flip_rate,
                n_closed_positions = excluded.n_closed_positions,
                n_held_to_resolution = excluded.n_held_to_resolution,
                n_open_positions = excluded.n_open_positions,
                n_open_dead = excluded.n_open_dead,
                net_pnl_usdc = excluded.net_pnl_usdc,
                dw_roi_point = excluded.dw_roi_point,
                dw_roi_ci_lower = excluded.dw_roi_ci_lower,
                dw_roi_ci_upper = excluded.dw_roi_ci_upper,
                ew_roi_point = excluded.ew_roi_point,
                activity_cap_hit = excluded.activity_cap_hit,
                archetype_layer1 = excluded.archetype_layer1,
                action_decision = excluded.action_decision,
                action_reason_codes_json = excluded.action_reason_codes_json,
                confidence_band = excluded.confidence_band,
                is_current = 1,
                held_branch_summary_json = excluded.held_branch_summary_json,
                flipper_branch_summary_json = excluded.flipper_branch_summary_json,
                tag_grain_summary_json = excluded.tag_grain_summary_json,
                capital_ledger_summary_json = excluded.capital_ledger_summary_json,
                data_quality_flags_json = excluded.data_quality_flags_json,
                applicable_limitations_json = excluded.applicable_limitations_json,
                open_questions_json = excluded.open_questions_json,
                copyability_note = excluded.copyability_note,
                report_markdown = excluded.report_markdown,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                pr.wallet,
                analysis_version,
                run_id,
                finished_ts,
                pr.gate_result,
                pr.trading_mode,
                pr.overall_verdict,
                pr.primary_edge_type,
                pr.flip_rate,
                pr.n_closed_positions,
                pr.n_held_to_resolution,
                pr.n_open_positions,
                pr.n_open_dead,
                pr.net_pnl,
                pr.dw_roi_point,
                pr.dw_roi_ci_lower,
                pr.dw_roi_ci_upper,
                pr.ew_roi_point,
                1 if pr.activity_cap_hit else 0,
                pr.archetype_layer1,
                decision.action_decision,
                json.dumps(decision.reason_codes),
                decision.confidence_band,
                _branch_to_json(pr.held_analyst_branch),
                _branch_to_json(pr.flipper_branch),
                _tag_grain_to_json(pr),
                _capital_ledger_to_json(pr.capital_ledger),
                json.dumps(pr.data_quality_flags or []),
                json.dumps(pr.applicable_limitations or []),
                json.dumps(pr.open_questions or []),
                pr.copyability_note,
                report_markdown,
            ),
        )

        conn.commit()
        return run_id

    return _run_transaction_with_locked_retry(
        conn,
        _transaction,
        op_name=f"persist_successful_run_with_decision:{pr.wallet}",
    )


def persist_failed_run(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    started_ts: int,
    finished_ts: int,
    duration_ms: int,
    error_class: str,
    error_message: str | None,
    invocation_source: str,
    args_json: str,
    fetch_quality_flags_json: str,
    activity_rows_fetched: int | None,
    activity_cap_hit: bool | None,
    subgraph_fallback_used: bool,
    analysis_version: str = DEFAULT_ANALYSIS_VERSION,
) -> str:
    """Standalone txn: insert analyzer_runs with success=0. No decision write."""
    run_id = str(uuid.uuid4())
    truncated = (error_message[:500] if error_message else None)
    def _transaction() -> str:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO analyzer_runs (
                run_id, analysis_version, wallet, started_ts, finished_ts,
                duration_ms, success, error_class, error_message,
                invocation_source, args_json, fetch_quality_flags_json,
                activity_rows_fetched, activity_cap_hit, subgraph_fallback_used
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                analysis_version,
                wallet,
                started_ts,
                finished_ts,
                duration_ms,
                error_class,
                truncated,
                invocation_source,
                args_json,
                fetch_quality_flags_json,
                activity_rows_fetched,
                _bool_to_int(activity_cap_hit),
                1 if subgraph_fallback_used else 0,
            ),
        )
        conn.commit()
        return run_id

    return _run_transaction_with_locked_retry(
        conn,
        _transaction,
        op_name=f"persist_failed_run:{wallet}",
    )


def get_wallet_layer5_decision(
    conn: sqlite3.Connection, wallet: str
) -> dict[str, Any] | None:
    """Canonical-latest read per spec § 3.1 — filters is_current=1."""
    row = conn.execute(
        "SELECT * FROM wallet_layer5_decision "
        "WHERE wallet = ? AND is_current = 1",
        (wallet,),
    ).fetchone()
    return dict(row) if row else None


def persist_position_signal_decision(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    analysis_version: str,
    wallet_action_decision: str,
    primary_tag: str | None,
    tag_fit_score: float | None,
    tag_fit_rank: int | None,
    tag_fit_peer_count: int | None,
    tag_fit_confidence_flag: str | None,
    signal_reason_codes_json: str,
    analyzed_ts: int,
) -> None:
    """Per (wallet, condition_id, outcome, analysis_version) signal cache.

    Standalone single-row write. Does not interact with the wallet-decision
    atomicity invariant.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO position_signal_decision (
            wallet, condition_id, outcome, analysis_version,
            wallet_action_decision, primary_tag,
            tag_fit_score, tag_fit_rank, tag_fit_peer_count,
            tag_fit_confidence_flag, signal_reason_codes_json,
            analyzed_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            wallet,
            condition_id,
            outcome,
            analysis_version,
            wallet_action_decision,
            primary_tag,
            tag_fit_score,
            tag_fit_rank,
            tag_fit_peer_count,
            tag_fit_confidence_flag,
            signal_reason_codes_json,
            analyzed_ts,
        ),
    )
    conn.commit()


# ---------- JSON serialization helpers (cached context blobs) ----------


def _branch_to_json(branch) -> str:
    if branch is None:
        return "{}"
    return json.dumps(
        {
            "branch": branch.branch,
            "is_active": branch.is_active,
            "skip_reason": branch.skip_reason,
            "hypotheses": [
                {
                    "name": h.name,
                    "verdict": h.verdict,
                    "n": h.n,
                    "ci_lower": h.ci_lower,
                    "ci_upper": h.ci_upper,
                    "p_value_bh_adjusted": h.p_value_bh_adjusted,
                    "underpowered_reason": h.underpowered_reason,
                }
                for h in branch.hypotheses
            ],
            "stationarity": (
                {
                    "signaled": branch.stationarity.signaled,
                    "h_used": branch.stationarity.h_used,
                    "pre_regime_n": branch.stationarity.pre_regime_n,
                    "post_regime_n": branch.stationarity.post_regime_n,
                }
                if branch.stationarity is not None
                else None
            ),
        }
    )


def _tag_grain_to_json(pr: ProtocolReport) -> str:
    grains = []
    for b in (pr.held_analyst_branch, pr.flipper_branch):
        if b is None:
            continue
        for g in b.tag_grains:
            grains.append(
                {
                    "branch": b.branch,
                    "grain": g.grain,
                    "tag": g.tag,
                    "n_at_grain": g.n_at_grain,
                    "metric_value": g.metric_value,
                    "verdict": g.verdict,
                }
            )
    return json.dumps(grains)


def _capital_ledger_to_json(ledger) -> str:
    if ledger is None:
        return "{}"
    if hasattr(ledger, "__dict__"):
        return json.dumps(ledger.__dict__, default=str)
    return "{}"


__all__ = [
    "DEFAULT_ANALYSIS_VERSION",
    "persist_successful_run_with_decision",
    "persist_failed_run",
    "get_wallet_layer5_decision",
    "persist_position_signal_decision",
]
