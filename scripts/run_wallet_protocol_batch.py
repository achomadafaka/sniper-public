#!/usr/bin/env python3
"""Run wallet-analysis-protocol-v2 progressively across multiple wallets.

Each wallet is its own unit of work: status is written before/during/after
analysis, successful runs keep the existing ``run_wallet_protocol --persist``
path, and failures are recorded without blocking the rest of the batch.

Processing is intentionally sequential for the first implementation. The
``(batch_id, wallet)`` item model is ready for future worker parallelism.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from sniper.analysis.wallet_protocol.batch_failures import classify as classify_failure

STATUSES: tuple[str, ...] = (
    "queued", "running", "completed", "failed", "skipped",
)
ACTION_BUCKETS: tuple[str, ...] = (
    "actionable_now", "review", "low_confidence", "no_action", "failed",
)
INTAKE_SOURCES: tuple[str, ...] = (
    "manual", "discovery_agent", "import_file", "api", "other",
)
_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

_ACTION_BUCKET_BY_DECISION: dict[str, str] = {
    "validated_copy_candidate": "actionable_now",
    "reverse_engineer_candidate": "review",
    "study": "low_confidence",
    "monitor": "low_confidence",
    "exclude_for_copy": "no_action",
    "anti_pattern": "no_action",
}


@dataclass(frozen=True)
class BatchWallet:
    wallet: str
    source: str
    source_ref: str
    metadata_json: str
    intake_queue_id: int | None = None


def action_bucket_for_decision(action_decision: str | None) -> str:
    return _ACTION_BUCKET_BY_DECISION.get(str(action_decision or ""), "no_action")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _now() -> int:
    return int(time.time())


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _validate_wallet(wallet: str) -> str:
    normalized = _normalize_wallet(wallet)
    if not _WALLET_RE.match(normalized):
        raise ValueError(f"invalid wallet address: {wallet}")
    return normalized


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Progressively run wallet-analysis-protocol-v2 for wallets",
    )
    ap.add_argument(
        "--db", default=os.getenv("SNIPER_DB_PATH", "sniper_tracker.db"),
        help="SQLite path used by the batch and single-wallet --persist run",
    )
    ap.add_argument(
        "--batch-id",
        default=None,
        help="Batch id. Required with --retry-failed; generated otherwise.",
    )
    ap.add_argument(
        "--wallet",
        action="append",
        default=[],
        help="Wallet to analyze. Repeatable.",
    )
    ap.add_argument(
        "--wallet-file",
        action="append",
        default=[],
        help="Plain text file with one wallet per line. Blank lines and "
             "lines starting with # are ignored. Repeatable.",
    )
    ap.add_argument(
        "--source",
        default="manual",
        choices=INTAKE_SOURCES,
        help="Source label for explicit --wallet / --wallet-file inputs.",
    )
    ap.add_argument(
        "--source-ref",
        default="",
        help="Optional source reference for explicit inputs.",
    )
    ap.add_argument(
        "--metadata-json",
        default="{}",
        help="Optional JSON object copied to each explicit batch item.",
    )
    ap.add_argument(
        "--notes",
        default="",
        help="Optional notes used when enqueueing analyzer-intake rows.",
    )
    ap.add_argument(
        "--from-queue",
        action="store_true",
        help="Claim wallets from wallet_analyzer_intake_queue only.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum wallet_analyzer_intake_queue rows to claim.",
    )
    ap.add_argument(
        "--retry-queue-failed",
        action="store_true",
        help="With --from-queue, include failed intake rows for retry.",
    )
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed wallet_analysis_batch_item rows from --batch-id.",
    )
    ap.add_argument(
        "--pause-batch",
        action="store_true",
        help="Mark queued/running batch items paused and return linked queue "
             "rows to pending. Stop the running process first.",
    )
    ap.add_argument(
        "--resume-batch",
        action="store_true",
        help="Resume items paused by --pause-batch for --batch-id.",
    )
    ap.add_argument(
        "--cancel-batch",
        action="store_true",
        help="Cancel queued/running batch items and mark linked queue rows "
             "skipped.",
    )
    ap.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=30.0,
        help="Seconds between DB heartbeat writes for the running wallet.",
    )
    ap.add_argument(
        "--per-wallet-timeout-s",
        type=float,
        default=0.0,
        help="Maximum seconds allowed for one wallet. 0 disables timeout.",
    )
    ap.add_argument("--n-iter", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-tags", action="store_true")
    ap.add_argument("--no-peers", action="store_true")
    ap.add_argument("--no-exec", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--max-exec-trades", type=int, default=500)
    ap.add_argument("--archetype", default=None)
    ap.add_argument("--no-archetype", action="store_true")
    ap.add_argument("--force-exec", action="store_true")
    ap.add_argument("--full-history", action="store_true")
    ap.add_argument("--capital-ledger", action="store_true")
    return ap


def _canonical_metadata_json(raw: str) -> str:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --metadata-json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--metadata-json must be a JSON object")
    return json.dumps(payload, sort_keys=True)


def _read_wallet_file(path: str) -> list[str]:
    wallets: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            wallets.append(value)
    return wallets


def build_explicit_wallets(args: argparse.Namespace) -> list[BatchWallet]:
    metadata_json = _canonical_metadata_json(args.metadata_json)
    raw_wallets: list[str] = []
    raw_wallets.extend(args.wallet or [])
    for path in args.wallet_file or []:
        raw_wallets.extend(_read_wallet_file(path))

    out: list[BatchWallet] = []
    seen: set[str] = set()
    for raw in raw_wallets:
        wallet = _validate_wallet(raw)
        if wallet in seen:
            continue
        seen.add(wallet)
        out.append(
            BatchWallet(
                wallet=wallet,
                source=args.source,
                source_ref=args.source_ref,
                metadata_json=metadata_json,
                intake_queue_id=None,
            )
        )
    return out


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    source: str,
    total_wallets: int,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO wallet_analysis_batch (
            batch_id, source, status, total_wallets, created_ts, started_ts
        ) VALUES (?, ?, 'running', ?, ?, ?)
        ON CONFLICT(batch_id) DO UPDATE SET
            source = excluded.source,
            status = 'running',
            total_wallets = excluded.total_wallets,
            started_ts = COALESCE(wallet_analysis_batch.started_ts,
                                  excluded.started_ts),
            finished_ts = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (batch_id, source, total_wallets, now, now),
    )
    conn.commit()


def queue_batch_items(
    conn: sqlite3.Connection,
    batch_id: str,
    wallets: list[BatchWallet],
) -> None:
    for item in wallets:
        conn.execute(
            """
            INSERT INTO wallet_analysis_batch_item (
                batch_id, wallet, status, source, source_ref,
                metadata_json, intake_queue_id
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
            ON CONFLICT(batch_id, wallet) DO NOTHING
            """,
            (
                batch_id,
                item.wallet,
                item.source,
                item.source_ref,
                item.metadata_json,
                item.intake_queue_id,
            ),
        )
    conn.commit()


def enqueue_intake_wallet(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    source: str,
    source_ref: str = "",
    priority: int = 0,
    metadata_json: str = "{}",
    notes: str = "",
) -> int:
    """Insert one source-clean analyzer intake row and return its id."""
    normalized = _validate_wallet(wallet)
    canonical_metadata = _canonical_metadata_json(metadata_json)
    if source not in INTAKE_SOURCES:
        raise ValueError(f"invalid source: {source}")
    now = _now()
    try:
        cur = conn.execute(
            """
            INSERT INTO wallet_analyzer_intake_queue (
                wallet, source, source_ref, status, priority, created_ts,
                metadata_json, notes
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                normalized,
                source,
                source_ref,
                int(priority),
                now,
                canonical_metadata,
                notes,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            SELECT id FROM wallet_analyzer_intake_queue
            WHERE wallet = ? AND status IN ('pending', 'claimed', 'running')
            ORDER BY id DESC LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if row is None:
            raise
        return int(row["id"])


def claim_intake_wallets(
    conn: sqlite3.Connection,
    *,
    limit: int,
    include_failed: bool = False,
) -> list[BatchWallet]:
    """Claim source-clean analyzer intake rows from the new queue only."""
    statuses = ["pending"]
    if include_failed:
        statuses.append("failed")
    claimed: list[BatchWallet] = []
    now = _now()
    for _ in range(max(int(limit), 0)):
        placeholders = ",".join("?" for _ in statuses)
        row = conn.execute(
            f"""
            SELECT * FROM wallet_analyzer_intake_queue
            WHERE status IN ({placeholders})
            ORDER BY priority DESC, created_ts ASC, id ASC
            LIMIT 1
            """,
            statuses,
        ).fetchone()
        if row is None:
            break
        cur = conn.execute(
            """
            UPDATE wallet_analyzer_intake_queue
            SET status = 'claimed',
                claimed_ts = ?,
                error_class = NULL,
                error_message = NULL,
                failure_category = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = ?
            """,
            (now, row["id"], row["status"]),
        )
        if cur.rowcount != 1:
            continue
        claimed.append(
            BatchWallet(
                wallet=str(row["wallet"]),
                source=str(row["source"]),
                source_ref=str(row["source_ref"] or ""),
                metadata_json=str(row["metadata_json"] or "{}"),
                intake_queue_id=int(row["id"]),
            )
        )
    conn.commit()
    return claimed


def _failed_wallets(conn: sqlite3.Connection, batch_id: str) -> list[BatchWallet]:
    rows = conn.execute(
        """
        SELECT wallet, source, source_ref, metadata_json, intake_queue_id
        FROM wallet_analysis_batch_item
        WHERE batch_id = ? AND status = 'failed'
        ORDER BY finished_ts, wallet
        """,
        (batch_id,),
    ).fetchall()
    return [
        BatchWallet(
            wallet=str(row["wallet"]),
            source=str(row["source"] or "manual"),
            source_ref=str(row["source_ref"] or ""),
            metadata_json=str(row["metadata_json"] or "{}"),
            intake_queue_id=(
                int(row["intake_queue_id"])
                if row["intake_queue_id"] is not None
                else None
            ),
        )
        for row in rows
    ]


def _paused_wallets(conn: sqlite3.Connection, batch_id: str) -> list[BatchWallet]:
    rows = conn.execute(
        """
        SELECT wallet, source, source_ref, metadata_json, intake_queue_id
        FROM wallet_analysis_batch_item
        WHERE batch_id = ?
          AND status = 'skipped'
          AND error_class = 'BatchPaused'
        ORDER BY finished_ts, wallet
        """,
        (batch_id,),
    ).fetchall()
    return [
        BatchWallet(
            wallet=str(row["wallet"]),
            source=str(row["source"] or "manual"),
            source_ref=str(row["source_ref"] or ""),
            metadata_json=str(row["metadata_json"] or "{}"),
            intake_queue_id=(
                int(row["intake_queue_id"])
                if row["intake_queue_id"] is not None
                else None
            ),
        )
        for row in rows
    ]


def _mark_batch_running(conn: sqlite3.Connection, batch_id: str, source: str) -> None:
    conn.execute(
        """
        UPDATE wallet_analysis_batch
        SET source = ?,
            status = 'running',
            started_ts = COALESCE(started_ts, ?),
            finished_ts = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ?
        """,
        (source, _now(), batch_id),
    )
    conn.commit()


def _set_unfinished_batch_state(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    item_error_class: str,
    item_error_message: str,
    intake_status: str,
    intake_error_class: str | None,
    intake_error_message: str | None,
) -> int:
    now = _now()
    rows = conn.execute(
        """
        SELECT wallet, intake_queue_id
        FROM wallet_analysis_batch_item
        WHERE batch_id = ? AND status IN ('queued', 'running')
        """,
        (batch_id,),
    ).fetchall()
    intake_ids = [
        int(row["intake_queue_id"])
        for row in rows
        if row["intake_queue_id"] is not None
    ]

    conn.execute(
        """
        UPDATE wallet_analysis_batch_item
        SET status = 'skipped',
            finished_ts = ?,
            heartbeat_ts = COALESCE(heartbeat_ts, ?),
            error_class = ?,
            error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ? AND status IN ('queued', 'running')
        """,
        (
            now,
            now,
            item_error_class,
            item_error_message[:500],
            batch_id,
        ),
    )

    if intake_ids:
        placeholders = ",".join("?" for _ in intake_ids)
        conn.execute(
            f"""
            UPDATE wallet_analyzer_intake_queue
            SET status = ?,
                claimed_ts = CASE WHEN ? = 'pending' THEN NULL ELSE claimed_ts END,
                started_ts = CASE WHEN ? = 'pending' THEN NULL ELSE started_ts END,
                finished_ts = CASE WHEN ? = 'skipped' THEN ? ELSE finished_ts END,
                error_class = ?,
                error_message = ?,
                failure_category = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
              AND status IN ('pending', 'claimed', 'running')
            """,
            (
                intake_status,
                intake_status,
                intake_status,
                intake_status,
                now,
                intake_error_class,
                (
                    intake_error_message[:500]
                    if intake_error_message is not None
                    else None
                ),
                (
                    classify_failure(RuntimeError(intake_error_message))
                    if intake_error_message
                    else None
                ),
                *intake_ids,
            ),
        )

    conn.execute(
        """
        UPDATE wallet_analysis_batch
        SET status = 'skipped',
            finished_ts = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ?
        """,
        (now, batch_id),
    )
    conn.commit()
    return len(rows)


def pause_batch(conn: sqlite3.Connection, batch_id: str) -> int:
    return _set_unfinished_batch_state(
        conn,
        batch_id=batch_id,
        item_error_class="BatchPaused",
        item_error_message="paused by run_wallet_protocol_batch --pause-batch",
        intake_status="pending",
        intake_error_class=None,
        intake_error_message=None,
    )


def cancel_batch(conn: sqlite3.Connection, batch_id: str) -> int:
    return _set_unfinished_batch_state(
        conn,
        batch_id=batch_id,
        item_error_class="BatchCancelled",
        item_error_message="cancelled by run_wallet_protocol_batch --cancel-batch",
        intake_status="skipped",
        intake_error_class="BatchCancelled",
        intake_error_message="cancelled by run_wallet_protocol_batch --cancel-batch",
    )


def _mark_item_running(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    wallet: str,
    started_ts: int,
) -> None:
    conn.execute(
        """
        UPDATE wallet_analysis_batch_item
        SET status = 'running',
            started_ts = ?,
            heartbeat_ts = ?,
            finished_ts = NULL,
            duration_ms = NULL,
            analyzer_run_id = NULL,
            action_bucket = NULL,
            action_decision = NULL,
            confidence_band = NULL,
            overall_verdict = NULL,
            primary_edge_type = NULL,
            summary_markdown = NULL,
            summary_json = NULL,
            error_class = NULL,
            error_message = NULL,
            attempt_count = attempt_count + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ? AND wallet = ?
        """,
        (started_ts, started_ts, batch_id, wallet),
    )
    conn.commit()


def _mark_intake_running(
    conn: sqlite3.Connection,
    intake_queue_id: int | None,
    started_ts: int,
) -> None:
    if intake_queue_id is None:
        return
    conn.execute(
        """
        UPDATE wallet_analyzer_intake_queue
        SET status = 'running',
            started_ts = ?,
            last_attempt_ts = ?,
            attempt_count = attempt_count + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (started_ts, started_ts, intake_queue_id),
    )
    conn.commit()


def _mark_intake_completed(
    conn: sqlite3.Connection,
    intake_queue_id: int | None,
    finished_ts: int,
) -> None:
    if intake_queue_id is None:
        return
    conn.execute(
        """
        UPDATE wallet_analyzer_intake_queue
        SET status = 'completed',
            finished_ts = ?,
            error_class = NULL,
            error_message = NULL,
            failure_category = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (finished_ts, intake_queue_id),
    )
    conn.commit()


def _mark_intake_failed(
    conn: sqlite3.Connection,
    intake_queue_id: int | None,
    finished_ts: int,
    error_class: str,
    error_message: str,
    failure_category: str,
) -> None:
    if intake_queue_id is None:
        return
    conn.execute(
        """
        UPDATE wallet_analyzer_intake_queue
        SET status = 'failed',
            finished_ts = ?,
            error_class = ?,
            error_message = ?,
            failure_category = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            finished_ts,
            error_class,
            error_message[:500],
            failure_category,
            intake_queue_id,
        ),
    )
    conn.commit()


def _heartbeat_item(db_path: str, batch_id: str, wallet: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            UPDATE wallet_analysis_batch_item
            SET heartbeat_ts = ?, updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = ? AND wallet = ? AND status = 'running'
            """,
            (_now(), batch_id, wallet),
        )
        conn.commit()
    finally:
        conn.close()


def _start_heartbeat_thread(
    *,
    db_path: str,
    batch_id: str,
    wallet: str,
    interval_seconds: float,
) -> tuple[threading.Event, threading.Thread | None]:
    if interval_seconds <= 0:
        return threading.Event(), None

    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            _heartbeat_item(db_path, batch_id, wallet)
            stop.wait(interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop, thread


def _run_single_wallet_protocol(
    wallet: str,
    *,
    db_path: str,
    single_wallet_args: list[str],
    timeout_seconds: float | None = None,
) -> int:
    timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None and float(timeout_seconds) > 0
        else None
    )
    script = Path(__file__).resolve().with_name("run_wallet_protocol.py")
    env = os.environ.copy()
    env["SNIPER_DB_PATH"] = db_path
    cmd = [
        sys.executable,
        str(script),
        wallet,
        "--db",
        db_path,
        "--persist",
        *single_wallet_args,
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(script.parents[1]),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            sys.stdout.write(str(exc.stdout))
        if exc.stderr:
            sys.stderr.write(str(exc.stderr))
        raise TimeoutError(f"wallet analysis timed out after {timeout:g}s") from exc

    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return int(completed.returncode)


def _single_wallet_args(args: argparse.Namespace) -> list[str]:
    out: list[str] = ["--n-iter", str(args.n_iter), "--seed", str(args.seed)]
    for flag in (
        "no_tags", "no_peers", "no_exec", "no_history", "no_archetype",
        "force_exec", "full_history", "capital_ledger",
    ):
        if getattr(args, flag):
            out.append("--" + flag.replace("_", "-"))
    out.extend(["--max-exec-trades", str(args.max_exec_trades)])
    if args.archetype:
        out.extend(["--archetype", args.archetype])
    return out


def _latest_decision(conn: sqlite3.Connection, wallet: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM wallet_layer5_decision
        WHERE wallet = ? AND is_current = 1
        ORDER BY as_of_ts DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    return dict(row) if row else None


def _latest_run_id(conn: sqlite3.Connection, wallet: str) -> str | None:
    row = conn.execute(
        """
        SELECT run_id FROM analyzer_runs
        WHERE wallet = ?
        ORDER BY started_ts DESC, created_at DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    return str(row["run_id"]) if row else None


def build_summary(decision: dict[str, Any]) -> tuple[str, str, str]:
    action_decision = str(decision["action_decision"])
    confidence_band = str(decision["confidence_band"])
    action_bucket = action_bucket_for_decision(action_decision)
    reason_codes = _json_loads(decision.get("action_reason_codes_json"), [])
    flags = list(_json_loads(decision.get("data_quality_flags_json"), []))
    if decision.get("activity_cap_hit"):
        flags.append("activity_cap_hit")
    flags_text = ", ".join(str(f) for f in flags) if flags else "none"
    why = ", ".join(str(r) for r in reason_codes) if reason_codes else "n/a"
    primary_edge_type = decision.get("primary_edge_type") or "n/a"

    summary = {
        "wallet": decision["wallet"],
        "bucket": action_bucket,
        "decision": action_decision,
        "confidence_band": confidence_band,
        "overall_verdict": decision.get("overall_verdict"),
        "primary_edge_type": decision.get("primary_edge_type"),
        "n_closed": decision.get("n_closed_positions"),
        "n_held": decision.get("n_held_to_resolution"),
        "pnl": decision.get("net_pnl_usdc"),
        "dw_roi": decision.get("dw_roi_point"),
        "archetype": decision.get("archetype_layer1"),
        "flags": flags,
        "reason_codes": reason_codes,
        "report_ref": "wallet_layer5_decision.report_markdown",
    }
    markdown = "\n".join([
        f"WALLET {decision['wallet']}",
        f"bucket: {action_bucket}",
        f"decision: {action_decision} / {confidence_band}",
        f"verdict: {decision.get('overall_verdict')} / {primary_edge_type}",
        "stats: "
        f"n_closed={decision.get('n_closed_positions')} "
        f"held={decision.get('n_held_to_resolution')} "
        f"pnl={decision.get('net_pnl_usdc')} "
        f"dw_roi={decision.get('dw_roi_point')}",
        f"archetype: {decision.get('archetype_layer1') or 'n/a'}",
        f"flags: {flags_text}",
        f"why: {why}",
        "report: stored in wallet_layer5_decision.report_markdown",
    ])
    return action_bucket, markdown, json.dumps(summary, sort_keys=True)


def _mark_item_completed(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    wallet: str,
    started_ts: int,
    finished_ts: int,
    duration_ms: int,
    decision: dict[str, Any],
) -> str:
    action_bucket, summary_markdown, summary_json = build_summary(decision)
    conn.execute(
        """
        UPDATE wallet_analysis_batch_item
        SET status = 'completed',
            finished_ts = ?,
            duration_ms = ?,
            heartbeat_ts = COALESCE(heartbeat_ts, ?),
            analyzer_run_id = ?,
            action_bucket = ?,
            action_decision = ?,
            confidence_band = ?,
            overall_verdict = ?,
            primary_edge_type = ?,
            summary_markdown = ?,
            summary_json = ?,
            error_class = NULL,
            error_message = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ? AND wallet = ?
        """,
        (
            finished_ts,
            duration_ms,
            finished_ts,
            decision.get("analyzer_run_id"),
            action_bucket,
            decision.get("action_decision"),
            decision.get("confidence_band"),
            decision.get("overall_verdict"),
            decision.get("primary_edge_type"),
            summary_markdown,
            summary_json,
            batch_id,
            wallet,
        ),
    )
    conn.commit()
    return summary_markdown


def _mark_item_failed(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    wallet: str,
    finished_ts: int,
    duration_ms: int,
    error_class: str,
    error_message: str,
    analyzer_run_id: str | None,
) -> None:
    conn.execute(
        """
        UPDATE wallet_analysis_batch_item
        SET status = 'failed',
            finished_ts = ?,
            duration_ms = ?,
            heartbeat_ts = COALESCE(heartbeat_ts, ?),
            analyzer_run_id = ?,
            action_bucket = 'failed',
            error_class = ?,
            error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ? AND wallet = ?
        """,
        (
            finished_ts,
            duration_ms,
            finished_ts,
            analyzer_run_id,
            error_class,
            error_message[:500],
            batch_id,
            wallet,
        ),
    )
    conn.commit()


def _process_wallet(
    *,
    db_path: str,
    batch_id: str,
    item: BatchWallet,
    single_wallet_args: list[str],
    heartbeat_seconds: float,
    wallet_timeout_seconds: float | None,
) -> bool:
    wallet = item.wallet
    conn = _connect(db_path)
    started_ts = _now()
    started_monotonic = time.monotonic()
    try:
        _mark_item_running(
            conn,
            batch_id=batch_id,
            wallet=wallet,
            started_ts=started_ts,
        )
        _mark_intake_running(conn, item.intake_queue_id, started_ts)
    finally:
        conn.close()

    print(f"\n=== running {wallet} ===", flush=True)
    stop, thread = _start_heartbeat_thread(
        db_path=db_path,
        batch_id=batch_id,
        wallet=wallet,
        interval_seconds=heartbeat_seconds,
    )

    try:
        run_kwargs: dict[str, Any] = {
            "db_path": db_path,
            "single_wallet_args": single_wallet_args,
        }
        if wallet_timeout_seconds is not None and wallet_timeout_seconds > 0:
            run_kwargs["timeout_seconds"] = wallet_timeout_seconds
        rc = _run_single_wallet_protocol(wallet, **run_kwargs)
        if rc != 0:
            raise RuntimeError(f"run_wallet_protocol returned {rc}")
    except Exception as exc:
        failure_category = classify_failure(exc)
        finished_ts = _now()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        stop.set()
        if thread is not None:
            thread.join(timeout=2)
        conn = _connect(db_path)
        try:
            _mark_item_failed(
                conn,
                batch_id=batch_id,
                wallet=wallet,
                finished_ts=finished_ts,
                duration_ms=duration_ms,
                error_class=type(exc).__name__,
                error_message=str(exc),
                analyzer_run_id=_latest_run_id(conn, wallet),
            )
            _mark_intake_failed(
                conn,
                item.intake_queue_id,
                finished_ts,
                type(exc).__name__,
                str(exc),
                failure_category,
            )
        finally:
            conn.close()
        print(f"FAILED {wallet} {type(exc).__name__}: {exc}", flush=True)
        return False

    finished_ts = _now()
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    stop.set()
    if thread is not None:
        thread.join(timeout=2)

    conn = _connect(db_path)
    try:
        decision = _latest_decision(conn, wallet)
        if decision is None:
            raise RuntimeError("run_wallet_protocol persisted no current decision")
        summary_markdown = _mark_item_completed(
            conn,
            batch_id=batch_id,
            wallet=wallet,
            started_ts=started_ts,
            finished_ts=finished_ts,
            duration_ms=duration_ms,
            decision=decision,
        )
        _mark_intake_completed(conn, item.intake_queue_id, finished_ts)
    except Exception as exc:
        failure_category = classify_failure(exc)
        _mark_item_failed(
            conn,
            batch_id=batch_id,
            wallet=wallet,
            finished_ts=finished_ts,
            duration_ms=duration_ms,
            error_class=type(exc).__name__,
            error_message=str(exc),
            analyzer_run_id=_latest_run_id(conn, wallet),
        )
        _mark_intake_failed(
            conn,
            item.intake_queue_id,
            finished_ts,
            type(exc).__name__,
            str(exc),
            failure_category,
        )
        print(f"FAILED {wallet} {type(exc).__name__}: {exc}", flush=True)
        return False
    finally:
        conn.close()

    print(f"=== completed {wallet} ===", flush=True)
    print(summary_markdown, flush=True)
    return True


def _print_aggregates(conn: sqlite3.Connection, batch_id: str) -> bool:
    status_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM wallet_analysis_batch_item
        WHERE batch_id = ?
        GROUP BY status
        ORDER BY status
        """,
        (batch_id,),
    ).fetchall()
    bucket_rows = conn.execute(
        """
        SELECT action_bucket, COUNT(*) AS n
        FROM wallet_analysis_batch_item
        WHERE batch_id = ? AND action_bucket IS NOT NULL
        GROUP BY action_bucket
        ORDER BY action_bucket
        """,
        (batch_id,),
    ).fetchall()

    print("\naggregate by status", flush=True)
    status_counts = {str(row["status"]): int(row["n"]) for row in status_rows}
    for status in STATUSES:
        if status in status_counts:
            print(f"{status}: {status_counts[status]}", flush=True)

    print("aggregate by action_bucket", flush=True)
    bucket_counts = {
        str(row["action_bucket"]): int(row["n"]) for row in bucket_rows
    }
    for bucket in ACTION_BUCKETS:
        if bucket in bucket_counts:
            print(f"{bucket}: {bucket_counts[bucket]}", flush=True)

    return bool(status_counts.get("failed"))


def _finish_batch(conn: sqlite3.Connection, batch_id: str, failed: bool) -> None:
    conn.execute(
        """
        UPDATE wallet_analysis_batch
        SET status = ?,
            finished_ts = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE batch_id = ?
        """,
        ("failed" if failed else "completed", _now(), batch_id),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    control_flags = [
        bool(args.pause_batch),
        bool(args.resume_batch),
        bool(args.cancel_batch),
    ]
    if sum(control_flags) > 1:
        print(
            "--pause-batch, --resume-batch, and --cancel-batch are mutually "
            "exclusive",
            file=sys.stderr,
        )
        return 2
    if any(control_flags) and not args.batch_id:
        print("--batch-id is required with batch control actions", file=sys.stderr)
        return 2
    if (args.pause_batch or args.cancel_batch) and (
        args.retry_failed or args.from_queue or args.wallet or args.wallet_file
    ):
        print(
            "--pause-batch/--cancel-batch cannot be combined with analysis "
            "inputs",
            file=sys.stderr,
        )
        return 2
    if args.resume_batch and (
        args.retry_failed or args.from_queue or args.wallet or args.wallet_file
    ):
        print("--resume-batch cannot be combined with other inputs", file=sys.stderr)
        return 2
    if args.retry_failed and not args.batch_id:
        print("--batch-id is required with --retry-failed", file=sys.stderr)
        return 2
    if args.retry_failed and args.from_queue:
        print("--retry-failed cannot be combined with --from-queue", file=sys.stderr)
        return 2
    if args.from_queue and (args.wallet or args.wallet_file):
        print("--from-queue cannot be combined with --wallet/--wallet-file", file=sys.stderr)
        return 2
    if int(args.limit) <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 2
    if float(args.per_wallet_timeout_s) < 0:
        print("--per-wallet-timeout-s must be non-negative", file=sys.stderr)
        return 2

    batch_id = args.batch_id or f"wallet-batch-{uuid.uuid4()}"
    db_path = str(args.db)
    single_wallet_args = _single_wallet_args(args)
    wallet_timeout_seconds = (
        float(args.per_wallet_timeout_s)
        if float(args.per_wallet_timeout_s) > 0
        else None
    )

    conn = _connect(db_path)
    try:
        db.init_db(conn)
        if args.pause_batch:
            count = pause_batch(conn, batch_id)
            print(f"paused {count} unfinished wallet(s) for batch {batch_id}")
            return 0
        if args.cancel_batch:
            count = cancel_batch(conn, batch_id)
            print(f"cancelled {count} unfinished wallet(s) for batch {batch_id}")
            return 0
        if args.resume_batch:
            batch_wallets = _paused_wallets(conn, batch_id)
            if not batch_wallets:
                print(f"no paused wallets to resume for batch {batch_id}")
                return 0
            _mark_batch_running(conn, batch_id, "resume_batch")
        elif args.retry_failed:
            batch_wallets = _failed_wallets(conn, batch_id)
            if not batch_wallets:
                print(f"no failed wallets to retry for batch {batch_id}")
                return 0
            create_batch(
                conn,
                batch_id=batch_id,
                source="retry_failed",
                total_wallets=len(batch_wallets),
            )
        elif args.from_queue:
            batch_wallets = claim_intake_wallets(
                conn,
                limit=int(args.limit),
                include_failed=bool(args.retry_queue_failed),
            )
            if not batch_wallets:
                print("no wallet_analyzer_intake_queue rows to analyze")
                return 0
            create_batch(
                conn,
                batch_id=batch_id,
                source="wallet_analyzer_intake_queue",
                total_wallets=len(batch_wallets),
            )
            queue_batch_items(conn, batch_id, batch_wallets)
        else:
            try:
                batch_wallets = build_explicit_wallets(args)
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if not batch_wallets:
                print("no wallets to analyze", file=sys.stderr)
                return 2
            create_batch(
                conn,
                batch_id=batch_id,
                source=args.source,
                total_wallets=len(batch_wallets),
            )
            queue_batch_items(conn, batch_id, batch_wallets)
    finally:
        conn.close()

    print(f"batch_id: {batch_id}", flush=True)
    print(f"wallets: {len(batch_wallets)}", flush=True)

    any_failed = False
    for item in batch_wallets:
        ok = _process_wallet(
            db_path=db_path,
            batch_id=batch_id,
            item=item,
            single_wallet_args=single_wallet_args,
            heartbeat_seconds=float(args.heartbeat_seconds),
            wallet_timeout_seconds=wallet_timeout_seconds,
        )
        any_failed = any_failed or not ok

    conn = _connect(db_path)
    try:
        aggregate_failed = _print_aggregates(conn, batch_id)
        any_failed = any_failed or aggregate_failed
        _finish_batch(conn, batch_id, any_failed)
    finally:
        conn.close()

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
