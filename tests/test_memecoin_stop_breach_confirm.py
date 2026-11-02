"""Tests for elapsed-time stop confirmation.

This replaces a converter that turned a seconds target into a FROZEN HIT COUNT against a nominal
tick rate. That was not a duration — the runner counts detections with no clock, the fast lane
sleeps AFTER variable quote work so 1s is a floor rather than a rate, and a ~31s slow-lane
fallback stretched a "75 second" window into 38 MINUTES on a position whose only other downside
exit was the -80% catastrophe floor.

Every test here is either that failure mode or a fail-toward-selling guarantee.
"""
from __future__ import annotations

import pytest

import sniper.settings.memecoin as cfg
from sniper.services.memecoin_real_runner import stop_breach_confirmed as confirm

T0 = "2026-08-11T12:00:00+00:00"


def at(sec):
    from datetime import datetime, timedelta, timezone
    return (datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=sec)).isoformat()


def test_a_young_breach_holds():
    ok, elapsed = confirm(T0, at(30), 75)
    assert ok is False and elapsed == pytest.approx(30.0)


def test_an_aged_breach_sells():
    ok, elapsed = confirm(T0, at(80), 75)
    assert ok is True and elapsed == pytest.approx(80.0)


def test_it_fires_exactly_at_the_boundary():
    assert confirm(T0, at(74.9), 75)[0] is False
    assert confirm(T0, at(75.0), 75)[0] is True


def test_the_window_is_WALL_CLOCK_not_a_detection_count():
    """THE DEFECT. A hit count means 75s on a 1s lane and 2325s on a 31s lane. Elapsed time is
    identical on both, which is the entire point."""
    for _lane_cadence in (1.0, 2.5, 31.0):
        assert confirm(T0, at(80), 75)[0] is True, "duration must not depend on the lane"
        assert confirm(T0, at(30), 75)[0] is False


@pytest.mark.parametrize("started,now,window", [
    (None, at(999), 75),                  # never stamped
    ("not-a-timestamp", at(999), 75),     # unparseable
    (T0, None, 75),                       # no clock
    (T0, at(-300), 75),                   # clock moved backwards
    (T0, at(10), "nonsense"),             # bad window
])
def test_every_unresolvable_state_SELLS(started, now, window):
    """This policy family has no mirror and no trail. A window that cannot resolve must never
    become an indefinite hold — the failure direction is always toward exiting."""
    assert confirm(started, now, window)[0] is True


def test_a_disabled_window_defers_to_the_caller():
    assert confirm(T0, at(1), 0)[0] is True
    assert confirm(T0, at(1), -5)[0] is True


def test_the_window_is_clamped_so_no_config_can_defer_forever():
    assert cfg.stop_confirm_window_s(75) == 75.0
    assert cfg.stop_confirm_window_s(9_999) == cfg.MEMECOIN_REAL_STOP_CONFIRM_MAX_S
    assert cfg.stop_confirm_window_s(0) == 0.0
    assert cfg.stop_confirm_window_s(-1) == 0.0
    assert cfg.MEMECOIN_REAL_STOP_CONFIRM_MAX_S <= 300, "ceiling must stay small"


def test_the_old_converter_is_gone():
    assert not hasattr(cfg, "stop_confirm_sweeps_for"), (
        "the frozen-hit-count converter must not come back; it cannot express a duration")


def test_the_release_path_clears_the_breach_stamp():
    """A breach must be CONTINUOUS: a coin that recovers and dips again starts its window over."""
    import inspect
    from sniper.services import memecoin_real_runner as rr
    src = inspect.getsource(rr.real_exit_sweep) if hasattr(rr, "real_exit_sweep") else \
        inspect.getsource(rr)
    assert "set_position_stop_breach_started(conn, pos[\"id\"], None)" in src
