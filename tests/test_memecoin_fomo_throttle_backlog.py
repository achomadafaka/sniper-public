"""The shared fomo throttle must not stall the heartbeat-owning thread.

Caught live 2026-08-23 15:33-15:36. The main thread was blocked in `_RateGuard.before_call`; the
heartbeat passed 120s (MEMECOIN_HEARTBEAT_MAX_AGE_S), the watchdog logged "runner unhealthy
(stale)" and killed the runner, and the death logger named the SIGTERM.

The gap is GLOBAL: the main poll loop and the armed follow-fast lane (one call per armed wallet)
share one guard, and each queued caller ratchets `_last_call` forward by another gap.
"""
import pytest

from sniper.feeds.fomo_feed import FomoBanGuardActive, FomoThrottleBacklog, _RateGuard


def _guard(gap_s, last_call, now):
    g = _RateGuard()
    g.gap_s = gap_s
    g._last_call = last_call
    return g


def test_a_backlog_beyond_the_cap_is_refused_not_slept_through(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    g = _RateGuard()
    g.gap_s = 30.0
    import time as _t
    g._last_call = _t.monotonic() + 300.0        # a dozen wallets queued ahead of us
    with pytest.raises(FomoThrottleBacklog):
        g.before_call(max_wait_s=45.0)
    assert slept == []                            # the caller is NOT parked for five minutes


def test_a_refused_call_does_not_ratchet_the_queue(monkeypatch):
    """A call we did not make must not push everyone behind us further out."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    import time as _t
    g = _RateGuard()
    g.gap_s = 30.0
    g._last_call = _t.monotonic() + 300.0
    before = g._last_call
    with pytest.raises(FomoThrottleBacklog):
        g.before_call(max_wait_s=45.0)
    assert g._last_call == before


def test_a_legitimate_single_gap_wait_still_sleeps(monkeypatch):
    """`gap_s` peaks at 30s. A normal wait under the cap must behave exactly as before."""
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    import time as _t
    g = _RateGuard()
    g.gap_s = 30.0
    g._last_call = _t.monotonic()
    g.before_call(max_wait_s=45.0)
    assert len(slept) == 1 and 25.0 < slept[0] <= 30.0


def test_no_gap_means_no_wait_and_no_raise(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    g = _RateGuard()                              # gap_s defaults to 0.0
    g.before_call(max_wait_s=45.0)
    assert slept == []


def test_zero_cap_restores_the_old_unbounded_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    import time as _t
    g = _RateGuard()
    g.gap_s = 30.0
    g._last_call = _t.monotonic() + 300.0
    g.before_call(max_wait_s=0.0)                 # cap disabled
    assert slept and slept[0] > 45.0


def test_the_ban_guard_still_wins_over_the_backlog_cap(monkeypatch):
    """A ban cooldown must keep raising its own type — hammering a ban is the worse failure."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    import time as _t
    g = _RateGuard()
    g.gap_s = 30.0
    g._last_call = _t.monotonic() + 300.0
    g.ban_until = _t.monotonic() + 60.0
    with pytest.raises(FomoBanGuardActive):
        g.before_call(max_wait_s=45.0)


def test_the_raise_is_catchable_by_the_loop_guard():
    """The main loop wraps the poll in `except Exception` and keeps looping, which is the
    documented contract: lanes treat a raised call as a failed poll."""
    assert issubclass(FomoThrottleBacklog, Exception)
