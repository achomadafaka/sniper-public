from __future__ import annotations

import hashlib
import json
from typing import Any

import duckdb

from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expected_timestamps(range_start_ts: int, range_end_ts: int, fidelity_minutes: int) -> list[int]:
    if range_start_ts > range_end_ts:
        raise ValueError("range_start_ts must be <= range_end_ts")
    if fidelity_minutes <= 0:
        raise ValueError("fidelity_minutes must be positive")
    step = fidelity_minutes * 60
    return list(range(range_start_ts, range_end_ts + 1, step))


def analysis_price_manifest_hash(rows: list[dict[str, Any]]) -> str:
    payload = [
        {
            "token_id": row["token_id"],
            "fidelity_minutes": int(row["fidelity_minutes"]),
            "ts": int(row["ts"]),
            "source_version": row["source_version"],
        }
        for row in sorted(rows, key=lambda item: (item["token_id"], int(item["fidelity_minutes"]), int(item["ts"]), item["source_version"]))
    ]
    return _sha256_text(_json_dumps(payload))


def _market_manifest_rows(
    conn_market: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    range_start_ts: int,
    range_end_ts: int,
    source_version: str,
) -> list[dict[str, Any]]:
    result = conn_market.execute(
        """
        SELECT token_id, fidelity_minutes, ts, source_version
        FROM market_price_points
        WHERE token_id = ?
          AND fidelity_minutes = ?
          AND source_version = ?
          AND point_status = 'captured'
          AND ts BETWEEN ? AND ?
        ORDER BY ts
        """,
        [token_id, fidelity_minutes, source_version, range_start_ts, range_end_ts],
    )
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _assert_complete(
    rows: list[dict[str, Any]],
    *,
    range_start_ts: int,
    range_end_ts: int,
    fidelity_minutes: int,
) -> None:
    expected = _expected_timestamps(range_start_ts, range_end_ts, fidelity_minutes)
    actual = [int(row["ts"]) for row in rows]
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise ValueError(f"missing market_price_points for manifest: {missing}")


def record_analysis_price_manifest(
    conn_wallet: duckdb.DuckDBPyConnection,
    conn_market: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    window_id: str,
    token_id: str,
    fidelity_minutes: int,
    range_start_ts: int,
    range_end_ts: int,
    source_version: str,
    source: str,
    captured_at: int,
) -> int:
    rows = _market_manifest_rows(
        conn_market,
        token_id=token_id,
        fidelity_minutes=fidelity_minutes,
        range_start_ts=range_start_ts,
        range_end_ts=range_end_ts,
        source_version=source_version,
    )
    _assert_complete(
        rows,
        range_start_ts=range_start_ts,
        range_end_ts=range_end_ts,
        fidelity_minutes=fidelity_minutes,
    )
    content_hash = analysis_price_manifest_hash(rows)
    result = conn_wallet.execute(
        """
        INSERT INTO analysis_price_manifest (
            wallet,
            capture_run_id,
            window_id,
            token_id,
            fidelity_minutes,
            range_start_ts,
            range_end_ts,
            source_version,
            content_hash,
            source,
            captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            wallet,
            capture_run_id,
            window_id,
            token_id,
            range_start_ts,
            range_end_ts
        ) DO NOTHING
        RETURNING wallet
        """,
        [
            normalize_wallet(wallet),
            capture_run_id,
            window_id,
            token_id,
            fidelity_minutes,
            range_start_ts,
            range_end_ts,
            source_version,
            content_hash,
            source,
            captured_at,
        ],
    ).fetchone()
    return 1 if result is not None else 0
