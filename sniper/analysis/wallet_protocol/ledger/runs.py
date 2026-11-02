from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import duckdb

from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(config).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CaptureShard:
    wallet: str
    capture_run_id: str
    surface: str
    shard_uid: str
    cursor_kind: str
    cursor_value: dict[str, Any] | None
    range_start_ts: int | None
    range_end_ts: int | None
    budget: dict[str, Any] | None
    status: str
    rows_written: int
    next_cursor: dict[str, Any] | None
    source: str
    source_version: str
    started_at: int
    finished_at: int | None
    error_class: str | None
    error_message: str | None
    content_hash: str | None


def create_capture_run(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    run_id: str,
    started_at: int,
    config: dict[str, Any],
    schema_version: str,
    source_versions: dict[str, Any],
    surfaces_requested: list[str],
) -> None:
    conn.execute(
        """
        INSERT INTO capture_runs (
            run_id,
            wallet,
            started_at,
            finished_at,
            status,
            config_hash,
            schema_version,
            source_versions_json,
            surfaces_requested_json,
            surfaces_complete_json,
            error_class,
            error_message
        )
        VALUES (?, ?, ?, NULL, 'running', ?, ?, ?, ?, ?, NULL, NULL)
        """,
        [
            run_id,
            normalize_wallet(wallet),
            started_at,
            _config_hash(config),
            schema_version,
            _json_dumps(source_versions),
            _json_dumps(surfaces_requested),
            _json_dumps({}),
        ],
    )


def complete_capture_run(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    run_id: str,
    finished_at: int,
    status: str,
    surfaces_complete: dict[str, bool],
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE capture_runs
        SET finished_at = ?,
            status = ?,
            surfaces_complete_json = ?,
            error_class = ?,
            error_message = ?
        WHERE wallet = ?
          AND run_id = ?
        """,
        [
            finished_at,
            status,
            _json_dumps(surfaces_complete),
            error_class,
            error_message,
            normalize_wallet(wallet),
            run_id,
        ],
    )


def upsert_capture_shard(conn: duckdb.DuckDBPyConnection, shard: CaptureShard) -> None:
    conn.execute(
        """
        INSERT INTO capture_shards (
            wallet,
            capture_run_id,
            surface,
            shard_uid,
            cursor_kind,
            cursor_value_json,
            range_start_ts,
            range_end_ts,
            budget_json,
            status,
            rows_written,
            next_cursor_json,
            source,
            source_version,
            started_at,
            finished_at,
            error_class,
            error_message,
            content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (wallet, capture_run_id, surface, shard_uid) DO UPDATE SET
            cursor_kind = excluded.cursor_kind,
            cursor_value_json = excluded.cursor_value_json,
            range_start_ts = excluded.range_start_ts,
            range_end_ts = excluded.range_end_ts,
            budget_json = excluded.budget_json,
            status = excluded.status,
            rows_written = excluded.rows_written,
            next_cursor_json = excluded.next_cursor_json,
            source = excluded.source,
            source_version = excluded.source_version,
            finished_at = excluded.finished_at,
            error_class = excluded.error_class,
            error_message = excluded.error_message,
            content_hash = excluded.content_hash
        """,
        [
            normalize_wallet(shard.wallet),
            shard.capture_run_id,
            shard.surface,
            shard.shard_uid,
            shard.cursor_kind,
            _json_dumps(shard.cursor_value) if shard.cursor_value is not None else None,
            shard.range_start_ts,
            shard.range_end_ts,
            _json_dumps(shard.budget) if shard.budget is not None else None,
            shard.status,
            shard.rows_written,
            _json_dumps(shard.next_cursor) if shard.next_cursor is not None else None,
            shard.source,
            shard.source_version,
            shard.started_at,
            shard.finished_at,
            shard.error_class,
            shard.error_message,
            shard.content_hash,
        ],
    )


def complete_capture_shard(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    run_id: str,
    surface: str,
    shard_uid: str,
    finished_at: int,
    rows_written: int,
    next_cursor: dict[str, Any] | None,
    content_hash: str | None,
) -> None:
    conn.execute(
        """
        UPDATE capture_shards
        SET status = 'completed',
            finished_at = ?,
            rows_written = ?,
            next_cursor_json = ?,
            content_hash = ?
        WHERE wallet = ?
          AND capture_run_id = ?
          AND surface = ?
          AND shard_uid = ?
        """,
        [
            finished_at,
            rows_written,
            _json_dumps(next_cursor) if next_cursor is not None else None,
            content_hash,
            normalize_wallet(wallet),
            run_id,
            surface,
            shard_uid,
        ],
    )


def next_pending_shard(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    run_id: str,
    surface: str,
) -> CaptureShard | None:
    row = conn.execute(
        """
        SELECT *
        FROM capture_shards
        WHERE wallet = ?
          AND capture_run_id = ?
          AND surface = ?
          AND status = 'pending'
        ORDER BY started_at ASC, shard_uid ASC
        LIMIT 1
        """,
        [normalize_wallet(wallet), run_id, surface],
    ).fetchone()
    if row is None:
        return None
    columns = [col[0] for col in conn.description]
    data = dict(zip(columns, row))
    return CaptureShard(
        wallet=data["wallet"],
        capture_run_id=data["capture_run_id"],
        surface=data["surface"],
        shard_uid=data["shard_uid"],
        cursor_kind=data["cursor_kind"],
        cursor_value=_json_loads(data["cursor_value_json"]),
        range_start_ts=data["range_start_ts"],
        range_end_ts=data["range_end_ts"],
        budget=_json_loads(data["budget_json"]),
        status=data["status"],
        rows_written=data["rows_written"],
        next_cursor=_json_loads(data["next_cursor_json"]),
        source=data["source"],
        source_version=data["source_version"],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        error_class=data["error_class"],
        error_message=data["error_message"],
        content_hash=data["content_hash"],
    )
