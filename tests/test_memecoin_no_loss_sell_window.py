"""No LOSS exit may fire in the first seconds after a fill; take-profit stays free.

Measured across the funded history, forcing every loss exit inside the window to wait:
  15s  n=5  +$23   |  20s  n=9  +$87  |  30s  n=12 +$88  |  45s n=19 +$109  |  60s n=25 +$124
Positive at every window; worst single case -$29 (DEBTCOIN) and it does not grow with the window.
Catastrophe is INCLUDED: loss exits inside 20s number 18 all-time with ZERO catastrophes, and
inside 60s, 66 with one. Take-profit is EXCLUDED: at a 60s window 80 of 121 affected exits are TPs,
worth about +$21 in aggregate against a worst case of -$1,706.
"""
from sniper.services.memecoin_real_runner import _LOSS_EXIT_REASONS, _fill_age_s


def test_every_losing_reason_is_covered():
    assert _LOSS_EXIT_REASONS == {"b_stop", "catastrophe", "vol_death"}


def test_catastrophe_is_inside_the_window_not_exempt():
    """The carve-out protected against something that has happened once in the whole history."""
    assert "catastrophe" in _LOSS_EXIT_REASONS


def test_profit_exits_are_never_blocked():
    for profit_reason in ("real_tp", "derisk", "real_trail", "manual"):
        assert profit_reason not in _LOSS_EXIT_REASONS


def test_age_is_measured_from_the_fill_not_the_decision():
    """`opened_at` is the decision start and runs a median 3.48s ahead of the recorded fill, so a
    window measured from it is short by that much before the coin is even held."""
    pos = {"opened_at": "2026-09-04T12:00:00+00:00",
           "opened_recorded_at": "2026-09-04T12:00:10+00:00"}
    assert _fill_age_s(pos, "2026-09-04T12:00:25+00:00") == 15.0


def test_it_falls_back_to_opened_at_when_the_fill_stamp_is_absent():
    pos = {"opened_at": "2026-09-04T12:00:00+00:00", "opened_recorded_at": None}
    assert _fill_age_s(pos, "2026-09-04T12:00:25+00:00") == 25.0
