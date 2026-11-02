from __future__ import annotations

import duckdb


_VALID_COVERAGE_STATUSES = {"fully_captured", "partial", "failed"}


def _validate_range(range_start_ts: int, range_end_ts: int) -> None:
    if range_start_ts > range_end_ts:
        raise ValueError("range_start_ts must be <= range_end_ts")


def _validate_status(coverage_status: str, api_error_msg: str | None) -> None:
    if coverage_status not in _VALID_COVERAGE_STATUSES:
        raise ValueError(f"invalid coverage_status: {coverage_status}")
    if coverage_status == "failed" and not api_error_msg:
        raise ValueError("api_error_msg is required for failed coverage")
    if coverage_status != "failed" and api_error_msg is not None:
        raise ValueError("api_error_msg is only allowed for failed coverage")


def record_coverage_range(
    conn: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    source_version: str,
    range_start_ts: int,
    range_end_ts: int,
    coverage_status: str,
    source: str,
    api_error_msg: str | None,
    content_hash: str,
    first_capture_run_id: str,
    captured_at: int,
) -> int:
    _validate_range(range_start_ts, range_end_ts)
    _validate_status(coverage_status, api_error_msg)
    result = conn.execute(
        """
        INSERT INTO market_price_coverage_ranges (
            token_id,
            fidelity_minutes,
            range_start_ts,
            range_end_ts,
            source_version,
            coverage_status,
            source,
            api_error_msg,
            content_hash,
            first_capture_run_id,
            captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            token_id,
            fidelity_minutes,
            range_start_ts,
            range_end_ts,
            source_version
        ) DO NOTHING
        RETURNING token_id
        """,
        [
            token_id,
            fidelity_minutes,
            range_start_ts,
            range_end_ts,
            source_version,
            coverage_status,
            source,
            api_error_msg,
            content_hash,
            first_capture_run_id,
            captured_at,
        ],
    ).fetchone()
    return 1 if result is not None else 0


def merged_coverage_ranges(
    conn: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    source_version: str,
) -> list[tuple[int, int]]:
    rows = conn.execute(
        """
        SELECT range_start_ts, range_end_ts
        FROM market_price_coverage_ranges
        WHERE token_id = ?
          AND fidelity_minutes = ?
          AND source_version = ?
          AND coverage_status = 'fully_captured'
        ORDER BY range_start_ts, range_end_ts
        """,
        [token_id, fidelity_minutes, source_version],
    ).fetchall()

    merged: list[tuple[int, int]] = []
    for start, end in rows:
        start_i = int(start)
        end_i = int(end)
        if not merged or start_i > merged[-1][1] + 1:
            merged.append((start_i, end_i))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end_i))
    return merged


def compute_uncovered(
    conn: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    source_version: str,
    requested_start: int,
    requested_end: int,
) -> list[tuple[int, int]]:
    _validate_range(requested_start, requested_end)
    covered = merged_coverage_ranges(
        conn,
        token_id=token_id,
        fidelity_minutes=fidelity_minutes,
        source_version=source_version,
    )

    uncovered: list[tuple[int, int]] = []
    cursor = requested_start
    for start, end in covered:
        if end < cursor:
            continue
        if start > requested_end:
            break
        if start > cursor:
            uncovered.append((cursor, min(start - 1, requested_end)))
        cursor = max(cursor, end + 1)
        if cursor > requested_end:
            break

    if cursor <= requested_end:
        uncovered.append((cursor, requested_end))
    return uncovered
