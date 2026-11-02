"""Tests for the regime-gated vol-floor selector (campaign maker-regime-gated-v1)."""
from sniper.algos.polymarket_crypto5m import maker_regime_gated as RGF
from sniper.algos.polymarket_crypto5m import maker_regime_state as RS


def test_boundaries_stay_in_sync_with_live_classifier():
    # maker_regime_gated hardcodes the same odds/spread boundaries as the runner's live classifier
    # (maker_regime_state). If that classifier is retuned, this catches the silent divergence the
    # module docstring promises can't happen.
    # one-sided (not mid_chop) must agree across a sweep of fairs
    for f in [i / 100 for i in range(1, 100)]:
        gated_one_sided = f < RGF.MID_CHOP_LOW or f > RGF.MID_CHOP_HIGH
        live_one_sided = RS._odds_bucket(f) != "mid_chop"
        assert gated_one_sided == live_one_sided, f"odds boundary drift at fair={f}"
    # healthy spread threshold must match _spread_state's "healthy" cut
    assert RS._spread_state(RGF.SPREAD_HEALTHY_MAX) == "healthy"
    assert RS._spread_state(RGF.SPREAD_HEALTHY_MAX + 0.0001) != "healthy"

QUIET = 2e-5
CONTESTED = 1e-4


def test_quiet_one_sided_low_with_tight_book_uses_quiet_floor():
    # up-fair 0.15 (extreme_low, one-sided), spread 0.02 (healthy)
    assert RGF.is_quiet(0.15, 0.02) is True
    assert RGF.select_gated_floor(0.15, 0.02, QUIET, CONTESTED) == QUIET


def test_quiet_one_sided_high_with_tight_book_uses_quiet_floor():
    # up-fair 0.88 (extreme_high, one-sided), spread 0.01 (healthy)
    assert RGF.is_quiet(0.88, 0.01) is True
    assert RGF.select_gated_floor(0.88, 0.01, QUIET, CONTESTED) == QUIET


def test_mid_chop_uses_contested_floor_even_with_tight_book():
    # up-fair 0.50 is mid_chop -> contested regardless of a healthy spread
    assert RGF.is_quiet(0.50, 0.01) is False
    assert RGF.select_gated_floor(0.50, 0.01, QUIET, CONTESTED) == CONTESTED


def test_one_sided_but_wide_book_uses_contested_floor():
    # one-sided fair but spread 0.05 (not healthy) -> contested (do not quote tight into a wide book)
    assert RGF.is_quiet(0.15, 0.05) is False
    assert RGF.select_gated_floor(0.15, 0.05, QUIET, CONTESTED) == CONTESTED


def test_mid_chop_boundaries_are_contested():
    # exact band edges [0.40, 0.60] are mid_chop -> contested
    assert RGF.is_quiet(0.40, 0.01) is False
    assert RGF.is_quiet(0.60, 0.01) is False
    # just outside the band with a healthy book -> quiet
    assert RGF.is_quiet(0.39, 0.01) is True
    assert RGF.is_quiet(0.61, 0.01) is True


def test_spread_healthy_boundary():
    # spread exactly 0.03 is healthy (quiet); 0.031 is not
    assert RGF.is_quiet(0.20, 0.03) is True
    assert RGF.is_quiet(0.20, 0.0301) is False


def test_missing_inputs_fail_to_contested_floor():
    assert RGF.is_quiet(None, 0.01) is False
    assert RGF.is_quiet(0.15, None) is False
    assert RGF.select_gated_floor(None, None, QUIET, CONTESTED) == CONTESTED


def test_selector_returns_float():
    # integer-typed floors still come back as float
    assert isinstance(RGF.select_gated_floor(0.15, 0.02, 0, 1), float)


# ---- regime-gated-v2 continuous ramp ----
HONEST = 2e-5
WIDE = 1e-4
X_LO, X_HI = 0.15, 0.25


def rf(fair, spread=0.01):
    return RGF.ramp_floor(fair, spread, HONEST, WIDE, X_LO, X_HI)


def test_ramp_wide_when_contested_below_xlo():
    # |fair-0.5|=0.05 <= x_lo -> wide (protect)
    assert rf(0.45) == WIDE
    assert rf(0.55) == WIDE


def test_ramp_honest_when_onesided_above_xhi():
    # |fair-0.5|=0.30 >= x_hi -> honest (harvest)
    assert rf(0.20) == HONEST
    assert rf(0.80) == HONEST


def test_ramp_interpolates_in_log_space_at_midpoint():
    # x=0.20 is the midpoint of [0.15,0.25]; log-interp midpoint = geometric mean of the anchors
    import math
    got = rf(0.70)  # |0.70-0.5|=0.20
    expected = math.exp((math.log(WIDE) + math.log(HONEST)) / 2)  # sqrt(WIDE*HONEST)
    assert abs(got - expected) < 1e-12
    assert abs(got - math.sqrt(WIDE * HONEST)) < 1e-12


def test_ramp_is_monotone_and_bounded():
    lo, hi = min(HONEST, WIDE), max(HONEST, WIDE)
    prev = None
    for i in range(51):
        x = i / 100.0  # 0.00 .. 0.50
        f = rf(0.5 + x)
        assert lo - 1e-15 <= f <= hi + 1e-15  # never extrapolates beyond anchors
        if prev is not None:
            assert f <= prev + 1e-15  # floor decreases (honest) as odds get more one-sided
        prev = f


def test_ramp_boundaries_exact():
    import pytest
    # exact-boundary inputs land within FP epsilon of the anchor (0.5+0.15 -> |x-.5|=0.15000000000000002,
    # a hair above x_lo, so the interp branch returns wide to ~15 digits — continuous, no cliff)
    assert rf(0.5 + X_LO) == pytest.approx(WIDE, rel=1e-9)
    assert rf(0.5 + X_HI) == pytest.approx(HONEST, rel=1e-9)
    # inputs clearly inside each region return the anchor exactly (no FP ambiguity)
    assert RGF.ramp_floor(0.40, 0.01, HONEST, WIDE, X_LO, X_HI) == WIDE   # |x-.5|=0.10 < x_lo
    assert RGF.ramp_floor(0.20, 0.01, HONEST, WIDE, X_LO, X_HI) == HONEST # |x-.5|=0.30 > x_hi


def test_ramp_unknown_fair_protects():
    assert RGF.ramp_floor(None, 0.01, HONEST, WIDE, X_LO, X_HI) == WIDE


def test_ramp_untradeable_book_protects():
    # one-sided fair but spread 0.09 (untradeable) -> wide, do not quote tight into a broken book
    assert RGF.ramp_floor(0.20, 0.09, HONEST, WIDE, X_LO, X_HI) == WIDE
    # healthy/moderate spread does NOT force wide (ramp uses odds)
    assert RGF.ramp_floor(0.20, 0.05, HONEST, WIDE, X_LO, X_HI) == HONEST


def test_ramp_missing_spread_protects():
    # spread None (crossed/locked/missing book) -> WIDE, mirroring v1's is_quiet(). This is
    # load-bearing (~9-17% of ticks) and keeps the v1-vs-v2 A/B differing only in binary-vs-continuous.
    assert RGF.ramp_floor(0.20, None, HONEST, WIDE, X_LO, X_HI) == WIDE  # one-sided but no book -> protect
    assert RGF.ramp_floor(0.48, None, HONEST, WIDE, X_LO, X_HI) == WIDE
    # is_quiet already protects on None spread — assert the two functions agree on this edge
    assert RGF.is_quiet(0.20, None) is False


def test_ramp_nonpositive_anchor_raises_in_interior():
    # log-interpolation needs positive anchors; a non-positive anchor raises ValueError in the
    # interior band (the runner rejects this at flag-parse so it never reaches here in production).
    import pytest
    with pytest.raises(ValueError):
        RGF.ramp_floor(0.70, 0.01, 0.0, WIDE, X_LO, X_HI)   # |f-.5|=0.20 interior, honest=0 -> log(0)
    # but at/beyond the anchors it returns the anchor without calling log
    assert RGF.ramp_floor(0.80, 0.01, 0.0, WIDE, X_LO, X_HI) == 0.0  # >= x_hi -> honest (0.0), no log
