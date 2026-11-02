"""
Regression tests for Track B alerter.py refactoring (2026-03-01).

Covers:
1. _days_left_str  — shared days-remaining helper
2. _passes_qualifying_roi_gate — shared ROI gate helper
3. _fails_data_quality — wins > trades anomaly detection
4. wins <= trade_count SQL constraint alignment (unit-tested via _fails_data_quality)
5. TELEGRAM_BOT_TOKEN no longer hardcoded (loaded via _load_telegram_bot_token)
"""

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class TestDaysLeftStr(unittest.TestCase):
    """_days_left_str returns consistent human-readable strings."""

    def setUp(self):
        # Import after module load so env patches take effect
        from alerter import _days_left_str
        self._fn = _days_left_str

    def test_empty_string_returns_empty(self):
        self.assertEqual(self._fn(""), "")

    def test_none_like_value(self):
        self.assertEqual(self._fn(""), "")

    def test_future_date_shows_days_left(self):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        result = self._fn(future)
        self.assertIn("left", result)
        # timedelta.days truncates partial days, so +5 days may read as 4 or 5
        self.assertTrue(
            any(str(d) in result for d in range(4, 6)),
            f"Expected 4 or 5 days in result, got: {result!r}",
        )

    def test_past_date_shows_resolved(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        result = self._fn(past)
        self.assertIn("resolved", result)

    def test_today_shows_zero_days_left(self):
        # Just a few seconds in the future — still "0d left"
        near_future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        result = self._fn(near_future)
        self.assertIn("left", result)

    def test_z_suffix_parsed_correctly(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = self._fn(future)
        self.assertIn("left", result)

    def test_unparseable_returns_empty(self):
        result = self._fn("not-a-date")
        self.assertEqual(result, "")

    def test_separator_is_dot(self):
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        result = self._fn(future)
        self.assertTrue(result.startswith(" · "), f"Expected ' · ' prefix, got: {result!r}")

    def test_proven_positions_uses_pipe_separator(self):
        """check_for_proven_positions converts ' · ' -> ' | ' via replace."""
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        raw = self._fn(future)
        converted = raw.replace(" · ", " | ")
        self.assertIn(" | ", converted)
        self.assertNotIn(" · ", converted)


class TestPassesQualifyingRoiGate(unittest.TestCase):
    """_passes_qualifying_roi_gate correctly enforces the ROI gate."""

    def setUp(self):
        from alerter import _passes_qualifying_roi_gate
        self._fn = _passes_qualifying_roi_gate

    def test_null_roi_low_score_blocked(self):
        passes, reason = self._fn(qualifying_roi=None, score=85.0, min_qroi=10.0)
        self.assertFalse(passes)
        self.assertIn("NULL", reason)
        self.assertIn("85", reason)

    def test_null_roi_high_score_passes(self):
        passes, reason = self._fn(qualifying_roi=None, score=90.0, min_qroi=10.0)
        self.assertTrue(passes)
        self.assertEqual(reason, "")

    def test_null_roi_exactly_at_bypass_passes(self):
        passes, _ = self._fn(qualifying_roi=None, score=90.0, min_qroi=10.0,
                              score_bypass_threshold=90.0)
        self.assertTrue(passes)

    def test_null_roi_just_below_bypass_blocked(self):
        passes, _ = self._fn(qualifying_roi=None, score=89.9, min_qroi=10.0,
                              score_bypass_threshold=90.0)
        self.assertFalse(passes)

    def test_sufficient_roi_passes(self):
        passes, reason = self._fn(qualifying_roi=15.0, score=85.0, min_qroi=10.0)
        self.assertTrue(passes)
        self.assertEqual(reason, "")

    def test_roi_at_threshold_passes(self):
        passes, _ = self._fn(qualifying_roi=10.0, score=85.0, min_qroi=10.0)
        self.assertTrue(passes)

    def test_roi_below_threshold_blocked(self):
        passes, reason = self._fn(qualifying_roi=9.9, score=85.0, min_qroi=10.0)
        self.assertFalse(passes)
        self.assertIn("9.9", reason)
        self.assertIn("10", reason)

    def test_label_included_in_reason(self):
        passes, reason = self._fn(qualifying_roi=None, score=70.0, min_qroi=10.0,
                                   label="wallet_abc")
        self.assertFalse(passes)
        self.assertIn("wallet_abc", reason)

    def test_custom_bypass_threshold(self):
        # When bypass is 95, score=92 should be blocked
        passes, _ = self._fn(qualifying_roi=None, score=92.0, min_qroi=10.0,
                              score_bypass_threshold=95.0)
        self.assertFalse(passes)

    def test_consistency_with_old_insider_gate(self):
        """Gate must behave identically to the old two-if block for check_for_new_insiders."""
        cases = [
            # (qualifying_roi, insider_score, expected_passes)
            (None,   89.0, False),   # NULL roi, score < 90 → skip
            (None,   90.0, True),    # NULL roi, score >= 90 → allow
            (9.0,    95.0, False),   # roi < 10% → skip regardless of score
            (10.0,   85.0, True),    # roi == 10% → pass
            (50.0,   85.0, True),    # roi >> 10% → pass
        ]
        for qroi, score, expected in cases:
            with self.subTest(qroi=qroi, score=score):
                passes, _ = self._fn(qualifying_roi=qroi, score=score, min_qroi=10.0)
                self.assertEqual(passes, expected)


class TestFailsDataQuality(unittest.TestCase):
    """_fails_data_quality catches impossible win/trade combinations."""

    def setUp(self):
        from alerter import _fails_data_quality
        self._fn = _fails_data_quality

    def test_wins_equal_trades_ok(self):
        fails, _ = self._fn(trades=10, wins=10, win_rate=1.0, pnl=100.0)
        # win_rate=1.0 with positive pnl is fine
        self.assertFalse(fails)

    def test_wins_exceed_trades_anomaly(self):
        fails, reason = self._fn(trades=10, wins=11, win_rate=1.0, pnl=100.0)
        self.assertTrue(fails)
        self.assertIn("wins_to_trades_anomaly", reason)

    def test_perfect_winrate_negative_pnl_impossible(self):
        fails, reason = self._fn(trades=10, wins=10, win_rate=0.9999, pnl=-50.0)
        self.assertTrue(fails)
        self.assertIn("impossible_profile", reason)

    def test_zero_trades_does_not_trigger_wins_check(self):
        # wins > trades only fires when trades > 0
        fails, _ = self._fn(trades=0, wins=0, win_rate=0.0, pnl=0.0)
        self.assertFalse(fails)

    def test_old_loose_gate_would_pass_new_tight_gate_blocks(self):
        """The old SQL filter allowed wins <= trades * 1.5 (e.g. wins=14 for trades=10).
        The new filter allows only wins <= trades.  Verify _fails_data_quality blocks
        wins=11 for trades=10 (which the old SQL would have passed).
        """
        fails, reason = self._fn(trades=10, wins=11, win_rate=1.0, pnl=500.0)
        self.assertTrue(fails, "wins=11 for trades=10 must be flagged")
        self.assertIn("wins_to_trades_anomaly", reason)

    def test_borderline_normal_case(self):
        # 65% win rate, positive pnl — clean data
        fails, _ = self._fn(trades=20, wins=13, win_rate=0.65, pnl=1500.0)
        self.assertFalse(fails)


class TestTokenNotHardcoded(unittest.TestCase):
    """TELEGRAM_BOT_TOKEN must be loaded from env/config, not hardcoded."""

    def test_token_loaded_from_env(self):
        """When env var is set, the module should use that token."""
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test_token_xyz"}):
            # Re-import to pick up env change via the loader function
            import importlib
            import alerter
            token = alerter._load_telegram_bot_token()
            self.assertEqual(token, "test_token_xyz")

    def test_no_hardcoded_token_in_source(self):
        """The source file must not contain Telegram bot-token-shaped literals."""
        source = (Path(__file__).resolve().parents[1] / "alerter.py").read_text(encoding="utf-8")
        token_literal = re.search(r"""["']\d{6,}:[A-Za-z0-9_-]{20,}["']""", source)
        self.assertIsNone(token_literal, "Hardcoded Telegram token must not appear in alerter.py source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
