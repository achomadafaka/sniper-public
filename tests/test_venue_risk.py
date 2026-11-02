"""
Tests for venue-aware risk policy extensions.

Validates:
- Existing Polymarket checks are unchanged (backward compat)
- venue field defaults to "polymarket"
- VenueRiskLimits dataclass works correctly
- Hyperliquid venue checks: enabled, exposure, leverage, concurrent, concentration
- Venue checks are skipped for polymarket venue (even with venue_limits)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.risk.models import RiskContext, RiskDecision, VenueRiskLimits
from sniper.risk.policy import evaluate_entry_risk


def _polymarket_ctx(**overrides) -> RiskContext:
    """Standard Polymarket risk context."""
    defaults = dict(
        bankroll_usdc=1000.0,
        total_exposure_usdc=200.0,
        market_exposure_usdc=50.0,
        concurrent_positions=5,
        daily_realized_pnl_usdc=-10.0,
        max_total_exposure_usdc=1000.0,
        max_market_exposure_usdc=200.0,
        max_concurrent_positions=20,
        daily_loss_limit_usdc=50.0,
        paper_only=True,
        real_trading_enabled=False,
        venue="polymarket",
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def _hl_ctx(**overrides) -> RiskContext:
    """Standard Hyperliquid risk context."""
    defaults = dict(
        bankroll_usdc=500.0,
        total_exposure_usdc=100.0,
        market_exposure_usdc=30.0,
        concurrent_positions=2,
        daily_realized_pnl_usdc=-5.0,
        max_total_exposure_usdc=500.0,
        max_market_exposure_usdc=200.0,
        max_concurrent_positions=20,
        daily_loss_limit_usdc=50.0,
        paper_only=True,
        real_trading_enabled=False,
        venue="hyperliquid",
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def _hl_limits(**overrides) -> VenueRiskLimits:
    """Standard Hyperliquid venue limits."""
    defaults = dict(
        max_exposure_usdc=500.0,
        max_leverage=3.0,
        max_concurrent=5,
        max_asset_concentration_pct=30.0,
    )
    defaults.update(overrides)
    return VenueRiskLimits(**defaults)


# --- Backward compatibility: Polymarket unchanged ---

class TestPolymarketBackwardCompat:

    def test_venue_defaults_to_polymarket(self):
        ctx = RiskContext(
            bankroll_usdc=1000.0,
            total_exposure_usdc=0.0,
            market_exposure_usdc=0.0,
            concurrent_positions=0,
            daily_realized_pnl_usdc=0.0,
            max_total_exposure_usdc=1000.0,
            max_market_exposure_usdc=200.0,
            max_concurrent_positions=20,
            daily_loss_limit_usdc=50.0,
            paper_only=True,
            real_trading_enabled=False,
        )
        assert ctx.venue == "polymarket"

    def test_existing_approve_unchanged(self):
        ctx = _polymarket_ctx()
        result = evaluate_entry_risk(ctx, 25.0, "cond_1", "paper")
        assert result.allowed is True
        assert result.approved_size_usdc == 25.0
        assert result.reason == "approved"

    def test_existing_reject_exposure_unchanged(self):
        ctx = _polymarket_ctx(total_exposure_usdc=990.0)
        result = evaluate_entry_risk(ctx, 50.0, "cond_1", "paper")
        assert result.allowed is False
        assert "total exposure" in result.reason

    def test_existing_clamp_market_exposure_unchanged(self):
        ctx = _polymarket_ctx(market_exposure_usdc=180.0)
        result = evaluate_entry_risk(ctx, 50.0, "cond_1", "paper")
        assert result.allowed is True
        assert result.clamped is True
        assert result.approved_size_usdc == 20.0

    def test_venue_limits_ignored_for_polymarket(self):
        """Even if venue_limits are passed, they are skipped for polymarket venue."""
        ctx = _polymarket_ctx()
        limits = VenueRiskLimits(max_exposure_usdc=0.0)  # would reject if applied
        result = evaluate_entry_risk(ctx, 25.0, "cond_1", "paper", venue_limits=limits)
        assert result.allowed is True


# --- VenueRiskLimits dataclass ---

class TestVenueRiskLimits:

    def test_defaults(self):
        limits = VenueRiskLimits()
        assert limits.max_exposure_usdc == 0.0
        assert limits.max_leverage == 1.0
        assert limits.max_concurrent == 0
        assert limits.max_asset_concentration_pct == 30.0

    def test_custom_values(self):
        limits = _hl_limits()
        assert limits.max_exposure_usdc == 500.0
        assert limits.max_leverage == 3.0
        assert limits.max_concurrent == 5


# --- Hyperliquid venue checks ---

class TestHyperliquidVenueChecks:

    def test_hl_approved_within_limits(self):
        ctx = _hl_ctx()
        limits = _hl_limits()
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
            proposed_leverage=2.0,
            asset_exposure_usdc=30.0,
        )
        assert result.allowed is True
        assert result.approved_size_usdc == 25.0

    def test_hl_venue_disabled(self):
        ctx = _hl_ctx()
        limits = _hl_limits(max_exposure_usdc=0.0)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
        )
        assert result.allowed is False
        assert "disabled" in result.reason

    def test_hl_venue_exposure_exceeded(self):
        # Global max is high (1000), but venue max is low (200).
        # total_exposure=180 + proposed=25 = 205 > venue max 200.
        ctx = _hl_ctx(total_exposure_usdc=180.0, max_total_exposure_usdc=1000.0)
        limits = _hl_limits(max_exposure_usdc=200.0)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
        )
        assert result.allowed is False
        assert "venue" in result.reason and "exposure" in result.reason

    def test_hl_leverage_exceeded(self):
        ctx = _hl_ctx()
        limits = _hl_limits(max_leverage=3.0)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
            proposed_leverage=5.0,
        )
        assert result.allowed is False
        assert "leverage" in result.reason
        assert "5.0x" in result.reason
        assert "3.0x" in result.reason

    def test_hl_leverage_at_limit(self):
        ctx = _hl_ctx()
        limits = _hl_limits(max_leverage=3.0)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
            proposed_leverage=3.0,
        )
        assert result.allowed is True

    def test_hl_concurrent_exceeded(self):
        ctx = _hl_ctx(concurrent_positions=5)
        limits = _hl_limits(max_concurrent=5)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
        )
        assert result.allowed is False
        assert "concurrent" in result.reason

    def test_hl_concurrent_zero_uses_global(self):
        """max_concurrent=0 means use global limit, not venue-level."""
        ctx = _hl_ctx(concurrent_positions=5)  # 5 < global max of 20
        limits = _hl_limits(max_concurrent=0)
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
        )
        # Venue concurrent check skipped (disabled), global check passes (5 < 20)
        assert result.allowed is True

    def test_hl_asset_concentration_exceeded(self):
        ctx = _hl_ctx()
        limits = _hl_limits(max_exposure_usdc=500.0, max_asset_concentration_pct=30.0)
        # 30% of 500 = $150 max per asset. Current $140 + proposed $25 = $165 > $150.
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
            asset_exposure_usdc=140.0,
        )
        assert result.allowed is False
        assert "concentration" in result.reason

    def test_hl_asset_concentration_within_limits(self):
        ctx = _hl_ctx()
        limits = _hl_limits(max_exposure_usdc=500.0, max_asset_concentration_pct=30.0)
        # 30% of 500 = $150 max. Current $100 + proposed $25 = $125 < $150.
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
            asset_exposure_usdc=100.0,
        )
        assert result.allowed is True

    def test_hl_global_checks_still_apply(self):
        """Global checks (bankroll, daily loss, etc.) still fire before venue checks."""
        ctx = _hl_ctx(daily_realized_pnl_usdc=-50.0, daily_loss_limit_usdc=50.0)
        limits = _hl_limits()
        result = evaluate_entry_risk(
            ctx, 25.0, "BTC", "paper",
            venue_limits=limits,
        )
        assert result.allowed is False
        assert "daily loss" in result.reason

    def test_hl_without_venue_limits_skips_venue_checks(self):
        """If no venue_limits provided, venue checks are skipped even for HL venue."""
        ctx = _hl_ctx()
        result = evaluate_entry_risk(ctx, 25.0, "BTC", "paper")
        assert result.allowed is True


# --- Instance profile HL fields ---

class TestInstanceProfileHLFields:

    def test_hl_defaults(self):
        from sniper.settings.instance_profile import load_instance_profile
        profile = load_instance_profile(env={})
        assert profile.hl_max_exposure_usdc == 0.0
        assert profile.hl_max_leverage == 3.0
        assert profile.hl_max_concurrent == 5

    def test_hl_custom_values(self):
        from sniper.settings.instance_profile import load_instance_profile
        profile = load_instance_profile(env={
            "INSTANCE_HL_MAX_EXPOSURE_USDC": "2000",
            "INSTANCE_HL_MAX_LEVERAGE": "5.0",
            "INSTANCE_HL_MAX_CONCURRENT": "10",
        })
        assert profile.hl_max_exposure_usdc == 2000.0
        assert profile.hl_max_leverage == 5.0
        assert profile.hl_max_concurrent == 10

    def test_hl_invalid_raises(self):
        from sniper.settings.instance_profile import load_instance_profile
        import pytest
        with pytest.raises(ValueError, match="INSTANCE_HL_MAX_EXPOSURE_USDC"):
            load_instance_profile(env={"INSTANCE_HL_MAX_EXPOSURE_USDC": "not_a_number"})
