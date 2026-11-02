"""Slice 4 — reconstructed flip writer.

Reads `wallet_events` for a wallet, FIFO-matches BUY→SELL pairs per
spec §12, and writes `wallet_reconstructed_flips`. The reader contract is
strict:

* Filter to `ingest_status = 'normalized'`.
* Group by `(condition_id, token_id)` — outcome label is unsafe across
  multi-outcome markets.
* Order within group: `ts ASC, event_row_ordinal ASC, event_uid ASC`.
* Dedup by `canonical_event_hash`. Within a `dedup_status='canonical'`
  group of duplicates, only one row is taken. A `dedup_status='ambiguous'`
  group is matched once but every flip touching the ambiguous canonical
  hash is marked `derivation_check_status='unverified'`.
* `flip_pnl_collateral = NULL` whenever buy and sell collateral tokens
  differ. `flip_pnl_usd_equivalent` is always populated using
  `usd_equivalent_amount` from each side.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

import duckdb

from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet


DEFAULT_RECONSTRUCTION_METHOD = "fifo_event_pair_v1"
DEFAULT_RECONSTRUCTION_VERSION = "v1"


@dataclass(frozen=True)
class _EventRow:
    event_uid: str
    canonical_event_hash: str
    event_row_ordinal: int
    ts: int
    side: str
    condition_id: str | None
    token_id: str | None
    outcome_index: int | None
    outcome_label: str | None
    price: float | None
    size_tokens: float | None
    collateral_amount: float | None
    collateral_token: str | None
    usd_equivalent_amount: float | None
    dedup_status: str


@dataclass
class _BuyState:
    """Mutable wrapper used during FIFO consumption."""

    row: _EventRow
    remaining: float


@dataclass
class _FlipDraft:
    buy: _EventRow
    sell: _EventRow
    matched_size: float
    derivation_check_status: str


def _format_size(value: float) -> str:
    return f"{value:.18f}"


def _flip_uid(
    wallet: str,
    method: str,
    version: str,
    buy_event_uid: str,
    sell_event_uid: str,
    matched_size: float,
) -> str:
    payload = "|".join(
        [
            wallet,
            method,
            version,
            buy_event_uid,
            sell_event_uid,
            _format_size(matched_size),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_normalized_events(
    conn: duckdb.DuckDBPyConnection, wallet: str
) -> list[_EventRow]:
    rows = conn.execute(
        """
        SELECT
            event_uid, canonical_event_hash, event_row_ordinal, ts, side,
            condition_id, token_id, outcome_index, outcome_label,
            price, size_tokens, collateral_amount, collateral_token,
            usd_equivalent_amount, dedup_status
        FROM wallet_events
        WHERE wallet = ? AND ingest_status = 'normalized'
        ORDER BY ts ASC, event_row_ordinal ASC, event_uid ASC
        """,
        [wallet],
    ).fetchall()
    return [
        _EventRow(
            event_uid=row[0],
            canonical_event_hash=row[1],
            event_row_ordinal=row[2],
            ts=row[3],
            side=(row[4] or "").upper(),
            condition_id=row[5],
            token_id=row[6],
            outcome_index=row[7],
            outcome_label=row[8],
            price=row[9],
            size_tokens=row[10],
            collateral_amount=row[11],
            collateral_token=row[12],
            usd_equivalent_amount=row[13],
            dedup_status=row[14],
        )
        for row in rows
    ]


def _ambiguous_hashes(rows: Iterable[_EventRow]) -> set[str]:
    return {row.canonical_event_hash for row in rows if row.dedup_status == "ambiguous"}


def _dedup_by_canonical_hash(rows: Iterable[_EventRow]) -> list[_EventRow]:
    """Within `dedup_status='canonical'` rows, drop accidental duplicates by
    canonical_event_hash. `dedup_status='ambiguous'` rows are NOT deduped here
    — they're carried forward so the matcher can mark touched flips as
    `unverified`.

    Iteration order is preserved (already FIFO-sorted by the SQL reader).
    """
    seen_canonical: set[str] = set()
    out: list[_EventRow] = []
    for row in rows:
        if row.dedup_status == "canonical":
            if row.canonical_event_hash in seen_canonical:
                continue
            seen_canonical.add(row.canonical_event_hash)
            out.append(row)
        elif row.dedup_status == "ambiguous":
            # Keep one per canonical hash on the ambiguous side too. The first
            # row encountered (FIFO) wins; downstream marks the flip
            # `unverified` when this hash participates.
            if row.canonical_event_hash in seen_canonical:
                continue
            seen_canonical.add(row.canonical_event_hash)
            out.append(row)
        else:
            # 'duplicate' rows are skipped entirely.
            continue
    return out


def _group_by_token(
    rows: Iterable[_EventRow],
) -> dict[tuple[str, str], list[_EventRow]]:
    groups: dict[tuple[str, str], list[_EventRow]] = {}
    for row in rows:
        if not row.condition_id or not row.token_id:
            continue
        key = (row.condition_id, row.token_id)
        groups.setdefault(key, []).append(row)
    return groups


def _match_fifo(
    rows: list[_EventRow], ambiguous: set[str]
) -> list[_FlipDraft]:
    buys: list[_BuyState] = []
    drafts: list[_FlipDraft] = []
    for row in rows:
        if row.side == "BUY":
            size = float(row.size_tokens or 0.0)
            if size <= 0:
                continue
            buys.append(_BuyState(row=row, remaining=size))
        elif row.side == "SELL":
            sell_size = float(row.size_tokens or 0.0)
            if sell_size <= 0:
                continue
            while sell_size > 1e-12 and buys:
                head = buys[0]
                matched = min(head.remaining, sell_size)
                touched_ambiguous = (
                    head.row.canonical_event_hash in ambiguous
                    or row.canonical_event_hash in ambiguous
                )
                drafts.append(
                    _FlipDraft(
                        buy=head.row,
                        sell=row,
                        matched_size=matched,
                        derivation_check_status=(
                            "unverified" if touched_ambiguous else "ok"
                        ),
                    )
                )
                head.remaining -= matched
                sell_size -= matched
                if head.remaining <= 1e-12:
                    buys.pop(0)
        # Other event types are ignored.
    return drafts


def _draft_to_row_values(
    draft: _FlipDraft,
    *,
    wallet: str,
    method: str,
    version: str,
    source: str,
    source_version: str,
    capture_run_id: str,
    captured_at: int,
) -> list:
    buy = draft.buy
    sell = draft.sell

    buy_unit_price = float(buy.price or 0.0)
    sell_unit_price = float(sell.price or 0.0)
    buy_notional = buy_unit_price * draft.matched_size
    sell_notional = sell_unit_price * draft.matched_size
    flip_roi: float | None
    if buy_unit_price > 0:
        flip_roi = (sell_unit_price - buy_unit_price) / buy_unit_price
    else:
        flip_roi = None

    same_collateral = (
        buy.collateral_token is not None
        and sell.collateral_token is not None
        and buy.collateral_token == sell.collateral_token
    )
    flip_pnl_collateral = sell_notional - buy_notional if same_collateral else None

    # USD equivalent uses each side's per-token USD rate when populated.
    def _per_token_usd(row: _EventRow) -> float:
        size = float(row.size_tokens or 0.0)
        usd = row.usd_equivalent_amount
        if usd is not None and size > 0:
            return float(usd) / size
        return float(row.price or 0.0)

    buy_per_token_usd = _per_token_usd(buy)
    sell_per_token_usd = _per_token_usd(sell)
    flip_pnl_usd_equivalent = (sell_per_token_usd - buy_per_token_usd) * draft.matched_size

    collateral_conversion_assumption = (
        "matched_collateral_one_to_one"
        if same_collateral
        else "mixed_collateral_usd_equivalent_per_event"
    )

    flip_uid_value = _flip_uid(
        wallet, method, version, buy.event_uid, sell.event_uid, draft.matched_size
    )

    input_event_uids_json = json.dumps(
        [buy.event_uid, sell.event_uid], sort_keys=False, separators=(",", ":")
    )

    outcome_index = (
        buy.outcome_index
        if buy.outcome_index is not None
        else sell.outcome_index
    )
    outcome_label = buy.outcome_label or sell.outcome_label

    return [
        wallet,
        flip_uid_value,
        buy.condition_id,
        buy.token_id,
        outcome_index,
        outcome_label,
        buy.event_uid,
        sell.event_uid,
        buy.ts,
        sell.ts,
        buy_unit_price,
        sell_unit_price,
        draft.matched_size,
        buy_notional,
        sell_notional,
        buy.collateral_token,
        sell.collateral_token,
        flip_roi,
        sell.ts - buy.ts,
        flip_pnl_collateral,
        flip_pnl_usd_equivalent,
        collateral_conversion_assumption,
        draft.derivation_check_status,
        method,
        version,
        input_event_uids_json,
        source,
        source_version,
        capture_run_id,
        captured_at,
    ]


def reconstruct_and_upsert_flips(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    capture_run_id: str,
    source: str,
    source_version: str,
    captured_at: int,
    reconstruction_method: str = DEFAULT_RECONSTRUCTION_METHOD,
    reconstruction_version: str = DEFAULT_RECONSTRUCTION_VERSION,
) -> int:
    """Reconstruct flips for `wallet` from `wallet_events` and write them.

    Returns the count of flip rows newly inserted (not the count of matches;
    idempotent re-runs return zero).
    """
    normalized_wallet = normalize_wallet(wallet)
    rows = _load_normalized_events(conn, normalized_wallet)
    if not rows:
        return 0

    ambiguous = _ambiguous_hashes(rows)
    deduped = _dedup_by_canonical_hash(rows)
    groups = _group_by_token(deduped)

    drafts: list[_FlipDraft] = []
    for group_rows in groups.values():
        drafts.extend(_match_fifo(group_rows, ambiguous))

    written = 0
    for draft in drafts:
        values = _draft_to_row_values(
            draft,
            wallet=normalized_wallet,
            method=reconstruction_method,
            version=reconstruction_version,
            source=source,
            source_version=source_version,
            capture_run_id=capture_run_id,
            captured_at=captured_at,
        )
        before = conn.execute(
            "SELECT COUNT(*) FROM wallet_reconstructed_flips WHERE wallet = ? AND flip_uid = ?",
            [values[0], values[1]],
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO wallet_reconstructed_flips (
                wallet,
                flip_uid,
                condition_id,
                token_id,
                outcome_index,
                outcome_label,
                buy_event_uid,
                sell_event_uid,
                buy_ts,
                sell_ts,
                buy_price,
                sell_price,
                size_tokens,
                buy_notional_collateral,
                sell_notional_collateral,
                buy_collateral_token,
                sell_collateral_token,
                flip_roi,
                hold_duration_seconds,
                flip_pnl_collateral,
                flip_pnl_usd_equivalent,
                collateral_conversion_assumption,
                derivation_check_status,
                reconstruction_method,
                reconstruction_version,
                input_event_uids_json,
                source,
                source_version,
                capture_run_id,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (wallet, flip_uid) DO NOTHING
            """,
            values,
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM wallet_reconstructed_flips WHERE wallet = ? AND flip_uid = ?",
            [values[0], values[1]],
        ).fetchone()[0]
        if after > before:
            written += 1

    return written


__all__ = ["reconstruct_and_upsert_flips"]
