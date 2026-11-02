"""Regression tests for filters.days_to_resolution().

This function is a shared helper used by 3 strategy plugins (insider_copy_v1,
news_intel_v1, term_structure_v1) to gate entries and exits based on how many
calendar days remain before market resolution. A bug here means positions get
opened too late or held past resolution — both costly.

The function lives in filters.py (not strategy_runner) to avoid a circular
import: strategy_runner imports strategies.registry which imports the plugins.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sniper.filters import days_to_resolution


def _iso(dt: datetime) -> str:
    """Format a datetime as ISO-8601 string matching Polymarket's format."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestDaysToResolutionBasicCases(unittest.TestCase):

    def test_none_input_returns_none(self):
        self.assertIsNone(days_to_resolution(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(days_to_resolution(""))

    def test_whitespace_string_returns_none(self):
        self.assertIsNone(days_to_resolution("   "))

    def test_garbage_string_returns_none(self):
        """Unparseable input must never raise — returns None."""
        self.assertIsNone(days_to_resolution("not-a-date"))

    def test_partial_date_returns_none(self):
        self.assertIsNone(days_to_resolution("2026-13-99"))


class TestDaysToResolutionFutureMarkets(unittest.TestCase):

    def test_one_day_in_future(self):
        future = _utc_now() + timedelta(days=1)
        result = days_to_resolution(_iso(future))
        # Should be 1, but allow 0 due to sub-second boundary on slow CI
        self.assertIn(result, (0, 1))

    def test_thirty_days_in_future(self):
        future = _utc_now() + timedelta(days=30)
        result = days_to_resolution(_iso(future))
        self.assertIn(result, (29, 30))

    def test_ninety_days_in_future(self):
        future = _utc_now() + timedelta(days=90)
        result = days_to_resolution(_iso(future))
        self.assertIn(result, (89, 90))

    def test_future_market_returns_positive(self):
        future = _utc_now() + timedelta(days=14)
        result = days_to_resolution(_iso(future))
        self.assertGreater(result, 0)


class TestDaysToResolutionPastMarkets(unittest.TestCase):
    """Markets that resolved in the past should return negative days."""

    def test_one_day_in_past(self):
        past = _utc_now() - timedelta(days=1)
        result = days_to_resolution(_iso(past))
        # Should be -1, allow -1 or 0 for boundary
        self.assertIn(result, (-1, 0))

    def test_ten_days_in_past(self):
        past = _utc_now() - timedelta(days=10)
        result = days_to_resolution(_iso(past))
        self.assertIn(result, (-10, -9))

    def test_past_market_returns_non_positive(self):
        past = _utc_now() - timedelta(days=5)
        result = days_to_resolution(_iso(past))
        self.assertLessEqual(result, 0)


class TestDaysToResolutionTodayBoundary(unittest.TestCase):

    def test_today_returns_zero(self):
        """A market resolving today (same calendar date) should be 0."""
        today_midnight = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        result = days_to_resolution(_iso(today_midnight))
        self.assertEqual(result, 0)


class TestDaysToResolutionTimezoneHandling(unittest.TestCase):
    """Polymarket uses both naive UTC and Z-suffix timestamps."""

    def test_z_suffix_parsed_correctly(self):
        future = _utc_now() + timedelta(days=7)
        iso_z = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = days_to_resolution(iso_z)
        self.assertIn(result, (6, 7))

    def test_naive_iso_treated_as_utc(self):
        """Naive timestamps (no tz info) must be treated as UTC, not local time."""
        future = _utc_now() + timedelta(days=5)
        naive_iso = future.strftime("%Y-%m-%dT%H:%M:%S")  # no Z, no offset
        result = days_to_resolution(naive_iso)
        self.assertIn(result, (4, 5))

    def test_offset_aware_iso_string(self):
        """Explicit UTC offset should parse correctly."""
        future = _utc_now() + timedelta(days=3)
        offset_iso = future.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        result = days_to_resolution(offset_iso)
        self.assertIn(result, (2, 3))

    def test_returns_int_not_float(self):
        future = _utc_now() + timedelta(days=10)
        result = days_to_resolution(_iso(future))
        self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()
