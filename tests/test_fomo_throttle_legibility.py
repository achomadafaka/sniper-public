"""The throttle must leave a trace, and it must behave exactly as it did before.

"Was the throttle ever active today, and how high did it go" had to be answered by grepping a log
and inferring, because one field meant both "a 429 raised the gap" and "a clean stretch lowered
it". Third defect of that shape in one day: the value existed and nothing kept it.

THE DECAY LADDER IS UNCHANGED. These tests pin the timing against the pre-split behaviour, because
a legibility fix that quietly retunes a live vendor-facing backoff is not a legibility fix.
"""
import pytest

from sniper.feeds.fomo_feed import _RateGuard


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def guard(monkeypatch):
    g = _RateGuard()
    clock = _Clock()
    import time
    monkeypatch.setattr(time, "monotonic", clock)
    g._clock = clock
    return g


def test_the_decay_ladder_timing_is_unchanged(guard):
    """From the 30s cap: six halvings, each needing 120 clean seconds. This is the pre-split
    behaviour and the split must reproduce it exactly."""
    for _ in range(6):
        guard.on_rate_limited()
    assert guard.gap_s == 30.0
    steps = []
    for _ in range(8):
        guard._clock.t += 121.0
        guard.on_success()
        steps.append(guard.gap_s)
    assert steps[:6] == [15.0, 7.5, 3.75, 1.875, 0.9375, 0.0]


def test_a_success_INSIDE_the_window_does_not_decay(guard):
    guard.on_rate_limited()
    guard._clock.t += 119.0
    guard.on_success()
    assert guard.gap_s == 1.0, "decayed early — the ladder was retuned"


def test_a_429_restarts_the_decay_clock_exactly_as_before(guard):
    """The shared field made a 429 delay the next decay step. `max()` of the two clocks must
    preserve that, or the split changed behaviour under pressure."""
    guard.on_rate_limited()
    guard.on_rate_limited()               # gap 2.0
    guard._clock.t += 100.0
    guard.on_rate_limited()               # gap 4.0, clock restarted
    guard._clock.t += 100.0               # 200s since the FIRST 429, only 100 since the last
    guard.on_success()
    assert guard.gap_s == 4.0, "decayed despite a 429 100s ago"
    guard._clock.t += 21.0
    guard.on_success()
    assert guard.gap_s == 2.0


def test_the_state_now_says_whether_the_throttle_was_ever_active(guard):
    """The whole point. Before this, nothing recorded that gap_s had ever been raised."""
    assert guard.snapshot() == {"gap_s": 0.0, "peak_gap_s": 0.0, "escalations": 0, "decays": 0,
                                "elevated_s": 0.0, "currently_elevated": False}
    for _ in range(3):
        guard.on_rate_limited()
    guard._clock.t += 50.0
    s = guard.snapshot()
    assert s["escalations"] == 3 and s["peak_gap_s"] == 4.0 and s["currently_elevated"] is True
    assert s["elevated_s"] == 50.0


def test_the_peak_survives_the_decay(guard):
    """A gap that returned to zero still has to say how high it went, or the record answers
    'was it active' with 'no' the moment it recovers."""
    for _ in range(6):
        guard.on_rate_limited()
    for _ in range(8):
        guard._clock.t += 121.0
        guard.on_success()
    s = guard.snapshot()
    assert s["gap_s"] == 0.0 and s["peak_gap_s"] == 30.0
    assert s["currently_elevated"] is False and s["elevated_s"] > 0


def test_escalation_and_decay_are_distinguishable_in_the_state(guard):
    """The defect itself: one field could not say which had happened."""
    guard.on_rate_limited()
    esc_at = guard._last_escalation
    guard._clock.t += 121.0
    guard.on_success()
    # VACUOUS FIRST DRAFT: this asserted only `_last_escalation != _last_decay`, which passes with
    # the split reverted because the unused field sits at 0.0. Caught by the mutation check.
    # The real property is that a DECAY advances the decay clock and LEAVES the escalation clock
    # alone — under the old shared field the decay overwrote it.
    assert guard._last_escalation == esc_at, "the decay moved the escalation clock: still shared"
    assert guard._last_decay > esc_at, "the decay did not record its own time"
    assert guard.escalations == 1 and guard.decays == 1
