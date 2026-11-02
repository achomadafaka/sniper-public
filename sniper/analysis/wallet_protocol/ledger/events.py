from __future__ import annotations

import hashlib
import json
from typing import Any

import duckdb

from sniper.analysis.wallet_protocol.ledger.collateral import resolve_collateral
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


NORMALIZATION_VERSION = "activity-v1"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upper(value: Any) -> str | None:
    text = _clean_str(value)
    return text.upper() if text else None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _first_not_none(*values: Any) -> Any:
    """Return the first explicitly non-None argument.

    Slice 6 TI-1: numeric / boolean fields can legitimately be `0` /
    `0.0` / `False`. The `or`-chained alias pattern drops those to the
    fallback because they are falsy. Use this helper for numeric
    field aliases. Strings still use `or`-chained `_clean_str(...)`
    where empty == missing is the desired semantic.
    """
    for value in values:
        if value is not None:
            return value
    return None


def _canonical_event_hash(row: dict[str, Any]) -> str:
    payload = {
        # Strings: `or`-chain is correct (empty string == missing).
        "condition_id": _clean_str(row.get("conditionId") or row.get("condition_id")),
        "token_id": _clean_str(row.get("asset") or row.get("asset_id") or row.get("token_id")),
        # Numeric: explicit is-not-None so 0 / 0.0 survive.
        "outcome_index": _int_or_none(_first_not_none(row.get("outcomeIndex"), row.get("outcome_index"))),
        "side": _upper(row.get("side")),
        "ts": _int_or_none(
            _first_not_none(row.get("timestamp"), row.get("createdAt"), row.get("created_at"))
        ),
        "price": _float_or_none(row.get("price")),
        "size": _float_or_none(row.get("size")),
    }
    return _sha256_text(_json_dumps(payload))


def _event_uid(source: str, row: dict[str, Any], event_row_ordinal: int) -> str:
    tx_hash = _clean_str(row.get("transactionHash") or row.get("transaction_hash")) or "no_tx"
    log_index = _clean_str(row.get("logIndex") or row.get("log_index"))
    row_component = log_index if log_index is not None else str(event_row_ordinal)
    token_component = (
        _clean_str(row.get("asset") or row.get("asset_id") or row.get("token_id"))
        or _clean_str(row.get("outcomeIndex") or row.get("outcome_index"))
        or "no_token"
    )
    return f"{source}:{tx_hash}:{row_component}:{token_component}"


def _source_event_uid(source: str, row: dict[str, Any], event_row_ordinal: int) -> str:
    tx_hash = _clean_str(row.get("transactionHash") or row.get("transaction_hash")) or "no_tx"
    log_index = _clean_str(row.get("logIndex") or row.get("log_index"))
    row_component = log_index if log_index is not None else str(event_row_ordinal)
    return f"{source}:{tx_hash}:{row_component}"


def _activity_ts(row: dict[str, Any]) -> int | None:
    return _int_or_none(
        _first_not_none(row.get("timestamp"), row.get("createdAt"), row.get("created_at"))
    )


def upsert_activity_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    activity_rows: list[dict[str, Any]],
    source: str,
    source_version: str,
    captured_at: int,
) -> int:
    """Persist one Data API activity row per affected token.

    Data API activity rows already carry a single `asset` token. Multi-token
    SPLIT/MERGE expansion needs a source payload with all affected token ids;
    this ingest path rejects/retains malformed rows but does not infer missing
    token rows.
    """
    wallet_lc = normalize_wallet(wallet)
    inserted = 0
    for ordinal, row in enumerate(activity_rows):
        event_uid = _event_uid(source, row, ordinal)
        source_event_uid = _source_event_uid(source, row, ordinal)
        canonical_hash = _canonical_event_hash(row)
        raw_json = _json_dumps(row)
        raw_sha256 = _sha256_text(raw_json)
        ts = _activity_ts(row)
        collateral_token, collateral_token_address, collateral_note = resolve_collateral(row, capture_ts=captured_at)
        ingest_status = "normalized" if ts is not None else "rejected"

        ambiguous = None
        if ingest_status == "normalized":
            ambiguous = conn.execute(
                """
                SELECT 1
                FROM wallet_events
                WHERE wallet = ?
                  AND canonical_event_hash = ?
                  AND source_event_uid <> ?
                  AND ingest_status = 'normalized'
                LIMIT 1
                """,
                [wallet_lc, canonical_hash, source_event_uid],
            ).fetchone()
        dedup_status = "ambiguous" if ambiguous else "canonical"

        result = conn.execute(
            """
            INSERT INTO wallet_events (
                wallet,
                event_uid,
                source_event_uid,
                canonical_event_hash,
                event_row_ordinal,
                source_event_type,
                ts,
                block_number,
                condition_id,
                token_id,
                outcome_index,
                outcome_label,
                market_structure,
                side,
                size_tokens,
                collateral_amount,
                fee_collateral,
                collateral_token,
                collateral_token_address,
                collateral_display_unit,
                usd_equivalent_amount,
                price,
                transaction_hash,
                log_index,
                order_hash,
                maker,
                taker,
                ingest_status,
                dedup_status,
                normalization_notes_json,
                normalization_version,
                source,
                source_version,
                capture_run_id,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet, event_uid) DO NOTHING
            RETURNING event_uid
            """,
            [
                wallet_lc,
                event_uid,
                source_event_uid,
                canonical_hash,
                ordinal,
                _upper(row.get("type")) or "UNKNOWN",
                ts or 0,
                # Numeric: explicit non-None alias selection (TI-1).
                _int_or_none(_first_not_none(row.get("blockNumber"), row.get("block_number"))),
                # Strings: `or` chain is correct (empty string == missing).
                _clean_str(row.get("conditionId") or row.get("condition_id")),
                _clean_str(row.get("asset") or row.get("asset_id") or row.get("token_id")),
                _int_or_none(_first_not_none(row.get("outcomeIndex"), row.get("outcome_index"))),
                _clean_str(row.get("outcome")),
                _clean_str(row.get("marketStructure") or row.get("market_structure")),
                _upper(row.get("side")),
                _float_or_none(row.get("size")),
                _float_or_none(
                    _first_not_none(
                        row.get("usdcSize"),
                        row.get("collateralAmount"),
                        row.get("collateral_amount"),
                    )
                ),
                _float_or_none(
                    _first_not_none(
                        row.get("fee"),
                        row.get("feeCollateral"),
                        row.get("fee_collateral"),
                    )
                ),
                collateral_token,
                collateral_token_address,
                _clean_str(row.get("collateralDisplayUnit") or row.get("collateral_display_unit")),
                _float_or_none(
                    _first_not_none(
                        row.get("usdEquivalentAmount"),
                        row.get("usd_equivalent_amount"),
                        row.get("usdcSize"),
                    )
                ),
                _float_or_none(row.get("price")),
                _clean_str(row.get("transactionHash") or row.get("transaction_hash")),
                _int_or_none(_first_not_none(row.get("logIndex"), row.get("log_index"))),
                _clean_str(row.get("orderHash") or row.get("order_hash")),
                _clean_str(row.get("maker")),
                _clean_str(row.get("taker")),
                ingest_status,
                dedup_status,
                _json_dumps(collateral_note),
                NORMALIZATION_VERSION,
                source,
                source_version,
                capture_run_id,
                captured_at,
            ],
        ).fetchone()

        if result is None:
            # canonical_event_hash is deterministic from the row payload; a no-op re-ingest cannot introduce new ambiguity.
            continue
        inserted += 1
        conn.execute(
            """
            INSERT INTO wallet_event_raw_payloads (
                wallet,
                event_uid,
                raw_json,
                raw_sha256,
                raw_payload_bytes,
                source,
                source_version,
                capture_run_id,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet, event_uid) DO NOTHING
            """,
            [
                wallet_lc,
                event_uid,
                raw_json,
                raw_sha256,
                len(raw_json.encode("utf-8")),
                source,
                source_version,
                capture_run_id,
                captured_at,
            ],
        )
        if ambiguous:
            conn.execute(
                """
                UPDATE wallet_events
                SET dedup_status = 'ambiguous'
                WHERE wallet = ?
                  AND canonical_event_hash = ?
                  AND ingest_status = 'normalized'
                """,
                [wallet_lc, canonical_hash],
            )

    return inserted
