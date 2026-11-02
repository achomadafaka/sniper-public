"""Per-cell data-lineage tracking for the wallet dossier.

Spec §6.10. Every non-key, non-null cell in any dossier table must produce
at least one row here. Tests assert this invariant.

Usage:
    tracker = LineageTracker()
    tracker.record(
        table="positions",
        column="entry_price",
        row_key=position_id,
        source="fixture_field",
        source_detail="WalletData.closed_positions[i].avgPrice",
        confidence="high",
        derivation_rule=None,
    )
    ...
    rows = tracker.rows()  # for SQLite insertion
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

LineageSource = Literal[
    "fixture_field",
    "derived",
    "protocol_report_surface",
    "data_unavailable",
]
LineageConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class LineageRow:
    claim_id: str
    table: str
    column: str
    row_key: str
    source: LineageSource
    source_detail: str | None
    confidence: LineageConfidence
    unavailable_reason: str | None
    derivation_rule: str | None


def _claim_id(table: str, column: str, row_key: str) -> str:
    raw = f"{table}|{column}|{row_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass
class LineageTracker:
    """Accumulate lineage rows during dossier construction."""

    _rows: list[LineageRow] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def record(
        self,
        *,
        table: str,
        column: str,
        row_key: str,
        source: LineageSource,
        confidence: LineageConfidence,
        source_detail: str | None = None,
        unavailable_reason: str | None = None,
        derivation_rule: str | None = None,
    ) -> None:
        if source == "data_unavailable" and not unavailable_reason:
            raise ValueError(
                f"data_unavailable lineage for {table}.{column} requires unavailable_reason"
            )
        claim_id = _claim_id(table, column, row_key)
        # Idempotent on (table, column, row_key) — re-recording is a no-op
        # to keep the builder side-effect-free across retries.
        if claim_id in self._seen:
            return
        self._seen.add(claim_id)
        self._rows.append(
            LineageRow(
                claim_id=claim_id,
                table=table,
                column=column,
                row_key=row_key,
                source=source,
                source_detail=source_detail,
                confidence=confidence,
                unavailable_reason=unavailable_reason,
                derivation_rule=derivation_rule,
            )
        )

    def rows(self) -> list[LineageRow]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)
