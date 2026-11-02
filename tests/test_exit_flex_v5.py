"""WO-5 — v5 flexibility constants: conviction-scaled fractions, parameter-free vol ratio, per-band
edge-duration profiles, and market-cap band boundaries."""
import pytest

from sniper.analysis import exit_flex_v5 as v5


def test_conviction_fraction_is_bounded_and_parameter_free():
    assert v5.conviction_scaled_fraction(0.0) == 0.25          # weakest -> low bound
    assert v5.conviction_scaled_fraction(1.0) == 0.75          # strongest -> high bound
    assert v5.conviction_scaled_fraction(0.5) == 0.5           # linear midpoint of [0.25, 0.75]
    assert v5.conviction_scaled_fraction(None) is None         # WO-5B: no midpoint fallback
    # clamps out-of-range ratios into the bound
    assert v5.conviction_scaled_fraction(2.0) == 0.75
    assert v5.conviction_scaled_fraction(-1.0) == 0.25


def test_vol_reference_ratio_is_saturating_and_non_degenerate():
    trail, vol = 30.0, 0.15
    ratio = v5.vol_reference_ratio(vol, trail)
    assert ratio == pytest.approx(1 / 3)
    assert v5.vol_reference_ratio(0.30, trail) == pytest.approx(0.5)
    assert v5.vol_reference_ratio(0.60, trail) == pytest.approx(2 / 3)
    assert v5.vol_reference_ratio(None, trail) is None


def test_band_edge_duration_orders_nano_below_mid():
    # measured: nano/small peak sooner than mid/large -> shorter derisk window
    assert v5.band_edge_duration_s("nano<1M") < v5.band_edge_duration_s("mid5-30M")
    assert v5.band_edge_duration_s("small1-5M") < v5.band_edge_duration_s("large>=30M")
    assert v5.band_edge_duration_s("unknown") == v5.BAND_TIME_TO_PEAK_S["mid5-30M"]


def test_funded_band_names_map_to_the_measured_market_cap_cohorts():
    """Catches sending funded nano/micro/small/mid names into research-only profile keys."""
    assert v5.band_edge_duration_s("assumed_nano") == 1524
    assert v5.band_edge_duration_s("nano") == 1524
    assert v5.band_edge_duration_s("micro") == 1524
    assert v5.band_edge_duration_s("small") == 1524
    assert v5.band_edge_duration_s("mid") == 1250


def test_market_cap_band_uses_the_preregistered_boundaries():
    assert v5.market_cap_band(None) == "unknown"
    assert v5.market_cap_band(999_999) == "nano<1M"
    assert v5.market_cap_band(1_000_000) == "small1-5M"
    assert v5.market_cap_band(5_000_000) == "mid5-30M"
    assert v5.market_cap_band(30_000_000) == "large>=30M"
