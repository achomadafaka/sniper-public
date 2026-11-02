from __future__ import annotations

from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


def _database_path(conn: duckdb.DuckDBPyConnection) -> Path | None:
    for _seq, _name, db_path in conn.execute("PRAGMA database_list").fetchall():
        if db_path:
            return Path(db_path)
    return None


def _market_path_for_wallet_conn(conn: duckdb.DuckDBPyConnection) -> Path:
    wallet_path = _database_path(conn)
    if wallet_path is None:
        raise ValueError("wallet connection must be file-backed to locate market_data.duckdb")
    return wallet_path.parent / "market_data.duckdb"


def _latest_coverage_status(
    conn_market: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    target_ts: int,
    source_version: str,
) -> str | None:
    row = conn_market.execute(
        """
        SELECT coverage_status
        FROM market_price_coverage_ranges
        WHERE token_id = ?
          AND fidelity_minutes = ?
          AND source_version = ?
          AND range_start_ts <= ?
          AND range_end_ts >= ?
        ORDER BY captured_at DESC, range_start_ts DESC, range_end_ts DESC
        LIMIT 1
        """,
        [token_id, fidelity_minutes, source_version, target_ts, target_ts],
    ).fetchone()
    return None if row is None else str(row[0])


def _nearest_match(
    conn_market: duckdb.DuckDBPyConnection,
    *,
    token_id: str,
    fidelity_minutes: int,
    target_ts: int,
    match_window_seconds: int,
    source_version: str,
) -> tuple[int, float] | None:
    row = conn_market.execute(
        """
        SELECT ts, price
        FROM market_price_points
        WHERE token_id = ?
          AND fidelity_minutes = ?
          AND source_version = ?
          AND point_status = 'captured'
          AND ABS(ts - ?) <= ?
        ORDER BY ABS(ts - ?), ts
        LIMIT 1
        """,
        [token_id, fidelity_minutes, source_version, target_ts, match_window_seconds, target_ts],
    ).fetchone()
    return None if row is None else (int(row[0]), float(row[1]))


def record_lookup_request(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    token_id: str,
    target_ts: int,
    fidelity_minutes: int,
    request_reason: str,
    match_window_seconds: int,
    source: str,
    source_version: str,
    captured_at: int,
) -> int:
    wallet_lc = normalize_wallet(wallet)
    with duckdb.connect(str(_market_path_for_wallet_conn(conn)), read_only=True) as conn_market:
        coverage_status = _latest_coverage_status(
            conn_market,
            token_id=token_id,
            fidelity_minutes=fidelity_minutes,
            target_ts=target_ts,
            source_version=source_version,
        )
        matched = None
        if coverage_status is None:
            lookup_status = "pending"
        elif coverage_status == "failed":
            lookup_status = "api_error"
        else:
            matched = _nearest_match(
                conn_market,
                token_id=token_id,
                fidelity_minutes=fidelity_minutes,
                target_ts=target_ts,
                match_window_seconds=match_window_seconds,
                source_version=source_version,
            )
            lookup_status = "matched" if matched is not None else "no_match_in_window"

    matched_price_ts = matched[0] if matched is not None else None
    matched_price = matched[1] if matched is not None else None
    match_lag_seconds = matched_price_ts - target_ts if matched_price_ts is not None else None
    match_source_version = source_version if matched is not None else None

    result = conn.execute(
        """
        INSERT INTO wallet_price_lookup_requests (
            wallet,
            capture_run_id,
            token_id,
            target_ts,
            fidelity_minutes,
            request_reason,
            lookup_status,
            matched_price_ts,
            matched_price,
            match_lag_seconds,
            match_source_version,
            source,
            source_version,
            captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            wallet,
            capture_run_id,
            token_id,
            target_ts,
            fidelity_minutes,
            request_reason
        ) DO NOTHING
        RETURNING wallet
        """,
        [
            wallet_lc,
            capture_run_id,
            token_id,
            target_ts,
            fidelity_minutes,
            request_reason,
            lookup_status,
            matched_price_ts,
            matched_price,
            match_lag_seconds,
            match_source_version,
            source,
            source_version,
            captured_at,
        ],
    ).fetchone()
    return 1 if result is not None else 0
