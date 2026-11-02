from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

METHOD_STATES = {
    "supported_proxy",
    "supported_direct",
    "descriptive",
    "blocked_missing_feed",
    "contradicted",
}

RUNNABILITY = {
    "forward_testable",
    "replication_required",
    "data_blocked",
    "speed_required",
    "capital_required",
    "non_replicable",
    "descriptive_only",
}


@dataclass(frozen=True)
class ForwardTestDesign:
    metric: str
    horizon: str
    success_threshold: float
    kill_threshold: float
    min_forward_n: int
    stake_convention: str = "uniform_paper_unit"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def valid_forward_test_design(design: ForwardTestDesign | None) -> bool:
    return (
        design is not None
        and design.metric != ""
        and design.horizon != ""
        and design.min_forward_n > 0
        and design.stake_convention == "uniform_paper_unit"
    )


@dataclass(frozen=True)
class MethodFinding:
    method: str
    state: str
    score: float
    effect: float | None
    n: int
    evidence: dict[str, Any] = field(default_factory=dict)
    controls: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    runnability: str = "descriptive_only"
    forward_test_design: ForwardTestDesign | None = None
    n_events: int = 0          # independent-observation count for significance (defaults to n if unset)

    def __post_init__(self) -> None:
        if self.state not in METHOD_STATES:
            raise ValueError(f"unknown method state: {self.state}")
        if self.runnability not in RUNNABILITY:
            raise ValueError(f"unknown runnability: {self.runnability}")
        if self.score < 0:
            raise ValueError("score must be non-negative")
