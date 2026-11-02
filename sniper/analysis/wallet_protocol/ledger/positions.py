from __future__ import annotations

import hashlib
import json
from typing import Any

import duckdb

from sniper.analysis.wallet_protocol.ledger.collateral import resolve_collateral
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _first_not_none(*values: Any) -> Any:
    """Return the first argument that is not `None`.

    Slice 6 TI-1: numeric / boolean fields can legitimately be `0`,
    `0.0`, or `False`, all of which are falsy in Python. Using
    `a or b` to pick the first populated alias drops those values to
    the fallback, corrupting persisted data (e.g. `curPrice=0.0` for a
    resolved-NO position becomes `None`). This helper picks the
    first **explicitly non-None** alias regardless of truthiness.

    Strings: callers that prefer "empty string == missing" should
    keep using `or`-chained `_clean_str(...)`. This helper is for the
    numeric / bool fields where zero is data, not absence.
    """
    for value in values:
        if value is not None:
            return value
    return None


def _position_uid(wallet: str, condition_id: str, token_id: str) -> str:
    payload = f"{wallet}:{condition_id}:{token_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _status_notes(status: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "status_source": "caller",
        "api_totalBought_preserved": "api_total_bought_raw",
        "api_initialValue_preserved": "api_initial_value_raw",
        "cost_basis_rule": "avgPrice * size",
        "total_bought_collateral_rule": "null_until_reconstructed_from_wallet_events",
        "cashPnl_mapping": "stored in unrealized_pnl_collateral when present; raw row retained",
        "input_status": row.get("status"),
        "input_redeemable": row.get("redeemable"),
        "input_mergeable": row.get("mergeable"),
    }


def _normalization_notes(status: str, row: dict[str, Any], collateral_note: dict[str, Any]) -> dict[str, Any]:
    notes = _status_notes(status, row)
    notes.update(collateral_note)
    return notes


def upsert_position_snapshots(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    position_rows: list[dict[str, Any]],
    status: str,
    source: str,
    source_version: str,
    captured_at: int,
) -> int:
    wallet_lc = normalize_wallet(wallet)
    inserted = 0
    for row in position_rows:
        condition_id = _clean_str(row.get("conditionId") or row.get("condition_id"))
        token_id = _clean_str(row.get("asset") or row.get("asset_id") or row.get("token_id"))
        if not condition_id or not token_id:
            continue

        position_uid = _position_uid(wallet_lc, condition_id, token_id)
        # Numeric aliases: pick the first explicitly non-None value so
        # `0.0` survives the fallback (TI-1 — see _first_not_none).
        avg_price = _float_or_none(_first_not_none(row.get("avgPrice"), row.get("avg_price")))
        size_tokens = _float_or_none(_first_not_none(row.get("size"), row.get("size_tokens")))
        cost_basis = (
            avg_price * size_tokens
            if avg_price is not None and size_tokens is not None
            else None
        )
        closed_at = _int_or_none(_first_not_none(row.get("timestamp"), row.get("closed_at")))
        closed_at_source = "position.timestamp" if closed_at is not None and status.startswith("closed_") else None
        collateral_token, collateral_token_address, collateral_note = resolve_collateral(row, capture_ts=captured_at)

        result = conn.execute(
            """
            INSERT INTO wallet_positions (
                wallet,
                position_uid,
                condition_id,
                token_id,
                outcome_index,
                outcome_label,
                market_structure,
                status,
                is_latest,
                opened_at,
                opened_at_source,
                closed_at,
                closed_at_source,
                avg_price,
                size_tokens,
                cost_basis_collateral,
                total_bought_collateral,
                total_sold_collateral,
                realized_pnl_collateral,
                unrealized_pnl_collateral,
                current_price,
                collateral_token,
                collateral_token_address,
                api_total_bought_raw,
                api_initial_value_raw,
                normalization_notes_json,
                raw_position_json,
                is_finalized,
                finalized_at,
                finalization_reason,
                source,
                source_version,
                capture_run_id,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, true, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, false, NULL, NULL, ?, ?, ?, ?)
            ON CONFLICT (wallet, position_uid, capture_run_id) DO NOTHING
            RETURNING position_uid
            """,
            [
                wallet_lc,
                position_uid,
                condition_id,
                token_id,
                _int_or_none(_first_not_none(row.get("outcomeIndex"), row.get("outcome_index"))),
                _clean_str(row.get("outcome")),
                # Strings: empty string == missing, so `or` is correct here.
                _clean_str(row.get("marketStructure") or row.get("market_structure")),
                status,
                _int_or_none(_first_not_none(row.get("openedAt"), row.get("opened_at"))),
                "position.openedAt"
                if _first_not_none(row.get("openedAt"), row.get("opened_at")) is not None
                else None,
                closed_at,
                closed_at_source,
                avg_price,
                size_tokens,
                cost_basis,
                None,
                None,
                _float_or_none(_first_not_none(row.get("realizedPnl"), row.get("realized_pnl"))),
                _float_or_none(
                    _first_not_none(
                        row.get("cashPnl"),
                        row.get("unrealizedPnl"),
                        row.get("unrealized_pnl"),
                    )
                ),
                _float_or_none(_first_not_none(row.get("curPrice"), row.get("current_price"))),
                collateral_token,
                collateral_token_address,
                _float_or_none(_first_not_none(row.get("totalBought"), row.get("api_total_bought_raw"))),
                _float_or_none(_first_not_none(row.get("initialValue"), row.get("api_initial_value_raw"))),
                _json_dumps(_normalization_notes(status, row, collateral_note)),
                _json_dumps(row),
                source,
                source_version,
                capture_run_id,
                captured_at,
            ],
        ).fetchone()
        if result is not None:
            inserted += 1
        conn.execute(
            """
            UPDATE wallet_positions
            SET is_latest = false
            WHERE wallet = ?
              AND position_uid = ?
            """,
            [wallet_lc, position_uid],
        )
        conn.execute(
            """
            UPDATE wallet_positions
            SET is_latest = true
            WHERE wallet = ?
              AND position_uid = ?
              AND capture_run_id = (
                  SELECT capture_run_id
                  FROM wallet_positions
                  WHERE wallet = ?
                    AND position_uid = ?
                  ORDER BY captured_at DESC, capture_run_id DESC
                  LIMIT 1
              )
            """,
            [wallet_lc, position_uid, wallet_lc, position_uid],
        )

    return inserted
