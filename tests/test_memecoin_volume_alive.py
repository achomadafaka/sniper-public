"""Regression tests for volume_alive_decision — the mcap-relative alive-token minimum."""
import pytest

from sniper.services.memecoin_real_runner import volume_alive_decision


def test_missing_volume_fails_open():
    allow, need, why = volume_alive_decision(None, 10_000_000, abs_floor=3000, turnover_floor=0.01)
    assert allow and why == "no_volume_data"


def test_turnover_blocks_dead_large_cap():
    # $10M coin at $50k/h = 0.5%/h turnover — dead by the 1% bar, though far over any flat floor
    allow, need, why = volume_alive_decision(50_000, 10_000_000, abs_floor=3000, turnover_floor=0.01)
    assert not allow and need == 100_000 and why == "turnover_under_floor"


def test_turnover_admits_alive_small_cap():
    # $50k coin at $2k/h = 4%/h turnover — alive, though under the old $3k flat floor
    allow, need, why = volume_alive_decision(2_000, 50_000, abs_floor=3000, turnover_floor=0.01)
    assert allow and need == 500 and why == "ok"


def test_unknown_mcap_falls_back_to_flat_floor():
    allow, _n, why = volume_alive_decision(1_000, None, abs_floor=3000, turnover_floor=0.01)
    assert not allow and why == "under_abs_floor"
    allow, _n, why = volume_alive_decision(5_000, None, abs_floor=3000, turnover_floor=0.01)
    assert allow and why == "ok"


def test_all_knobs_off_allows_everything():
    allow, _n, why = volume_alive_decision(1.0, 50_000_000, abs_floor=0, turnover_floor=0)
    assert allow and why == "ok"


def test_zero_turnover_keeps_flat_floor_behavior():
    allow, need, why = volume_alive_decision(1_000, 10_000_000, abs_floor=3000, turnover_floor=0)
    assert not allow and need == 3000 and why == "under_abs_floor"


def test_m5_leg_blocks_flat_now_despite_hot_h1():
    # Momota shape: $976k mcap, $579k/h h1 (crash volume) but a flat last-5-min tape
    allow, need, why = volume_alive_decision(
        579_000, 976_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=500, m5_floor=0.002)
    assert not allow and why == "m5_turnover_under_floor" and round(need) == 1952


def test_m5_leg_passes_live_coin():
    allow, _n, why = volume_alive_decision(
        579_000, 976_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=5_000, m5_floor=0.002)
    assert allow and why == "ok"


def test_m5_missing_fails_open_to_h1_leg():
    allow, _n, why = volume_alive_decision(
        50_000, 10_000_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=None, m5_floor=0.002)
    assert not allow and why == "turnover_under_floor"


def test_m5_off_keeps_prior_behavior():
    allow, _n, why = volume_alive_decision(
        2_000, 50_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=0.0, m5_floor=0)
    assert allow and why == "ok"


def test_ceiling_scoped_off_above_its_own_mcap_bound():
    """happyseb 2026-08-25: $224.1k mcap, m5 $543,894 = 2.43x. The ceiling refused it as a float
    dump. Above MEMECOIN_REAL_TURNOVER_CEILING_MAX_MCAP_USD the ceiling is silent."""
    kw = dict(abs_floor=0.0, turnover_floor=0.0, vol_m5=543_894.0,
              m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    # shared bound only (the old behaviour): refused
    allow, _need, why = volume_alive_decision(None, 224_100.0, **kw)
    assert not allow and why == "m5_turnover_over_ceiling"
    # ceiling scoped to 100k: this coin is above it, so the ceiling does not apply
    allow, _need, why = volume_alive_decision(
        None, 224_100.0, m5_ceiling_max_mcap=100_000.0, **kw)
    assert allow, why


def test_scoping_the_ceiling_does_not_silence_the_floor():
    """The floor keeps its own (shared) bound: a dead $224.1k coin is still refused with the
    ceiling scoped out at 100k."""
    allow, need, why = volume_alive_decision(
        None, 224_100.0, abs_floor=0.0, turnover_floor=0.0, vol_m5=100.0,
        m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0,
        m5_ceiling_max_mcap=100_000.0)
    assert not allow and why == "m5_turnover_under_floor"
    assert round(need) == 22_410


def test_ceiling_still_fires_on_a_microcap_float_dump():
    """The mechanism the ceiling exists for is unchanged below its bound."""
    allow, _need, why = volume_alive_decision(
        None, 10_000.0, abs_floor=0.0, turnover_floor=0.0, vol_m5=40_000.0,
        m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0,
        m5_ceiling_max_mcap=100_000.0)
    assert not allow and why == "m5_turnover_over_ceiling"


def test_ceiling_bound_unset_falls_back_to_the_shared_band_bound():
    """Omitting the new knob must reproduce the pre-existing behaviour exactly."""
    kw = dict(abs_floor=0.0, turnover_floor=0.0, vol_m5=543_894.0,
              m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    assert volume_alive_decision(None, 224_100.0, **kw) == \
        volume_alive_decision(None, 224_100.0, m5_ceiling_max_mcap=0.0, **kw)


def test_ceiling_bound_intersects_the_band_bound_and_never_widens_it():
    """Peer review 2026-08-25 (F2): `ceil_ok = mc <= ceiling_max` discarded band_ok.

    A ceiling bound set ABOVE the band bound would then fire the ceiling above the mcap range the
    band was fitted on — the extrapolation the band bound exists to prevent. The two bounds must
    intersect, so the ceiling is silent wherever EITHER bound says it is.
    """
    allow, _need, why = volume_alive_decision(
        None, 400_000.0, abs_floor=0.0, turnover_floor=0.0, vol_m5=2_000_000.0,
        m5_floor=0.10, m5_ceiling=1.50,
        m5_band_max_mcap=250_000.0, m5_ceiling_max_mcap=1_000_000.0)
    assert allow, f"ceiling fired above the band bound: {why}"


def test_agents_coin_is_refused_at_150k_and_admitted_at_100k():
    """The measured cell that decided the bound: AGENTS #44659, $117,847 mcap, m5 $226,004 = 1.92x,
    closed -41.0%. Of the 6 funded closes that breach 1.5x, a 100k bound is the only one of
    100k/150k/250k whose admitted set is net negative."""
    kw = dict(abs_floor=0.0, turnover_floor=0.0, vol_m5=226_004.0,
              m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    assert volume_alive_decision(None, 117_847.0, m5_ceiling_max_mcap=150_000.0, **kw)[2] \
        == "m5_turnover_over_ceiling"
    assert volume_alive_decision(None, 117_847.0, m5_ceiling_max_mcap=100_000.0, **kw)[0] is True
    # and the trigger case stays exempt at 150k, which is why 150k costs nothing
    assert volume_alive_decision(None, 224_100.0, m5_ceiling_max_mcap=150_000.0,
                                 **{**kw, "vol_m5": 543_894.0})[0] is True


# ------------------------------------------- the over-band floor (2026-08-26)

def test_a_dead_giant_passes_h1_and_is_refused_by_the_over_band_m5_floor():
    """44690, verbatim: mcap $9.28M, h1 $110k (1%/h passes), m5 $18.5k = 0.2% moving NOW."""
    allow, need, why = volume_alive_decision(
        110_101, 9_282_777, abs_floor=3000, turnover_floor=0.01,
        vol_m5=18_476, m5_floor=0.10, m5_band_max_mcap=250_000, m5_floor_over_band=0.03)
    assert allow is False
    assert why == "m5_turnover_under_big_floor"
    assert need == pytest.approx(278_483.31)


def test_an_alive_big_coin_passes_the_over_band_floor():
    """44692, verbatim: mcap $1.07M, m5 $59.7k = 5.6%. A winner; must stay admitted."""
    allow, _n, why = volume_alive_decision(
        80_388, 1_074_719, abs_floor=3000, turnover_floor=0.01,
        vol_m5=59_725, m5_floor=0.10, m5_band_max_mcap=250_000, m5_floor_over_band=0.03)
    assert allow is True and why == "ok"


def test_the_winner_nearest_the_bar_still_passes_at_three_percent():
    """44694, verbatim: +$12.60 at 3.3% turnover, $404k mcap. The tightest measured winner —
    margin 1.1x. Any taper still near the in-band 10% just above the band refuses this row,
    which is why the over-band leg is a step to its own fitted ratio, not a curve from 10%."""
    allow, _n, why = volume_alive_decision(
        106_828, 403_593, abs_floor=3000, turnover_floor=0.01,
        vol_m5=13_328, m5_floor=0.10, m5_band_max_mcap=250_000, m5_floor_over_band=0.03)
    assert allow is True and why == "ok"


def test_the_over_band_floor_unset_keeps_the_old_silence():
    """Default 0 = the pre-existing behaviour: over-band m5 is not consulted."""
    allow, _n, _w = volume_alive_decision(
        110_101, 9_282_777, abs_floor=3000, turnover_floor=0.01,
        vol_m5=18_476, m5_floor=0.10, m5_band_max_mcap=250_000)
    assert allow is True


def test_the_over_band_reason_is_distinct_so_exact_string_consumers_stay_scoped():
    """revival watch keys on the exact in-band string; a big-cap refusal must not enqueue there."""
    from sniper.services.memecoin_revival_watch import FLOOR_REASON
    assert FLOOR_REASON == "m5_turnover_under_floor"
    assert "m5_turnover_under_big_floor" != FLOOR_REASON
    assert FLOOR_REASON not in "m5_turnover_under_big_floor"


def test_missing_m5_fails_closed_when_required():
    """44693 entered a floor-scoped coin with m5 unreadable. Fail-closed is opt-in."""
    allow, _n, why = volume_alive_decision(
        50_000, 17_543, abs_floor=3000, turnover_floor=0.01,
        vol_m5=None, m5_floor=0.10, m5_band_max_mcap=250_000, m5_required=True)
    assert allow is False and why == "m5_volume_unreadable"


def test_missing_m5_still_fails_open_by_default():
    allow, _n, _w = volume_alive_decision(
        50_000, 17_543, abs_floor=3000, turnover_floor=0.01,
        vol_m5=None, m5_floor=0.10, m5_band_max_mcap=250_000)
    assert allow is True


def test_missing_m5_fails_closed_over_band_too_when_required():
    allow, _n, why = volume_alive_decision(
        110_101, 9_282_777, abs_floor=3000, turnover_floor=0.01,
        vol_m5=None, m5_floor=0.10, m5_band_max_mcap=250_000,
        m5_floor_over_band=0.03, m5_required=True)
    assert allow is False and why == "m5_volume_unreadable"


def test_the_in_band_floor_and_ceiling_are_untouched_by_the_new_knobs():
    allow, _n, why = volume_alive_decision(
        50_000, 100_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=5_000, m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000,
        m5_ceiling_max_mcap=150_000, m5_floor_over_band=0.03, m5_required=True)
    assert allow is False and why == "m5_turnover_under_floor"


# ------------------------------------- signal-volume exclusion (2026-08-26)

def test_the_signal_cannot_be_the_volume_that_admits_it():
    """$2.5k buy on a $20k coin: raw m5 $2.6k passes a 10% floor with $100 of organic flow."""
    allow, _n, why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=2_600, m5_floor=0.10, m5_band_max_mcap=250_000, signal_usd=2_500)
    assert allow is False and why == "m5_turnover_under_floor"


def test_organic_volume_clearing_the_floor_still_admits():
    allow, _n, why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=5_000, m5_floor=0.10, m5_band_max_mcap=250_000, signal_usd=2_500)
    assert allow is True and why == "ok"


def test_the_ceiling_always_reads_RAW_volume():
    """Churn is churn whoever traded it — subtracting from a safety bound would loosen it."""
    allow, _n, why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=40_000, m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000,
        m5_ceiling_max_mcap=250_000, signal_usd=15_000)
    assert allow is False and why == "m5_turnover_over_ceiling"   # raw 2.0x fires; organic 1.25x must not save it


def test_h1_floor_also_judges_organic_volume():
    allow, _n, why = volume_alive_decision(
        2_100, 200_000, abs_floor=0, turnover_floor=0.01,
        vol_m5=None, m5_floor=0.0, signal_usd=2_000)
    assert allow is False and why == "turnover_under_floor"       # organic $100 < $2k need


def test_no_signal_size_subtracts_nothing():
    allow, _n, why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=2_600, m5_floor=0.10, m5_band_max_mcap=250_000, signal_usd=None)
    assert allow is True and why == "ok"


def test_a_buy_larger_than_observed_volume_is_not_subtracted():
    """REVERSED 2026-09-12 (WMR). A provider volume below the copied buy provably does not contain
    it, so the buy is not taken off it again. The floor still judges what the provider shows."""
    allow, _n, why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=1_500, m5_floor=0.10, m5_band_max_mcap=250_000, signal_usd=9_999)
    assert allow is False and why == "m5_turnover_under_floor", "1,500 is still under 2,000"
    allow, _n, _why = volume_alive_decision(
        50_000, 20_000, abs_floor=3000, turnover_floor=0.01,
        vol_m5=2_000, m5_floor=0.10, m5_band_max_mcap=250_000, signal_usd=9_999)
    assert allow is True, "before: 2,000 - 9,999 clamped to 0 and refused"


def test_abs_floor_on_unknown_mcap_judges_organic_h1():
    allow, _n, why = volume_alive_decision(
        3_500, None, abs_floor=3000, turnover_floor=0.0, signal_usd=1_000)
    assert allow is False and why == "under_abs_floor"
