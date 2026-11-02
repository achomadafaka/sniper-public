"""Slice 4 — execution probe writer.

Persists `FillProbeRecord` rows produced by `iter_fill_probes` into
`wallet_execution_probes` and `wallet_execution_probe_horizons`. Idempotent
on the spec primary keys and retains failed probes as data-quality facts.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

import duckdb

from scripts.execution_profile import FillProbeRecord
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


def _probe_uid(wallet: str, fill_event_uid: str, probe_config_hash: str) -> str:
    payload = f"{wallet}|{fill_event_uid}|{probe_config_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _enforce_horizon_invariants(
    record: FillProbeRecord,
) -> Iterable[tuple[int, float | None, int | None, int, float | None, float | None, float | None, str]]:
    """Yield horizon row tuples with the spec §11 write-time invariants applied:

    * `return_pct` MUST be NULL when `reference_price <= 0`.
    * `horizon_status` MUST be `'invalid_reference'` in that case, regardless
      of what the iterator produced.
    """
    for horizon in record.horizons:
        ref_price = horizon.reference_price
        if ref_price is None or ref_price <= 0:
            return_pct: float | None = None
            horizon_status = "invalid_reference"
        else:
            return_pct = horizon.return_pct
            horizon_status = horizon.horizon_status
        yield (
            horizon.horizon_seconds,
            ref_price,
            horizon.reference_ts,
            horizon.target_ts,
            horizon.target_price,
            horizon.drift,
            return_pct,
            horizon_status,
        )


def upsert_execution_probes(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    fill_probes: Iterable[FillProbeRecord],
    source: str,
    source_version: str,
    captured_at: int,
) -> int:
    """Write probe and horizon rows to the local wallet warehouse.

    Returns the number of probe rows newly inserted (not the count of horizon
    rows; idempotent re-runs return zero).
    """
    normalized_wallet = normalize_wallet(wallet)
    written = 0

    for record in fill_probes:
        if not record.fill_event_uid:
            # Spec requires fill_event_uid to be a foreign key into wallet_events.
            # iter_fill_probes leaves this as "" when the caller hasn't pre-joined
            # the activity rows with wallet_events. Skip silently — writer-side
            # rejection prevents foreign-key drift in the ledger.
            continue
        probe_uid = _probe_uid(
            normalized_wallet, record.fill_event_uid, record.probe_config_hash
        )

        before = conn.execute(
            """
            SELECT COUNT(*) FROM wallet_execution_probes
            WHERE wallet = ? AND fill_event_uid = ? AND probe_config_hash = ?
            """,
            [normalized_wallet, record.fill_event_uid, record.probe_config_hash],
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO wallet_execution_probes (
                wallet,
                fill_event_uid,
                probe_config_hash,
                probe_uid,
                probe_version,
                horizons_seconds_json,
                reference_rule,
                condition_id,
                token_id,
                fill_ts,
                fill_price,
                side,
                size_tokens,
                reference_price,
                reference_ts,
                probe_status,
                probe_error_msg,
                ref_tape,
                tape_n,
                in_play,
                thin_market,
                resolution_ts_unknown,
                source,
                source_version,
                capture_run_id,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet, fill_event_uid, probe_config_hash) DO NOTHING
            """,
            [
                normalized_wallet,
                record.fill_event_uid,
                record.probe_config_hash,
                probe_uid,
                record.probe_version,
                record.horizons_seconds_json,
                record.reference_rule,
                record.condition_id,
                record.token_id,
                record.fill_ts,
                record.fill_price,
                record.side,
                record.size_tokens,
                record.reference_price,
                record.reference_ts,
                record.probe_status,
                record.probe_error_msg,
                record.ref_tape,
                record.tape_n,
                record.in_play,
                record.thin_market,
                record.resolution_ts_unknown,
                source,
                source_version,
                capture_run_id,
                captured_at,
            ],
        )

        after = conn.execute(
            """
            SELECT COUNT(*) FROM wallet_execution_probes
            WHERE wallet = ? AND fill_event_uid = ? AND probe_config_hash = ?
            """,
            [normalized_wallet, record.fill_event_uid, record.probe_config_hash],
        ).fetchone()[0]
        if after > before:
            written += 1

        for (
            horizon_seconds,
            ref_price,
            ref_ts,
            target_ts,
            target_price,
            drift,
            return_pct,
            horizon_status,
        ) in _enforce_horizon_invariants(record):
            conn.execute(
                """
                INSERT INTO wallet_execution_probe_horizons (
                    wallet,
                    fill_event_uid,
                    probe_config_hash,
                    horizon_seconds,
                    reference_price,
                    reference_ts,
                    target_ts,
                    target_price,
                    drift,
                    return_pct,
                    horizon_status,
                    source,
                    source_version,
                    capture_run_id,
                    captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (wallet, fill_event_uid, probe_config_hash, horizon_seconds)
                DO NOTHING
                """,
                [
                    normalized_wallet,
                    record.fill_event_uid,
                    record.probe_config_hash,
                    horizon_seconds,
                    ref_price,
                    ref_ts,
                    target_ts,
                    target_price,
                    drift,
                    return_pct,
                    horizon_status,
                    source,
                    source_version,
                    capture_run_id,
                    captured_at,
                ],
            )

    return written


__all__ = ["upsert_execution_probes"]
