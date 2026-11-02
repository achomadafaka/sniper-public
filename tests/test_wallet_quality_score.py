"""Tests for the wallet_quality_score scoring system (Tasks 1-4 of scoring rebuild)."""

import sqlite3
import pytest


def _make_db():
    from db import get_connection, init_db
    conn = get_connection(":memory:")
    init_db(conn)
    return conn


class TestWalletQualityScoreSchema:
    def test_user_analysis_has_wallet_quality_score_column(self):
        """wallet_quality_score column must exist in user_analysis."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "wallet_quality_score" in cols

    def test_user_analysis_has_market_maker_detected_column(self):
        """market_maker_detected column must exist in user_analysis."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "market_maker_detected" in cols

    def test_user_analysis_has_wallet_quality_eligibility_state(self):
        """Rollout must distinguish NULL/unscored from explicit hard-filter failure."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "wallet_quality_eligible" in cols
        assert "wallet_quality_fail_reason" in cols

    def test_wallet_quality_score_defaults_to_null(self):
        """wallet_quality_score should default to NULL (unscored)."""
        conn = _make_db()
        col_info = {r[1]: r for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()}
        wqs = col_info.get("wallet_quality_score")
        assert wqs is not None
        # dflt_value index is [4] in pragma table_info
        assert wqs[4] is None or wqs[4] == "NULL"

    def test_wallet_quality_eligible_defaults_to_null(self):
        """wallet_quality_eligible should default to NULL (not evaluated yet)."""
        conn = _make_db()
        col_info = {r[1]: r for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()}
        wqe = col_info.get("wallet_quality_eligible")
        assert wqe is not None
        assert wqe[4] is None or wqe[4] == "NULL"


class TestHardFilters:
    def test_gate0_fails_negative_lifetime_pnl(self):
        """Wallets with lb_pnl < -10000 should fail Gate 0."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=-50000, closed_sample_size=100,
            market_maker_detected=False
        )
        assert result["passes"] is False
        assert result["eligible"] == 0
        assert "gate0" in result["fail_reason"]
        assert result["gate0_status"] == "failed"

    def test_gate0_passes_positive_pnl(self):
        """Wallets with positive lb_pnl should pass Gate 0."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=50000, closed_sample_size=100,
            market_maker_detected=False
        )
        assert result["passes"] is True
        assert result["gate0_status"] == "passed"

    def test_gate0_passes_when_lb_pnl_is_none(self):
        """Wallets with unknown lb_pnl (None) should pass Gate 0 (rollout grace)."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=None, closed_sample_size=100,
            market_maker_detected=False
        )
        assert result["passes"] is True
        assert result["gate0_status"] == "unknown"

    def test_gate0_borderline_passes(self):
        """Wallets with lb_pnl exactly at threshold boundary should pass."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=-10000, closed_sample_size=100,
            market_maker_detected=False
        )
        # -10000 is NOT less than -10000, so should pass
        assert result["passes"] is True

    def test_market_maker_override_disqualifies(self):
        """Wallets detected as market makers should be disqualified."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=100000, closed_sample_size=100,
            market_maker_detected=True
        )
        assert result["passes"] is False
        assert result["eligible"] == 0
        assert "market_maker" in result["fail_reason"]
        # Gate 0 itself passed (positive PnL), but MM override disqualifies
        assert result["gate0_status"] == "passed"

    def test_minimum_evidence_floor(self):
        """Wallets with too few closed trades should not be scored."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=50000, closed_sample_size=3,
            market_maker_detected=False
        )
        assert result["passes"] is False
        assert result["eligible"] == 0
        assert "evidence" in result["fail_reason"]

    def test_passing_wallet(self):
        """Wallet with good P&L, enough trades, not MM should pass."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=50000, closed_sample_size=50,
            market_maker_detected=False
        )
        assert result["passes"] is True
        assert result["eligible"] == 1
        assert result["fail_reason"] is None
        assert result["gate0_status"] == "passed"

    def test_filter_order_gate0_first(self):
        """Gate 0 should be checked before market maker and evidence."""
        from sniper.analysis.legacy_wallet_scoring import _wallet_passes_hard_filters
        # All three filters would fail, but gate0 should be reported
        result = _wallet_passes_hard_filters(
            lb_pnl=-50000, closed_sample_size=3,
            market_maker_detected=True
        )
        assert "gate0" in result["fail_reason"]
        assert result["gate0_status"] == "failed"


class TestMarketMakerDetection:
    def test_detect_market_maker_from_positions(self):
        """Wallet holding both sides of >50% of markets is a market maker."""
        from sniper.analysis.legacy_wallet_scoring import _detect_market_maker
        positions = [
            {"conditionId": "cid1", "outcome": "Yes"},
            {"conditionId": "cid1", "outcome": "No"},
            {"conditionId": "cid2", "outcome": "Yes"},
            {"conditionId": "cid2", "outcome": "No"},
            {"conditionId": "cid3", "outcome": "Yes"},
        ]
        assert _detect_market_maker(positions) is True

    def test_no_market_maker_single_side(self):
        """Wallet holding only one side per market is not a market maker."""
        from sniper.analysis.legacy_wallet_scoring import _detect_market_maker
        positions = [
            {"conditionId": "cid1", "outcome": "Yes"},
            {"conditionId": "cid2", "outcome": "Yes"},
            {"conditionId": "cid3", "outcome": "No"},
        ]
        assert _detect_market_maker(positions) is False

    def test_no_market_maker_empty(self):
        """Empty positions should not detect as market maker."""
        from sniper.analysis.legacy_wallet_scoring import _detect_market_maker
        assert _detect_market_maker([]) is False

    def test_market_maker_threshold_at_50_percent(self):
        """Exactly 50% both-sides should NOT be market maker (need >50%)."""
        from sniper.analysis.legacy_wallet_scoring import _detect_market_maker
        positions = [
            {"conditionId": "cid1", "outcome": "Yes"},
            {"conditionId": "cid1", "outcome": "No"},
            {"conditionId": "cid2", "outcome": "Yes"},
        ]
        # 1 out of 2 markets has both sides = 50%, not >50%
        assert _detect_market_maker(positions) is False

    def test_market_maker_above_threshold(self):
        """More than 50% both-sides should detect as market maker."""
        from sniper.analysis.legacy_wallet_scoring import _detect_market_maker
        positions = [
            {"conditionId": "cid1", "outcome": "Yes"},
            {"conditionId": "cid1", "outcome": "No"},
            {"conditionId": "cid2", "outcome": "Yes"},
            {"conditionId": "cid2", "outcome": "No"},
            {"conditionId": "cid3", "outcome": "Yes"},
            {"conditionId": "cid3", "outcome": "No"},
            {"conditionId": "cid4", "outcome": "Yes"},
        ]
        # 3 out of 4 markets = 75% > 50%
        assert _detect_market_maker(positions) is True


class TestWalletQualityScore:
    def test_high_pnl_wallet_gets_high_score(self):
        """SwissMiss-like wallet: +$956K lb_pnl should score very high."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=956000, closed_realized_pnl=12500000, open_cash_pnl=240000,
            qualifying_roi=12.9, closed_sample_size=485, total_capital_deployed=88000000
        )
        assert score >= 80.0

    def test_losing_wallet_gets_low_score(self):
        """Talvez10-like wallet: -$379K should score near zero."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=-379000, closed_realized_pnl=-338000, open_cash_pnl=7000,
            qualifying_roi=-1.9, closed_sample_size=237, total_capital_deployed=19000000
        )
        assert score <= 10.0

    def test_hidden_open_losses_reduce_score(self):
        """How.Dare.You-like: +$466K closed but -$389K open should be penalized."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        # Without open losses
        score_clean = _compute_wallet_quality_score(
            lb_pnl=76000, closed_realized_pnl=466000, open_cash_pnl=0,
            qualifying_roi=3.4, closed_sample_size=411, total_capital_deployed=3000000
        )
        # With open losses
        score_underwater = _compute_wallet_quality_score(
            lb_pnl=76000, closed_realized_pnl=466000, open_cash_pnl=-389000,
            qualifying_roi=3.4, closed_sample_size=411, total_capital_deployed=3000000
        )
        assert score_underwater < score_clean

    def test_no_penalty_for_high_frequency(self):
        """Systematic/algorithmic behavior should NOT reduce score."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=200000, closed_realized_pnl=200000, open_cash_pnl=0,
            qualifying_roi=15.0, closed_sample_size=500, total_capital_deployed=1000000
        )
        assert score >= 60.0

    def test_small_sample_gets_lower_confidence(self):
        """Wallet with few trades should score lower than same P&L with many trades."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score_small = _compute_wallet_quality_score(
            lb_pnl=50000, closed_realized_pnl=50000, open_cash_pnl=0,
            qualifying_roi=20.0, closed_sample_size=15, total_capital_deployed=100000
        )
        score_large = _compute_wallet_quality_score(
            lb_pnl=50000, closed_realized_pnl=50000, open_cash_pnl=0,
            qualifying_roi=20.0, closed_sample_size=200, total_capital_deployed=100000
        )
        assert score_large > score_small

    def test_score_range_is_0_to_100(self):
        """Score must always be in [0, 100]."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        for lb in [-1000000, 0, 50000, 1000000]:
            score = _compute_wallet_quality_score(
                lb_pnl=lb, closed_realized_pnl=lb, open_cash_pnl=0,
                qualifying_roi=10.0, closed_sample_size=100, total_capital_deployed=500000
            )
            assert 0.0 <= score <= 100.0

    def test_none_inputs_handled_gracefully(self):
        """All None inputs should produce a valid score (0 or near-zero)."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=None, closed_realized_pnl=None, open_cash_pnl=None,
            qualifying_roi=None, closed_sample_size=0, total_capital_deployed=None
        )
        assert 0.0 <= score <= 100.0

    def test_lb_pnl_none_falls_back_to_closed_realized(self):
        """When lb_pnl is None, should use closed_realized_pnl as fallback."""
        from sniper.analysis.legacy_wallet_scoring import _compute_wallet_quality_score
        score_with_lb = _compute_wallet_quality_score(
            lb_pnl=100000, closed_realized_pnl=100000, open_cash_pnl=0,
            qualifying_roi=10.0, closed_sample_size=100, total_capital_deployed=500000
        )
        score_fallback = _compute_wallet_quality_score(
            lb_pnl=None, closed_realized_pnl=100000, open_cash_pnl=0,
            qualifying_roi=10.0, closed_sample_size=100, total_capital_deployed=500000
        )
        # Fallback should produce same score since lb_pnl falls back to closed_realized_pnl
        assert score_with_lb == score_fallback


class TestPaperTraderWalletQualityGate:
    """Test that the paper trader setting exists and has correct default."""

    def test_paper_min_wallet_quality_score_setting_exists(self):
        """PAPER_MIN_WALLET_QUALITY_SCORE must be importable from paper_settings."""
        from sniper.settings.paper import PAPER_MIN_WALLET_QUALITY_SCORE
        assert PAPER_MIN_WALLET_QUALITY_SCORE == 30.0

    def test_paper_min_wallet_quality_score_is_float(self):
        """Setting must be a float for comparison with DB values."""
        from sniper.settings.paper import PAPER_MIN_WALLET_QUALITY_SCORE
        assert isinstance(PAPER_MIN_WALLET_QUALITY_SCORE, float)

    def test_paper_trader_imports_wallet_quality_setting(self):
        """paper_trader.py must import the threshold constant."""
        import paper_trader
        assert hasattr(paper_trader, 'PAPER_MIN_WALLET_QUALITY_SCORE')
        assert paper_trader.PAPER_MIN_WALLET_QUALITY_SCORE == 30.0
