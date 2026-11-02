"""
tests/test_risk_policy.py — Tests for the shared risk policy layer.

Covers RiskContext, RiskDecision dataclasses and the evaluate_entry_risk() evaluator.
"""

from __future__ import annotations

import pytest

from sniper.risk.models import RiskContext, RiskDecision
from sniper.risk.policy import evaluate_entry_risk


# ---------------------------------------------------------------------------
# Helper: build a default RiskContext with generous limits (all checks pass)
# ---------------------------------------------------------------------------

def _ctx(**overrides) -> RiskContext:
    """Return a RiskContext with sensible defaults; override any field via kwargs."""
    defaults = dict(
        bankroll_usdc=10_000.0,
        total_exposure_usdc=0.0,
        market_exposure_usdc=0.0,
        concurrent_positions=0,
        daily_realized_pnl_usdc=0.0,
        max_total_exposure_usdc=10_000.0,
        max_market_exposure_usdc=200.0,
        max_concurrent_positions=20,
        daily_loss_limit_usdc=50.0,
        paper_only=False,
        real_trading_enabled=True,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------

class TestModels:
    def test_risk_context_is_frozen(self):
        ctx = _ctx()
        with pytest.raises(AttributeError):
            ctx.bankroll_usdc = 999.0  # type: ignore[misc]

    def test_risk_decision_is_frozen(self):
        d = RiskDecision(allowed=True, approved_size_usdc=50.0, reason="ok", clamped=False)
        with pytest.raises(AttributeError):
            d.allowed = False  # type: ignore[misc]

    def test_risk_context_fields(self):
        ctx = _ctx(bankroll_usdc=500.0, paper_only=True)
        assert ctx.bankroll_usdc == 500.0
        assert ctx.paper_only is True

    def test_risk_decision_fields(self):
        d = RiskDecision(allowed=False, approved_size_usdc=0.0, reason="denied", clamped=False)
        assert d.allowed is False
        assert d.approved_size_usdc == 0.0
        assert d.reason == "denied"
        assert d.clamped is False


# ---------------------------------------------------------------------------
# Policy: approve when all checks pass
# ---------------------------------------------------------------------------

class TestApproval:
    def test_approve_when_all_checks_pass(self):
        ctx = _ctx()
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True
        assert d.approved_size_usdc == 50.0
        assert d.clamped is False
        assert d.reason == "approved"

    def test_approve_paper_mode_default(self):
        """Default execution_mode is paper; paper_only=True should not reject."""
        ctx = _ctx(paper_only=True)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True

    def test_approve_real_when_paper_only_false(self):
        ctx = _ctx(paper_only=False, real_trading_enabled=True)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.allowed is True

    def test_approve_exact_bankroll_boundary(self):
        """proposed_size == bankroll should pass (not strict >)."""
        ctx = _ctx(bankroll_usdc=100.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=100.0, condition_id="mkt_1")
        assert d.allowed is True

    def test_approve_exact_total_exposure_boundary(self):
        """Exactly hitting total exposure limit should pass."""
        ctx = _ctx(total_exposure_usdc=900.0, max_total_exposure_usdc=1000.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=100.0, condition_id="mkt_1")
        assert d.allowed is True

    def test_approve_exact_concurrent_boundary(self):
        """concurrent_positions < max should pass."""
        ctx = _ctx(concurrent_positions=19, max_concurrent_positions=20)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Policy: rejections
# ---------------------------------------------------------------------------

class TestRejections:
    def test_reject_real_when_paper_only(self):
        ctx = _ctx(paper_only=True)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.allowed is False
        assert d.reason == "real trading disabled (paper_only)"

    def test_reject_real_when_not_enabled(self):
        ctx = _ctx(paper_only=False, real_trading_enabled=False)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.allowed is False
        assert d.reason == "real trading not enabled"

    def test_reject_insufficient_bankroll(self):
        ctx = _ctx(bankroll_usdc=10.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "insufficient bankroll"

    def test_reject_daily_loss_limit(self):
        ctx = _ctx(daily_realized_pnl_usdc=-50.0, daily_loss_limit_usdc=50.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "daily loss limit reached"

    def test_reject_daily_loss_limit_exceeded(self):
        """Reject when losses exceed (not just equal) the limit."""
        ctx = _ctx(daily_realized_pnl_usdc=-100.0, daily_loss_limit_usdc=50.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "daily loss limit reached"

    def test_reject_concurrent_positions_at_max(self):
        ctx = _ctx(concurrent_positions=20, max_concurrent_positions=20)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "max concurrent positions reached"

    def test_reject_total_exposure_exceeded(self):
        ctx = _ctx(total_exposure_usdc=950.0, max_total_exposure_usdc=1000.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=100.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "total exposure limit exceeded"

    def test_reject_market_exposure_at_limit(self):
        """Zero headroom -> reject (not clamp)."""
        ctx = _ctx(market_exposure_usdc=200.0, max_market_exposure_usdc=200.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "market exposure limit reached"


# ---------------------------------------------------------------------------
# Policy: clamping
# ---------------------------------------------------------------------------

class TestClamping:
    def test_clamp_to_market_headroom(self):
        ctx = _ctx(market_exposure_usdc=180.0, max_market_exposure_usdc=200.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True
        assert d.clamped is True
        assert d.approved_size_usdc == pytest.approx(20.0)
        assert d.reason == "clamped to market exposure headroom"

    def test_clamp_preserves_exact_headroom(self):
        ctx = _ctx(market_exposure_usdc=199.5, max_market_exposure_usdc=200.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=10.0, condition_id="mkt_1")
        assert d.allowed is True
        assert d.clamped is True
        assert d.approved_size_usdc == pytest.approx(0.5)

    def test_clamp_min_headroom_market_tighter(self):
        """When both headrooms are relevant, min() picks the correct one."""
        # market headroom ($20) < total headroom ($100) → clamp to market
        ctx = _ctx(
            total_exposure_usdc=900.0, max_total_exposure_usdc=1000.0,
            market_exposure_usdc=180.0, max_market_exposure_usdc=200.0,
        )
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True
        assert d.clamped is True
        assert d.approved_size_usdc == pytest.approx(20.0)  # min(20, 100) = 20

    def test_no_clamp_when_fits(self):
        ctx = _ctx(market_exposure_usdc=100.0, max_market_exposure_usdc=200.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True
        assert d.clamped is False
        assert d.approved_size_usdc == 50.0


# ---------------------------------------------------------------------------
# Policy: check ordering
# ---------------------------------------------------------------------------

class TestCheckOrdering:
    def test_paper_only_checked_before_bankroll(self):
        """Even with 0 bankroll, paper_only should be the rejection reason for real mode."""
        ctx = _ctx(paper_only=True, bankroll_usdc=0.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.reason == "real trading disabled (paper_only)"

    def test_paper_only_checked_before_real_not_enabled(self):
        """paper_only rejection takes priority over real_trading_enabled."""
        ctx = _ctx(paper_only=True, real_trading_enabled=False)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.reason == "real trading disabled (paper_only)"

    def test_real_not_enabled_checked_before_bankroll(self):
        ctx = _ctx(paper_only=False, real_trading_enabled=False, bankroll_usdc=0.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1",
                                execution_mode="real")
        assert d.reason == "real trading not enabled"

    def test_bankroll_checked_before_daily_loss(self):
        ctx = _ctx(bankroll_usdc=1.0, daily_realized_pnl_usdc=-999.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.reason == "insufficient bankroll"

    def test_daily_loss_checked_before_concurrent(self):
        ctx = _ctx(
            daily_realized_pnl_usdc=-100.0, daily_loss_limit_usdc=50.0,
            concurrent_positions=999, max_concurrent_positions=1,
        )
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.reason == "daily loss limit reached"

    def test_concurrent_checked_before_total_exposure(self):
        ctx = _ctx(
            concurrent_positions=20, max_concurrent_positions=20,
            total_exposure_usdc=99_999.0, max_total_exposure_usdc=100.0,
        )
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.reason == "max concurrent positions reached"

    def test_total_exposure_checked_before_market_exposure(self):
        ctx = _ctx(
            total_exposure_usdc=990.0, max_total_exposure_usdc=1000.0,
            market_exposure_usdc=200.0, max_market_exposure_usdc=200.0,
        )
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.reason == "total exposure limit exceeded"


# ---------------------------------------------------------------------------
# Policy: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_zero_proposed_size_rejected(self):
        ctx = _ctx()
        d = evaluate_entry_risk(ctx, proposed_size_usdc=0.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "proposed size must be positive"

    def test_negative_proposed_size_rejected(self):
        ctx = _ctx()
        d = evaluate_entry_risk(ctx, proposed_size_usdc=-10.0, condition_id="mkt_1")
        assert d.allowed is False
        assert d.reason == "proposed size must be positive"

    def test_daily_pnl_positive_does_not_reject(self):
        ctx = _ctx(daily_realized_pnl_usdc=100.0, daily_loss_limit_usdc=50.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True

    def test_daily_pnl_small_loss_does_not_reject(self):
        """Loss smaller than limit should not trigger rejection."""
        ctx = _ctx(daily_realized_pnl_usdc=-10.0, daily_loss_limit_usdc=50.0)
        d = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="mkt_1")
        assert d.allowed is True

    def test_condition_id_is_passthrough(self):
        """condition_id is accepted but not used in logic."""
        ctx = _ctx()
        d1 = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="abc")
        d2 = evaluate_entry_risk(ctx, proposed_size_usdc=50.0, condition_id="xyz")
        assert d1.allowed == d2.allowed
        assert d1.approved_size_usdc == d2.approved_size_usdc


# ---------------------------------------------------------------------------
# Package import test
# ---------------------------------------------------------------------------

class TestPackageImports:
    def test_import_from_risk_package(self):
        from sniper.risk import RiskContext, RiskDecision, evaluate_entry_risk as erf
        assert RiskContext is not None
        assert RiskDecision is not None
        assert callable(erf)
