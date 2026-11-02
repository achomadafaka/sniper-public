"""Small immutable carriers for the Hyperliquid microstructure paper lane."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeatureSnapshot:
    asset: str
    decision_ts_ms: int
    feature_version: str
    features: dict
    status: str
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyDecision:
    policy_id: str
    action: str                 # enter|skip|hold|exit
    side: str | None
    reason: str
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PaperResult:
    status: str                 # entered|skipped|held|exited
    reason: str
    position_id: int | None = None
    fill_price: float | None = None
    realized_pnl: float | None = None
