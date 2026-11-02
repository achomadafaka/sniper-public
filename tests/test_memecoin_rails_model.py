"""The rails model is a shared contract, so its properties are the tests.

Two callers depend on it meaning the same thing: the exit-rail replay (coins we held) and the
refused-mint capture (coins we never entered). A counterfactual that means different things in the
two places is worse than no counterfactual.
"""
from __future__ import annotations

import pytest

from sniper.analysis import memecoin_rails_model as rm

ENTRY = 1.0


def _flat(n=200, price=1.0, step=5.0):
    return [(i * step, price) for i in range(n)]


def _pol(**kw):
    return rm.RailsPolicy(**kw)


# ------------------------------------------------------------------ property 1: pure

def test_the_same_inputs_always_give_the_same_result():
    series = [(0, 1.0), (5, 1.1), (10, 1.3), (15, 0.9)]
    a = rm.rails_outcome(series, _pol(), price_space=rm.SPACE_CHART, entry_price=ENTRY)
    b = rm.rails_outcome(series, _pol(), price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert a == b


# ------------------------------------------------------------------ property 2: price-space honest

def test_the_price_space_must_be_declared_and_is_carried_on_the_result():
    """Chart and route differ by −33% to +76% on this book. A silent mix is a confident wrong
    answer."""
    got = rm.rails_outcome([(0, 1.0)], _pol(), price_space="guess", entry_price=ENTRY)
    assert got.reason == rm.UNDETERMINED and "declared" in got.detail

    for space in (rm.SPACE_CHART, rm.SPACE_ROUTE):
        out = rm.rails_outcome(_flat(), _pol(), price_space=space, entry_price=ENTRY)
        assert out.price_space == space


# ------------------------------------------------------------------ property 3: censoring

def test_a_gap_wider_than_the_reaction_window_is_UNDETERMINED_not_no_trigger():
    """THE DISTINCTION THIS CODEBASE HAS PAID FOR THREE TIMES. `undetermined` = we cannot tell.
    `no_trigger` = we watched and nothing happened. Pooling them reports a policy inactive when it
    was merely unobservable."""
    series = [(0, 1.0), (5, 1.02), (600, 1.03), (605, 1.04)]
    got = rm.rails_outcome(series, _pol(), price_space=rm.SPACE_CHART, entry_price=ENTRY,
                           cadence_hint_s=5.0)
    assert got.reason == rm.UNDETERMINED
    assert got.determined is False
    assert "595" in got.detail or "gap" in got.detail


def test_a_series_that_ends_before_the_horizon_is_undetermined():
    """We stopped watching; that is not evidence that nothing fired."""
    got = rm.rails_outcome(_flat(n=10), _pol(max_hold_s=780.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY)
    assert got.reason == rm.UNDETERMINED and "inside the" in got.detail


def test_a_complete_quiet_series_is_NO_TRIGGER_not_undetermined():
    got = rm.rails_outcome(_flat(n=200), _pol(max_hold_s=780.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY)
    assert got.reason in (rm.NO_TRIGGER, rm.MAX_HOLD)
    assert got.determined is True


@pytest.mark.parametrize("series,why", [
    ([], "empty series"),
    (None, "empty series"),
    ([(0, 0)], "empty series"),
    ([(0, None)], "empty series"),
])
def test_an_unusable_series_is_undetermined(series, why):
    got = rm.rails_outcome(series, _pol(), price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.UNDETERMINED and why in got.detail


def test_a_missing_entry_price_is_undetermined():
    got = rm.rails_outcome(_flat(), _pol(), price_space=rm.SPACE_CHART, entry_price=0)
    assert got.reason == rm.UNDETERMINED


# ------------------------------------------------------------------ property 4: no forward leakage

def test_a_later_peak_cannot_arm_an_earlier_trail():
    """The peak is accumulated forward. If it were computed over the whole series, a spike AFTER
    the exit would arm a trail before it."""
    # dips to the stop at t=10; a huge spike arrives later and must not matter
    series = [(0, 1.0), (5, 1.02), (10, 0.6), (15, 5.0)]
    got = rm.rails_outcome(series, _pol(stop_pct=35.0, trail_arm_pct=15.0, trail_pct=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.STOP and got.exit_t == 10


# ------------------------------------------------------------------ the rails themselves

def test_the_take_profit_fires_and_reports_its_trigger():
    series = [(0, 1.0), (5, 1.1), (10, 1.30)]
    got = rm.rails_outcome(series, _pol(tp_pct=25.0), price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.TP and got.exit_t == 10
    assert got.return_pct == pytest.approx(30.0)
    assert got.trigger_index == 2


def test_the_stop_fires_on_the_first_observation_that_breaches_it():
    """0.70 is ABOVE a 35% stop (0.65) and must not fire; 0.60 is below it and must."""
    series = [(0, 1.0), (5, 0.70), (10, 0.60)]
    got = rm.rails_outcome(series, _pol(stop_pct=35.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY)
    assert got.reason == rm.STOP and got.exit_t == 10
    assert got.return_pct == pytest.approx(-40.0), "realises AT the observed price, not at nominal"


def test_the_stop_is_checked_before_the_take_profit():
    """Both levels crossed in one observation is a wick. Taking the loss is the conservative read
    and it must be deterministic, not incidental to statement order."""
    got = rm.rails_outcome([(0, 1.0), (5, 0.5)], _pol(tp_pct=25.0, stop_pct=35.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.STOP


def test_max_hold_closes_a_position_that_never_triggers():
    got = rm.rails_outcome([(0, 1.0), (5, 1.05), (780, 1.05)], _pol(max_hold_s=780.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY, cadence_hint_s=1000)
    assert got.reason == rm.MAX_HOLD and got.return_pct == pytest.approx(5.0)


# ------------------------------------------------------------------ the ladder

def test_a_ladder_sells_a_fraction_and_rides_the_rest_to_the_stop():
    series = [(0, 1.0), (5, 1.30), (10, 0.60)]
    got = rm.rails_outcome(series, _pol(tp_pct=25.0, tp_fraction=0.5, stop_pct=35.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.STOP
    assert [leg[2] for leg in got.legs] == [rm.TP, rm.STOP]
    # 0.5 * +30% + 0.5 * −40%
    assert got.return_pct == pytest.approx(0.5 * 30.0 + 0.5 * -40.0)


def test_a_full_take_profit_leaves_no_remainder():
    got = rm.rails_outcome([(0, 1.0), (5, 1.30)], _pol(tp_pct=25.0, tp_fraction=1.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.TP and len(got.legs) == 1


def test_the_ladder_remainder_can_trail_out_above_the_stop():
    series = [(0, 1.0), (5, 1.30), (10, 1.60), (15, 1.40)]
    got = rm.rails_outcome(series, _pol(tp_pct=25.0, tp_fraction=0.5, trail_arm_pct=15.0,
                                        trail_pct=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.TRAIL
    assert [leg[2] for leg in got.legs] == [rm.TP, rm.TRAIL]
    assert got.return_pct == pytest.approx(0.5 * 30.0 + 0.5 * 40.0)


def test_the_trail_does_not_apply_before_the_ladder_fires():
    """A flat policy has no remainder, so a trail must not close a full position early."""
    series = [(0, 1.0), (5, 1.20), (10, 1.00)]
    got = rm.rails_outcome(series, _pol(tp_pct=25.0, tp_fraction=1.0, trail_arm_pct=15.0,
                                        trail_pct=10.0, max_hold_s=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.MAX_HOLD, "no ladder remainder means no trail leg"


# ------------------------------------------------------------------ the censor threshold itself

def _jittered(seed, *, jitter, drops=0, cadence=5.0, n=162):
    """A realistic observation series: wall-clock stamps, not a perfect grid."""
    import random
    rnd = random.Random(seed)
    t, pts = 0.0, []
    for i in range(n):
        t += cadence + rnd.uniform(-jitter, jitter)
        if drops and i in range(20, 20 + drops):
            continue
        pts.append((t, 1.0))
    return pts


def _censored(jitter, drops, seeds=60, cadence=5.0):
    n = 0
    for s in range(seeds):
        pts = _jittered(s, jitter=jitter, drops=drops, cadence=cadence)
        got = rm.rails_outcome(pts, _pol(max_hold_s=pts[-1][0] + 1),
                               price_space=rm.SPACE_CHART, entry_price=ENTRY,
                               cadence_hint_s=cadence)
        n += (got.reason == rm.UNDETERMINED)
    return n


def test_ordinary_jitter_does_not_censor():
    """REGRESSION. The threshold was one nominal interval, and wall-clock stamps land at
    cadence +/- jitter, so about half of all gaps exceeded it and 50ms of jitter censored 100% of
    series. `undetermined` was the DEFAULT, and the model could not distinguish the case it exists
    to distinguish."""
    assert _censored(0.05, 0) == 0, "50ms of jitter must not censor"
    assert _censored(0.25, 0) == 0, "250ms of jitter must not censor"


def test_a_genuinely_dropped_poll_still_censors():
    """The other edge: a tolerance loose enough to survive jitter must still catch a real hole,
    or a rail that could have fired inside it is silently resolved past it."""
    assert _censored(0.25, 1) == 60, "one dropped poll must censor every time"
    assert _censored(0.25, 3) == 60


def test_the_tolerance_sits_inside_the_measured_safe_window():
    assert 1.2 <= rm.RailsPolicy.GAP_TOLERANCE_MULT <= 1.9


# ------------------------------------------------- the per-observation cadence (Gamma, 2026-08-26)

def _two_tier(fast=5.0, slow=15.0, n_fast=6, n_slow=6):
    """A series whose cadence changes ON PURPOSE partway through, as a throttled collector produces:
    full rate while the funded book is empty, a slower tier while a position is open."""
    t, pts, hints = 0.0, [], []
    for _ in range(n_fast):
        pts.append((t, 1.0)); hints.append(fast); t += fast
    for _ in range(n_slow):
        pts.append((t, 1.0)); hints.append(slow); t += slow
    return pts, hints


def test_a_deliberate_cadence_change_does_not_read_as_an_outage():
    """One hint cannot describe two cadences. At the fast hint the whole slow segment censors; the
    series is healthy and the model must say so."""
    pts, hints = _two_tier()
    pol = _pol(max_hold_s=pts[-1][0] + 1)
    assert rm.rails_outcome(pts, pol, price_space=rm.SPACE_CHART, entry_price=ENTRY,
                            cadence_hint_s=5.0).reason == rm.UNDETERMINED
    got = rm.rails_outcome(pts, pol, price_space=rm.SPACE_CHART, entry_price=ENTRY,
                           cadence_hint_s=hints)
    assert got.reason != rm.UNDETERMINED


def test_a_real_dropout_inside_the_fast_segment_still_censors():
    """The other edge: the slow tier's tolerance must not be lent to the fast one."""
    pts, hints = _two_tier()
    pts = pts[:3] + [(pts[3][0] + 20.0, 1.0)] + [(t + 20.0, p) for t, p in pts[4:]]
    got = rm.rails_outcome(pts, _pol(max_hold_s=pts[-1][0] + 1), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY, cadence_hint_s=hints)
    assert got.reason == rm.UNDETERMINED


def test_the_boundary_gap_is_judged_by_the_wider_of_the_two_cadences():
    """A slow -> fast transition arrives at an observation whose own hint is FAST, but the gap that
    reached it was legitimate under the SLOW tier. Judging it by the arriving hint alone censors
    every such boundary — the mistake a caller-side splitter makes, which is why the model owns it."""
    pts = [(0.0, 1.0), (15.0, 1.0), (30.0, 1.0), (35.0, 1.0), (40.0, 1.0)]
    hints = [15.0, 15.0, 5.0, 5.0, 5.0]
    got = rm.rails_outcome(pts, _pol(max_hold_s=41.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY, cadence_hint_s=hints)
    assert got.reason != rm.UNDETERMINED, "the 15s gap into the fast tier is legitimate"


def test_a_misaligned_cadence_sequence_is_refused_not_padded():
    """A silently misaligned hint censors the wrong observations and reads as a data property."""
    pts, hints = _two_tier()
    got = rm.rails_outcome(pts, _pol(), price_space=rm.SPACE_CHART, entry_price=ENTRY,
                           cadence_hint_s=hints[:-2])
    assert got.reason == rm.UNDETERMINED and "cadence hint" in got.detail


def test_the_hint_stays_aligned_when_unusable_prices_are_dropped():
    """The filter that removes null/zero prices must carry the hints with it. Here the dropped
    observation sits in the FAST segment, so a naive `hints[filtered_index]` would slide the slow
    tier's 15s hint one place early and censor the legitimate 15s gap at t=25."""
    pts = [(0.0, None), (5.0, 1.0), (10.0, 1.0), (25.0, 1.0), (40.0, 1.0)]
    hints = [5.0, 5.0, 5.0, 15.0, 15.0]
    got = rm.rails_outcome(pts, _pol(max_hold_s=41.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY, cadence_hint_s=hints)
    assert got.reason != rm.UNDETERMINED

    misaligned = [h for h, (_t, p) in zip(hints, pts) if p][:-1] + [15.0]
    shifted = rm.rails_outcome(pts[1:], _pol(max_hold_s=41.0), price_space=rm.SPACE_CHART,
                               entry_price=ENTRY, cadence_hint_s=[5.0, 5.0, 5.0, 15.0])
    assert shifted.reason == rm.UNDETERMINED, "the misalignment this guards against is detectable"
    assert misaligned  # the shifted list is what a naive filter would produce


def test_a_dropped_poll_is_still_censored_when_the_cadence_is_recorded():
    """Recording the cadence must not become a way to excuse missing data: a hole inside a segment
    is still a hole, whatever tier it is in."""
    pts = [(0.0, 1.0), (5.0, 1.0), (10.0, 1.0), (35.0, 1.0), (50.0, 1.0)]
    hints = [5.0, 5.0, 5.0, 15.0, 15.0]
    got = rm.rails_outcome(pts, _pol(max_hold_s=51.0), price_space=rm.SPACE_CHART,
                           entry_price=ENTRY, cadence_hint_s=hints)
    assert got.reason == rm.UNDETERMINED, "25s into a 15s tier is a dropped poll, not a cadence"


# ------------------------------------------------------- zero disables, and a ladder respects arm

def test_zero_tp_pct_disables_the_take_profit():
    """0 means OFF, as it does in the funded contract. It must not resolve at the entry price.

    Regression: `tp_at = entry * (1 + 0/100)` equals `entry`, so the first observation at or above
    entry booked a TP for +0.0%. `memecoin_exit_rail_replay.baseline_policy` feeds the position's
    live funded policy straight in, and the band policies all carry tp_pct=0.0.
    """
    series = [(0, 1.0), (5, 1.02), (10, 1.40)]
    got = rm.rails_outcome(series, _pol(tp_pct=0.0, stop_pct=35.0, max_hold_s=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason != rm.TP
    assert all(leg[2] != rm.TP for leg in got.legs)


def test_zero_stop_pct_disables_the_stop():
    series = [(0, 1.0), (5, 0.98), (10, 1.05)]
    got = rm.rails_outcome(series, _pol(tp_pct=200.0, stop_pct=0.0, max_hold_s=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason != rm.STOP
    assert all(leg[2] != rm.STOP for leg in got.legs)


def test_a_partial_tp_does_not_arm_a_trail_below_its_arm_threshold():
    """The ladder makes the trail ELIGIBLE; trail_arm_pct still decides when it ARMS.

    Regression: the TP leg set armed=True unconditionally, so a +10% take-profit under a +50% arm
    threshold trailed out at +5% on a peak that never passed +10%.
    """
    series = [(0, 1.0), (5, 1.10), (10, 1.05)]
    got = rm.rails_outcome(series, _pol(tp_pct=10.0, stop_pct=90.0, tp_fraction=0.5,
                                        trail_pct=4.0, trail_arm_pct=50.0, max_hold_s=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason != rm.TRAIL, "the peak never passed the +50% arm threshold"
    assert rm.TRAIL not in [leg[2] for leg in got.legs]
    # the remainder closes on the horizon instead, which is the honest outcome here
    assert [leg[2] for leg in got.legs] == [rm.TP, rm.MAX_HOLD]


def test_a_partial_tp_arms_the_trail_when_no_arm_threshold_is_set():
    """trail_arm_pct <= 0 means armed as soon as a remainder exists — the funded band convention."""
    series = [(0, 1.0), (5, 1.10), (10, 1.05)]
    got = rm.rails_outcome(series, _pol(tp_pct=10.0, stop_pct=90.0, tp_fraction=0.5,
                                        trail_pct=4.0, trail_arm_pct=0.0, max_hold_s=10.0),
                           price_space=rm.SPACE_CHART, entry_price=ENTRY)
    assert got.reason == rm.TRAIL
    assert [leg[2] for leg in got.legs] == [rm.TP, rm.TRAIL]
