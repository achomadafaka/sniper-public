"""Tests for the memecoin entry conditioner (deploy-gated feature-band gate).

Pins: default-OFF is inert, band-implied probability ordering matches the feature-store
separation, and 'on' mode blocks only below the floor. This gate touches the real-money entry
path when wired, so its default-inert behaviour is the safety-critical property.
"""
from sniper.services.memecoin_entry_conditioner import (
    band_implied_prob,
    evaluate_entry,
    mcap_band,
    size_band,
)


def test_default_off_is_inert():
    # Even the worst possible signal must be ALLOWED when the conditioner is off.
    v = evaluate_entry(mcap=50_000_000, size_usd=50_000, is_first_buy=0, mode="off", min_prob=0.10)
    assert v.allow is True
    assert v.mode == "off"


def test_shadow_mode_allows_but_reports_verdict():
    v = evaluate_entry(mcap=40_000_000, size_usd=30_000, is_first_buy=0, mode="shadow", min_prob=0.10)
    assert v.allow is True           # shadow never blocks
    assert v.mode == "shadow"
    assert "p(hit20)" in v.reason    # verdict is logged


def test_on_mode_blocks_low_prob_signal():
    # 30M+ mcap + 20k+ size + not-first-buy = the worst bands -> well below a 10% floor.
    v = evaluate_entry(mcap=45_000_000, size_usd=25_000, is_first_buy=0, mode="on", min_prob=0.10)
    assert v.allow is False
    assert v.prob < 0.10
    assert "blocked" in v.reason


def test_on_mode_passes_sweet_spot_signal():
    # 300k-1M mcap (the one dedup-robust elevated band) -> ~base * ~2x, well above a 10% floor.
    v = evaluate_entry(mcap=500_000, size_usd=2_000, is_first_buy=1, mode="on", min_prob=0.10)
    assert v.allow is True
    assert v.prob > 0.20            # ~0.132 * ~1.97 (mcap only; size 1k-5k + first-buy now neutral)
    assert "pass" in v.reason


def test_dropped_bands_are_neutral_not_boosting():
    # 1k-5k size and first_buy were dropped (not dedup-robust) -> they must NOT raise probability
    # above the mcap-only level. A first-buy 1k-5k signal in a neutral mcap band ~ base rate.
    p = evaluate_entry(mcap=3_000_000, size_usd=2_000, is_first_buy=1, mode="on", min_prob=0.10).prob
    assert abs(p - 0.132) < 0.01    # 1M-5M mcap neutral, size/fb neutral -> ~base


def test_probability_ordering_matches_feature_store():
    sweet = band_implied_prob(500_000, 2_000, 1)     # 300k-1M, 1k-5k, first-buy
    mid = band_implied_prob(3_000_000, 3_000, 1)     # 1M-5M
    bad = band_implied_prob(50_000_000, 30_000, 0)   # 30M+, 20k+, not-first
    assert sweet > mid > bad


def test_unknown_and_low_n_bands_are_neutral():
    # missing mcap -> unknown band -> LR 1.0 -> prob near base rate, not zero
    p = band_implied_prob(None, None, None)
    assert 0.05 < p < 0.25          # ~ base rate, not collapsed to 0 or inflated


def test_band_boundaries():
    assert mcap_band(500_000) == "300k-1M"
    assert mcap_band(30_000_000) == "30M+"
    assert size_band(2_000) == "1k-5k"
    assert size_band(25_000) == "20k+"
