from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALIDATION_STATES = {
    "paper_queued",
    "active",
    "promoted",
    "killed",
    "inconclusive",
    "replication_required",
    "descriptive_only",
    "blocked_missing_feed",
}


@dataclass(frozen=True)
class ForwardPower:
    expected_events: int
    horizon_days: int
    powered: bool


@dataclass(frozen=True)
class ValidationPlan:
    thesis_id: str
    validation_state: str
    forward_power: ForwardPower
    paper_card: Any | None = None
    paper_enrollment_id: str | None = None
    replication: dict[str, Any] = field(default_factory=lambda: {"state": "not_run"})
    null_calibration: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.validation_state not in VALIDATION_STATES:
            raise ValueError(f"unknown validation_state: {self.validation_state}")
