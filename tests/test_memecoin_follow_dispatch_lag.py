"""The follow lane must name a slow dispatch instead of hiding it.

Everything between the wallet response and the funded hook is DB-only by design, so a large gap
means the database blocked. It is invisible otherwise: `observed_at` on the attempt row is the
response receipt and `decision_started_at` is the hook entry, so the interval reads as a mystery.

Live 2026-08-23: position 44596 (KIRK) sat 11.22s there — 1 of 494 attempts that day (p50 0.01s,
p90 0.64s) with nothing logged during it.
"""
import logging

from sniper.services import memecoin_follow_activity as F


def test_the_threshold_is_well_clear_of_normal_jitter():
    """p90 was 0.64s; the warn line must not fire on ordinary dispatches."""
    assert F._DISPATCH_LAG_WARN_S >= 1.0
    assert F._DISPATCH_LAG_WARN_S <= 5.0


def _lag_between(observed_iso, now_iso):
    """Mirror of the production computation, so the arithmetic itself is covered."""
    obs = F._parse(observed_iso)
    return (F._parse(now_iso) - obs).total_seconds()


def test_the_live_kirk_gap_would_have_been_logged():
    lag = _lag_between("2026-08-23T15:44:29.224351+00:00", "2026-08-23T15:44:40.442911+00:00")
    assert round(lag, 2) == 11.22
    assert lag >= F._DISPATCH_LAG_WARN_S


def test_a_normal_dispatch_stays_quiet():
    lag = _lag_between("2026-08-23T15:44:29.224351+00:00", "2026-08-23T15:44:29.864351+00:00")
    assert round(lag, 2) == 0.64                      # the day's p90
    assert lag < F._DISPATCH_LAG_WARN_S


def test_an_unparseable_receipt_never_raises_on_the_money_path():
    """The lag line is diagnostic. It must not be able to break a funded dispatch."""
    assert F._parse("not-a-timestamp") is None
    assert F._parse(None) is None
