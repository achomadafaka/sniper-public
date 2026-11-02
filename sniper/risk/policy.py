"""
risk/policy.py — Shared risk policy evaluator.

Pure function: no DB access, no side effects, no strategy-specific knowledge.
The runner calls ``evaluate_entry_risk`` before every entry attempt.
"""

from __future__ import annotations

from typing import Optional

from .models import RiskContext, RiskDecision, VenueRiskLimits


def evaluate_entry_risk(
    ctx: RiskContext,
    proposed_size_usdc: float,
    condition_id: str,
    execution_mode: str = "paper",
    *,
    venue_limits: Optional[VenueRiskLimits] = None,
    proposed_leverage: float = 1.0,
    asset_exposure_usdc: float = 0.0,
) -> RiskDecision:
    """
    Evaluate whether a proposed entry is permitted under the current risk state.

    Checks are applied in strict order; the first failure wins.

    Parameters
    ----------
    ctx:
        Current portfolio state + instance limits.
    proposed_size_usdc:
        Requested position size in USDC (margin for leveraged venues).
    condition_id:
        Market identifier (for logging context; not used in logic).
    execution_mode:
        ``"paper"`` or ``"real"``.
    venue_limits:
        Per-venue risk caps. When provided and venue != polymarket,
        additional checks (leverage, venue exposure, asset concentration)
        are applied between checks 5 and 6.
    proposed_leverage:
        Leverage for this entry (1.0 for spot/binary, >1 for perps).
    asset_exposure_usdc:
        Existing exposure to this specific asset on this venue (for
        concentration checks).

    Returns
    -------
    RiskDecision
    """

    def _reject(reason: str) -> RiskDecision:
        return RiskDecision(
            allowed=False,
            approved_size_usdc=0.0,
            reason=reason,
            clamped=False,
        )

    # 1a. Paper-only guard
    if execution_mode == "real" and ctx.paper_only:
        return _reject("real trading disabled (paper_only)")

    # 1b. Real trading not enabled
    if execution_mode == "real" and not ctx.real_trading_enabled:
        return _reject("real trading not enabled")

    # 2a. Non-positive proposed size
    if proposed_size_usdc <= 0:
        return _reject("proposed size must be positive")

    # 2b. Bankroll floor
    if ctx.bankroll_usdc < proposed_size_usdc:
        return _reject("insufficient bankroll")

    # 3. Daily loss limit
    if ctx.daily_realized_pnl_usdc <= -ctx.daily_loss_limit_usdc:
        return _reject("daily loss limit reached")

    # 4. Concurrent positions cap
    if ctx.concurrent_positions >= ctx.max_concurrent_positions:
        return _reject("max concurrent positions reached")

    # 5. Total exposure cap
    if ctx.total_exposure_usdc + proposed_size_usdc > ctx.max_total_exposure_usdc:
        return _reject("total exposure limit exceeded")

    # 5v. Venue-specific checks (only when venue_limits provided)
    if venue_limits is not None and ctx.venue != "polymarket":
        # 5v-a. Venue enabled check
        if venue_limits.max_exposure_usdc <= 0:
            return _reject(f"venue {ctx.venue} is disabled (max_exposure=0)")

        # 5v-b. Venue-level exposure cap
        # NOTE (audit C12): For HL, ctx.total_exposure_usdc is HL-only (from _build_hl_risk_context)
        # and venue_limits.max_exposure_usdc comes from the same profile field as
        # ctx.max_total_exposure_usdc. This check is redundant with check 5 in the
        # single-strategy case. It becomes a true independent guard only if multiple
        # HL strategies share the same RiskContext or if the venue limit diverges from
        # the profile-level limit.
        if ctx.total_exposure_usdc + proposed_size_usdc > venue_limits.max_exposure_usdc:
            return _reject(f"venue {ctx.venue} exposure limit exceeded")

        # 5v-c. Leverage check
        if proposed_leverage > venue_limits.max_leverage:
            return _reject(
                f"leverage {proposed_leverage:.1f}x exceeds venue max {venue_limits.max_leverage:.1f}x"
            )

        # 5v-d. Concurrent positions (venue-level, if configured)
        if venue_limits.max_concurrent > 0 and ctx.concurrent_positions >= venue_limits.max_concurrent:
            return _reject(f"venue {ctx.venue} max concurrent positions reached")

        # 5v-e. Asset concentration check
        if venue_limits.max_exposure_usdc > 0 and venue_limits.max_asset_concentration_pct > 0:
            new_asset_exposure = asset_exposure_usdc + proposed_size_usdc
            max_asset_usdc = venue_limits.max_exposure_usdc * (venue_limits.max_asset_concentration_pct / 100.0)
            if new_asset_exposure > max_asset_usdc:
                return _reject(
                    f"asset concentration {new_asset_exposure:.0f} exceeds "
                    f"{venue_limits.max_asset_concentration_pct:.0f}% of venue allocation"
                )

    # 6. Market exposure — clamp or reject
    if ctx.market_exposure_usdc + proposed_size_usdc > ctx.max_market_exposure_usdc:
        market_headroom = ctx.max_market_exposure_usdc - ctx.market_exposure_usdc
        portfolio_headroom = ctx.max_total_exposure_usdc - ctx.total_exposure_usdc
        headroom = min(market_headroom, portfolio_headroom)
        if headroom <= 0:
            return _reject("market exposure limit reached")
        return RiskDecision(
            allowed=True,
            approved_size_usdc=headroom,
            reason="clamped to market exposure headroom",
            clamped=True,
        )

    # 7. All checks pass
    return RiskDecision(
        allowed=True,
        approved_size_usdc=proposed_size_usdc,
        reason="approved",
        clamped=False,
    )
