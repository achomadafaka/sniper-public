"""Fomo self-defense (2026-08-17): 429 widens a shared inter-call gap, a 403 on a fresh token
(the 2026-08-03 app-ban signature) pauses every fomo call for the cooldown, and a clean stretch
decays the gap back to zero. One guard in fomo_request covers every lane."""
import time
import urllib.error

import pytest

from sniper.feeds import fomo_feed as ff


class _Auth:
    access = "tok"
    def refresh_token(self, http=None):
        self.access = "tok2"


def _err(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


@pytest.fixture(autouse=True)
def fresh_guard(monkeypatch):
    g = ff._RateGuard()
    monkeypatch.setattr(g, "_page", lambda *a, **k: None)
    monkeypatch.setattr(ff, "_rate_guard", g)
    return g


def test_429_escalates_the_gap(fresh_guard):
    def http(*a, **k):
        raise _err(429)
    for expect in (1.0, 2.0, 4.0):
        with pytest.raises(urllib.error.HTTPError):
            ff.fomo_request(_Auth(), "GET", "/feed", http=http)
        assert fresh_guard.gap_s == expect


def test_fresh_token_403_trips_the_ban_guard(fresh_guard):
    calls = []
    def http(*a, **k):
        calls.append(1)
        raise _err(403)
    with pytest.raises(urllib.error.HTTPError):
        ff.fomo_request(_Auth(), "GET", "/feed", http=http)
    assert len(calls) == 2                      # original + post-refresh retry
    assert fresh_guard.ban_until > time.monotonic()
    with pytest.raises(ff.FomoBanGuardActive):  # refused LOCALLY — fomo never touched
        ff.fomo_request(_Auth(), "GET", "/feed", http=http)
    assert len(calls) == 2


def test_success_decays_the_gap_after_a_clean_stretch(fresh_guard):
    # UPDATED 2026-08-29: the decay clock split off `_last_escalation`, so rewinding one field no
    # longer rewinds the ladder. Both are wound back here, which is what this test always meant.
    # Production behaviour is unchanged — `max(escalation, decay)` is exactly what the single
    # field held, the time of the most recent state change in either direction.
    def _rewind(g, seconds):
        g._last_escalation = time.monotonic() - seconds
        g._last_decay = time.monotonic() - seconds

    fresh_guard.gap_s = 4.0
    _rewind(fresh_guard, 121.0)
    ok = lambda *a, **k: (200, {}, None)
    ff.fomo_request(_Auth(), "GET", "/feed", http=ok)
    assert fresh_guard.gap_s == 2.0
    _rewind(fresh_guard, 121.0)
    fresh_guard.gap_s = 1.0
    ff.fomo_request(_Auth(), "GET", "/feed", http=ok)
    assert fresh_guard.gap_s == 0.0


def test_plain_success_stays_at_full_speed(fresh_guard):
    t0 = time.monotonic()
    for _ in range(3):
        ff.fomo_request(_Auth(), "GET", "/feed", http=lambda *a, **k: (200, {}, None))
    assert time.monotonic() - t0 < 0.5 and fresh_guard.gap_s == 0.0
