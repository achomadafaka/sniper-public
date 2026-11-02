"""
risk/models.py — Data models for the shared risk policy layer.

RiskContext captures the live state needed for risk evaluation.
RiskDecision is the output: allowed/rejected, possibly clamped size, and reason.
VenueRiskLimits carries per-venue caps (e.g. Hyperliquid leverage limits).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskContext:
    """Snapshot of current portfolio state + instance limits for risk evaluation."""

    # --- live state (built by runner from DB) ---
    bankroll_usdc: float
    total_exposure_usdc: float
    market_exposure_usdc: float
    concurrent_positions: int
    daily_realized_pnl_usdc: float

    # --- limits (from InstanceProfile) ---
    max_total_exposure_usdc: float
    max_market_exposure_usdc: float
    max_concurrent_positions: int
    daily_loss_limit_usdc: float

    # --- mode flags ---
    paper_only: bool
    real_trading_enabled: bool

    # --- venue (default preserves backward compat) ---
    venue: str = "polymarket"


@dataclass(frozen=True)
class VenueRiskLimits:
    """Per-venue risk caps, loaded from InstanceProfile env vars."""

    max_exposure_usdc: float = 0.0       # 0 = venue disabled
    max_leverage: float = 1.0
    max_concurrent: int = 0              # 0 = use global default
    max_asset_concentration_pct: float = 30.0  # max % of venue allocation in one asset


@dataclass(frozen=True)
class RiskDecision:
    """Result of a risk-policy evaluation."""

    allowed: bool
    approved_size_usdc: float
    reason: str
    clamped: bool
    duplicate_blocker: dict | None = None
    # Memecoin cross-wallet re-entry (2026-10-08): {family: blocking close} for each re-entry
    # family the caller's scope lifted on an allowed entry, else None. Evidence for grading only.
    reentry_lift: dict | None = None
