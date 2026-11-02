"""Tests for the cheap dossier pre-screen (Tier-1 local, zero-API).

The screen decides analyze-vs-skip for a wallet BEFORE the expensive full
dossier, using local user_analysis snapshot fields. Conservative: only skip
clear losers; never over-filter a wallet with unknown lifetime PnL but a
positive computed PnL.
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.dossier_prescreen import (
    DEFAULT_LB_PNL_FLOOR,
    partition_wallets,
    screen_wallet_row,
)


def _row(**kw):
    base = {
        "wallet": "0x" + "a" * 40,
        "classification": "high_winrate_review",
        "lb_pnl": None,
        "pnl": 0.0,
        "wallet_quality_eligible": None,
        "wallet_quality_score": None,
        "wallet_quality_fail_reason": None,
    }
    base.update(kw)
    return base


# ===== Skip rules =====

def test_market_maker_skipped():
    r = screen_wallet_row(_row(classification="market_maker", lb_pnl=500_000))
    assert r.verdict == "skip"
    assert "excluded_class" in r.reason


def test_gate0_known_lifetime_loser_skipped():
    r = screen_wallet_row(_row(lb_pnl=-9300.0, pnl=5000.0))
    assert r.verdict == "skip"
    assert "gate0" in r.reason


def test_gate0_lifetime_at_floor_skipped():
    r = screen_wallet_row(_row(lb_pnl=0.0))
    assert r.verdict == "skip"  # 0 <= floor(0)


def test_no_lifetime_and_negative_computed_pnl_skipped():
    r = screen_wallet_row(_row(lb_pnl=None, pnl=-1200.0))
    assert r.verdict == "skip"
    assert "computed_pnl" in r.reason


def test_no_lifetime_and_zero_computed_pnl_analyzed():
    """A flat $0 computed PnL with no lifetime data is NOT a clear loser —
    only a strict loss (pnl < 0) is skipped. Conservative: don't over-filter."""
    r = screen_wallet_row(_row(lb_pnl=None, pnl=0.0))
    assert r.verdict == "analyze"


def test_wallet_quality_hard_fail_skipped():
    r = screen_wallet_row(_row(
        lb_pnl=50_000, wallet_quality_eligible=0,
        wallet_quality_fail_reason="evidence_floor_not_met",
    ))
    assert r.verdict == "skip"
    assert "ineligible" in r.reason


# ===== Analyze rules =====

def test_profitable_lifetime_analyzed():
    r = screen_wallet_row(_row(lb_pnl=743_000.0))
    assert r.verdict == "analyze"
    assert r.rank_score == 743_000.0
    assert "lb_pnl" in r.reason


def test_unknown_lifetime_but_positive_computed_pnl_analyzed():
    """Conservative: a wallet with no lb_pnl but positive computed pnl is NOT
    skipped — we don't have lifetime data so we give it the dossier."""
    r = screen_wallet_row(_row(lb_pnl=None, pnl=12_000.0))
    assert r.verdict == "analyze"
    assert r.rank_score == 12_000.0
    assert "computed_pnl" in r.reason


def test_wallet_quality_score_preferred_for_rank():
    r = screen_wallet_row(_row(lb_pnl=100_000, wallet_quality_score=88.5))
    assert r.verdict == "analyze"
    assert r.rank_score == 88.5
    assert "wallet_quality_score" in r.reason


def test_eligible_flag_1_does_not_skip():
    r = screen_wallet_row(_row(lb_pnl=50_000, wallet_quality_eligible=1,
                               wallet_quality_fail_reason=None))
    assert r.verdict == "analyze"


def test_custom_floor_raises_bar():
    # lb_pnl=5000 passes default floor(0) but fails a 10000 floor
    assert screen_wallet_row(_row(lb_pnl=5000.0)).verdict == "analyze"
    assert screen_wallet_row(_row(lb_pnl=5000.0), lb_pnl_floor=10_000.0).verdict == "skip"


# ===== Partition =====

def test_partition_sorts_analyze_by_rank_desc():
    rows = [
        _row(wallet="0xlow", lb_pnl=10_000.0),
        _row(wallet="0xhigh", lb_pnl=900_000.0),
        _row(wallet="0xmid", lb_pnl=200_000.0),
        _row(wallet="0xmm", classification="market_maker", lb_pnl=500_000.0),
        _row(wallet="0xloser", lb_pnl=-5000.0),
    ]
    out = partition_wallets(rows)
    assert [r["wallet"] for r in out["analyze"]] == ["0xhigh", "0xmid", "0xlow"]
    skip_wallets = {r["wallet"] for r in out["skip"]}
    assert skip_wallets == {"0xmm", "0xloser"}
    # Every row carries the prescreen annotations
    for r in out["analyze"] + out["skip"]:
        assert "_prescreen_verdict" in r and "_prescreen_reason" in r


def test_default_floor_is_zero():
    assert DEFAULT_LB_PNL_FLOOR == 0.0


# ===== Rank unit consistency (regression for PR #110 mixed-units bug) =====

def test_scored_wallet_outranks_unscored_high_dollar_wallet():
    """A wallet WITH wallet_quality_score=90 must rank ABOVE an unscored
    wallet with lb_pnl=$10k. Previously rank mixed units (90.0 vs 10000.0),
    so the mediocre unscored wallet sorted on top of the high-quality one.
    """
    scored = _row(wallet="0xscored", wallet_quality_score=90.0, lb_pnl=500_000.0)
    unscored = _row(wallet="0xunscored", wallet_quality_score=None, lb_pnl=10_000.0)
    out = partition_wallets([unscored, scored])
    assert [r["wallet"] for r in out["analyze"]] == ["0xscored", "0xunscored"]


def test_rank_tiers_scored_then_lbpnl_then_computed():
    """Ordering is tiered by signal quality, never by raw cross-unit value:
    all quality-scored wallets first (by score desc), then lb_pnl-ranked,
    then computed-pnl-ranked. Within a tier, higher value first.
    """
    rows = [
        _row(wallet="0xqs_low", wallet_quality_score=20.0, lb_pnl=1_000.0),
        _row(wallet="0xqs_high", wallet_quality_score=80.0, lb_pnl=1_000.0),
        _row(wallet="0xlb", wallet_quality_score=None, lb_pnl=900_000.0),
        _row(wallet="0xpnl", wallet_quality_score=None, lb_pnl=None, pnl=400_000.0),
    ]
    out = partition_wallets(rows)
    assert [r["wallet"] for r in out["analyze"]] == [
        "0xqs_high", "0xqs_low", "0xlb", "0xpnl",
    ]
