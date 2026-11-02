from __future__ import annotations

import hashlib
import json
from typing import Any

import duckdb

from sniper.analysis.wallet_protocol.ledger.coverage import compute_uncovered, record_coverage_range
from sniper.analysis.wallet_protocol.ledger.price_api import (
    BatchPriceRequest,
    PRICE_HISTORY_SOURCE_VERSION,
    fetch_batch_price_history,
)
from sniper.analysis.wallet_protocol.ledger.prices import MarketPricePoint, upsert_price_points


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _coverage_hash(request: BatchPriceRequest, source_version: str, status: str, api_error_msg: str | None) -> str:
    return _sha256_text(
        _json_dumps(
            {
                "token_id": request.token_id,
                "fidelity_minutes": request.fidelity_minutes,
                "range_start_ts": request.range_start_ts,
                "range_end_ts": request.range_end_ts,
                "source_version": source_version,
                "coverage_status": status,
                "api_error_msg": api_error_msg,
            }
        )
    )


def capture_missing_price_history(
    conn_market: duckdb.DuckDBPyConnection,
    client: object,
    requests: list[BatchPriceRequest],
    *,
    source_version: str = PRICE_HISTORY_SOURCE_VERSION,
    capture_run_id: str,
    captured_at: int,
) -> list[BatchPriceRequest]:
    uncovered_requests: list[BatchPriceRequest] = []
    for request in requests:
        for start, end in compute_uncovered(
            conn_market,
            token_id=request.token_id,
            fidelity_minutes=request.fidelity_minutes,
            source_version=source_version,
            requested_start=request.range_start_ts,
            requested_end=request.range_end_ts,
        ):
            uncovered_requests.append(
                BatchPriceRequest(
                    token_id=request.token_id,
                    fidelity_minutes=request.fidelity_minutes,
                    range_start_ts=start,
                    range_end_ts=end,
                )
            )

    if not uncovered_requests:
        return []

    result = fetch_batch_price_history(client, uncovered_requests, source_version)
    points_by_request = {
        (request.token_id, request.fidelity_minutes, request.range_start_ts, request.range_end_ts): []
        for request in uncovered_requests
    }
    for point in result.points:
        for request in uncovered_requests:
            if (
                point.token_id == request.token_id
                and point.fidelity_minutes == request.fidelity_minutes
                and request.range_start_ts <= point.ts <= request.range_end_ts
            ):
                points_by_request[
                    (request.token_id, request.fidelity_minutes, request.range_start_ts, request.range_end_ts)
                ].append(point)
                break

    price_rows = [
        MarketPricePoint(point.token_id, point.fidelity_minutes, point.ts, point.price)
        for point in result.points
    ]
    price_rows.extend(
        MarketPricePoint(request.token_id, request.fidelity_minutes, request.range_start_ts, None, "requested_no_data")
        for request in result.empty_requests
    )
    if price_rows:
        upsert_price_points(
            conn_market,
            price_rows,
            source_version=source_version,
            capture_run_id=capture_run_id,
            captured_at=captured_at,
        )

    failed_keys = set()
    for batch_error in result.batch_errors:
        for request in batch_error.requests:
            failed_keys.add((request.token_id, request.fidelity_minutes, request.range_start_ts, request.range_end_ts))
            record_coverage_range(
                conn_market,
                token_id=request.token_id,
                fidelity_minutes=request.fidelity_minutes,
                source_version=source_version,
                range_start_ts=request.range_start_ts,
                range_end_ts=request.range_end_ts,
                coverage_status="failed",
                source="polymarket_clob_v2",
                api_error_msg=batch_error.message,
                content_hash=_coverage_hash(request, source_version, "failed", batch_error.message),
                first_capture_run_id=capture_run_id,
                captured_at=captured_at,
            )

    empty_keys = {
        (request.token_id, request.fidelity_minutes, request.range_start_ts, request.range_end_ts)
        for request in result.empty_requests
    }
    for request in uncovered_requests:
        key = (request.token_id, request.fidelity_minutes, request.range_start_ts, request.range_end_ts)
        if key in failed_keys:
            continue
        status = "fully_captured" if key in empty_keys or points_by_request[key] else "partial"
        record_coverage_range(
            conn_market,
            token_id=request.token_id,
            fidelity_minutes=request.fidelity_minutes,
            source_version=source_version,
            range_start_ts=request.range_start_ts,
            range_end_ts=request.range_end_ts,
            coverage_status=status,
            source="polymarket_clob_v2",
            api_error_msg=None,
            content_hash=_coverage_hash(request, source_version, status, None),
            first_capture_run_id=capture_run_id,
            captured_at=captured_at,
        )

    return uncovered_requests
