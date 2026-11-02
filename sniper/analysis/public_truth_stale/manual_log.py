from __future__ import annotations

import csv
from pathlib import Path

from sniper.analysis.public_truth_stale.types import (
    ConfirmationStatus,
    ManualObservation,
    SourceLane,
)


REQUIRED_COLUMNS = {
    "signal_id",
    "market_url",
    "market_id",
    "condition_id",
    "token_id",
    "outcome",
    "rule_text_hash",
    "resolution_source_url",
    "source_lane",
    "source_family",
    "source_account_id",
    "source_url",
    "source_timestamp",
    "fetch_timestamp",
    "observed_source_value",
    "rule_match_status",
    "finality_status",
    "confirmation_status",
    "stake_usdc",
    "modeled_cost_per_share",
    "orderbook_file",
    "notes",
}


def _enum(enum_type, value: str, column: str):
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{column} has unsupported value {value!r}") from exc


def _float(row: dict[str, str], column: str) -> float:
    try:
        parsed = float(row[column])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{column} must be numeric") from exc
    if parsed < 0:
        raise ValueError(f"{column} must be non-negative")
    return parsed


def load_manual_observations(path: str | Path) -> list[ManualObservation]:
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manual log missing columns: {sorted(missing)}")

        observations: list[ManualObservation] = []
        for row in reader:
            observations.append(
                ManualObservation(
                    signal_id=row["signal_id"],
                    market_url=row["market_url"],
                    market_id=row["market_id"],
                    condition_id=row["condition_id"],
                    token_id=row["token_id"],
                    outcome=row["outcome"],
                    rule_text_hash=row["rule_text_hash"],
                    resolution_source_url=row["resolution_source_url"],
                    source_lane=_enum(SourceLane, row["source_lane"], "source_lane"),
                    source_family=row["source_family"],
                    source_account_id=row["source_account_id"],
                    source_url=row["source_url"],
                    source_timestamp=row["source_timestamp"],
                    fetch_timestamp=row["fetch_timestamp"],
                    observed_source_value=row["observed_source_value"],
                    rule_match_status=row["rule_match_status"],
                    finality_status=row["finality_status"],
                    confirmation_status=_enum(
                        ConfirmationStatus,
                        row["confirmation_status"],
                        "confirmation_status",
                    ),
                    stake_usdc=_float(row, "stake_usdc"),
                    modeled_cost_per_share=_float(row, "modeled_cost_per_share"),
                    orderbook_file=Path(row["orderbook_file"]),
                    notes=row["notes"],
                )
            )
    return observations
