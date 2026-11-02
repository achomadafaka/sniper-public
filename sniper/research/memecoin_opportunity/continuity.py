"""Durable source continuity updates for live capture and recovery."""

from __future__ import annotations

import sqlite3

from .frontier import (
    FrontierBatch,
    GapKind,
    SourceFrontier,
    apply_frontier_batch,
    list_open_gaps,
    load_frontier,
)
from .identity import require_utc_iso


def record_processed_cursor(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    source_cursor: str,
    observed_at: str,
    decode_complete: bool,
    detail: str | None = None,
) -> SourceFrontier:
    """Advance only after raw persistence and complete decode/state application."""
    current = load_frontier(conn, source_key)
    at = require_utc_iso(observed_at)
    if decode_complete:
        matching_decode_gaps = tuple(
            row["gap_id"]
            for row in list_open_gaps(conn, source_key)
            if row["gap_kind"] == GapKind.DECODE_FAILURE.value
            and row["resume_cursor"] == source_cursor
        )
        return apply_frontier_batch(
            conn,
            FrontierBatch(
                source_key=source_key,
                expected_version=current.version if current else 0,
                previous_durable_cursor=current.durable_cursor if current else None,
                newest_observed_cursor=(
                    current.latest_observed_cursor
                    if current and matching_decode_gaps and current.latest_observed_cursor
                    else source_cursor
                ),
                contiguous_durable_cursor=source_cursor,
                resume_cursor=current.resume_cursor if current and current.has_open_gap else None,
                reached_previous_frontier=True,
                enumeration_complete=True,
                decode_complete=True,
                observed_at=at,
                resolved_gap_ids=matching_decode_gaps,
            ),
        )
    return apply_frontier_batch(
        conn,
        FrontierBatch(
            source_key=source_key,
            expected_version=current.version if current else 0,
            previous_durable_cursor=current.durable_cursor if current else None,
            newest_observed_cursor=source_cursor,
            contiguous_durable_cursor=current.durable_cursor if current else None,
            resume_cursor=(current.resume_cursor if current and current.has_open_gap else source_cursor),
            reached_previous_frontier=True,
            enumeration_complete=True,
            decode_complete=False,
            observed_at=at,
            gap_kind=GapKind.DECODE_FAILURE,
            gap_detail=detail or "decoder did not produce a canonical state",
        ),
    )


def record_source_failure(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    observed_at: str,
    detail: str,
) -> SourceFrontier:
    current = load_frontier(conn, source_key)
    resume = (
        current.resume_cursor
        if current and current.resume_cursor
        else current.latest_observed_cursor
        if current and current.latest_observed_cursor
        else current.durable_cursor
        if current and current.durable_cursor
        else "bootstrap"
    )
    return apply_frontier_batch(
        conn,
        FrontierBatch(
            source_key=source_key,
            expected_version=current.version if current else 0,
            previous_durable_cursor=current.durable_cursor if current else None,
            newest_observed_cursor=current.latest_observed_cursor if current else None,
            contiguous_durable_cursor=current.durable_cursor if current else None,
            resume_cursor=resume,
            reached_previous_frontier=False,
            enumeration_complete=False,
            decode_complete=False,
            observed_at=require_utc_iso(observed_at),
            gap_kind=GapKind.FETCH_FAILURE,
            gap_detail=detail,
        ),
    )
