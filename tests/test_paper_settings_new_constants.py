"""Tests for the new paper_settings constants added in the code review.

These constants are used across paper_trader.py, insider_copy_v1.py, and both
NO-value strategy plugins. If an env override breaks them (e.g., wrong type,
wrong default), entries silently pass or silently block without explanation.
Each test documents the *intent* of the constant so future changes require
conscious review rather than silent drift.
"""
from __future__ import annotations

import unittest

import sniper.settings.paper as paper_settings


class TestNewPaperSettingsConstants(unittest.TestCase):

    # ------------------------------------------------------------------ #
    # PAPER_BANKROLL_USDC                                                  #
    # ------------------------------------------------------------------ #

    def test_bankroll_is_float(self):
        self.assertIsInstance(paper_settings.PAPER_BANKROLL_USDC, float)

    def test_bankroll_default_is_500(self):
        """Default $500 bankroll matches the YatSen strategy baseline sizing."""
        self.assertEqual(paper_settings.PAPER_BANKROLL_USDC, 500.0)

    def test_bankroll_is_positive(self):
        self.assertGreater(paper_settings.PAPER_BANKROLL_USDC, 0)

    # ------------------------------------------------------------------ #
    # PAPER_MIN_CONFIDENCE_SCORE                                           #
    # ------------------------------------------------------------------ #

    def test_min_confidence_is_float(self):
        self.assertIsInstance(paper_settings.PAPER_MIN_CONFIDENCE_SCORE, float)

    def test_min_confidence_default_is_35(self):
        """35 is the empirically-derived minimum — below this, classification
        is unreliable regardless of wallet_class tier label."""
        self.assertEqual(paper_settings.PAPER_MIN_CONFIDENCE_SCORE, 35.0)

    def test_min_confidence_in_valid_range(self):
        self.assertGreaterEqual(paper_settings.PAPER_MIN_CONFIDENCE_SCORE, 0)
        self.assertLessEqual(paper_settings.PAPER_MIN_CONFIDENCE_SCORE, 100)

    # ------------------------------------------------------------------ #
    # PAPER_LOW_SIGNAL_MIN_SAMPLE                                          #
    # ------------------------------------------------------------------ #

    def test_low_signal_min_sample_is_int(self):
        self.assertIsInstance(paper_settings.PAPER_LOW_SIGNAL_MIN_SAMPLE, int)

    def test_low_signal_min_sample_default_is_10(self):
        self.assertEqual(paper_settings.PAPER_LOW_SIGNAL_MIN_SAMPLE, 10)

    def test_low_signal_min_sample_is_positive(self):
        self.assertGreater(paper_settings.PAPER_LOW_SIGNAL_MIN_SAMPLE, 0)

    # ------------------------------------------------------------------ #
    # PAPER_LOW_SIGNAL_MIN_WIN_RATE                                        #
    # ------------------------------------------------------------------ #

    def test_low_signal_min_win_rate_is_float(self):
        self.assertIsInstance(paper_settings.PAPER_LOW_SIGNAL_MIN_WIN_RATE, float)

    def test_low_signal_min_win_rate_default_is_65_percent(self):
        """65% matches the high_winrate_qualified threshold in analyze_users.py.
        Below this, the wallet shows no reliable edge."""
        self.assertAlmostEqual(paper_settings.PAPER_LOW_SIGNAL_MIN_WIN_RATE, 0.65)

    def test_low_signal_min_win_rate_is_probability(self):
        """Must be in [0, 1] — not a percentage like 65."""
        self.assertGreaterEqual(paper_settings.PAPER_LOW_SIGNAL_MIN_WIN_RATE, 0.0)
        self.assertLessEqual(paper_settings.PAPER_LOW_SIGNAL_MIN_WIN_RATE, 1.0)

    # ------------------------------------------------------------------ #
    # PAPER_MAX_SPECULATOR_SCORE                                           #
    # ------------------------------------------------------------------ #

    def test_max_speculator_score_is_float(self):
        self.assertIsInstance(paper_settings.PAPER_MAX_SPECULATOR_SCORE, float)

    def test_max_speculator_score_default_is_70(self):
        """Wallets at or above 70 speculator score are execution bots / noise
        traders. All paper trade losses at ins>=80 came from this cohort."""
        self.assertEqual(paper_settings.PAPER_MAX_SPECULATOR_SCORE, 70.0)

    def test_max_speculator_score_in_valid_range(self):
        self.assertGreater(paper_settings.PAPER_MAX_SPECULATOR_SCORE, 0)
        self.assertLessEqual(paper_settings.PAPER_MAX_SPECULATOR_SCORE, 100)

    # ------------------------------------------------------------------ #
    # NEAR_ZERO_PRICE_THRESHOLD                                            #
    # ------------------------------------------------------------------ #

    def test_near_zero_threshold_is_float(self):
        self.assertIsInstance(paper_settings.NEAR_ZERO_PRICE_THRESHOLD, float)

    def test_near_zero_threshold_default_is_0_10(self):
        """Below 10¢, source-exit suppression activates — the position is
        expected to resolve near zero and following source exit locks in the loss."""
        self.assertAlmostEqual(paper_settings.NEAR_ZERO_PRICE_THRESHOLD, 0.10)

    def test_near_zero_threshold_is_valid_price(self):
        self.assertGreater(paper_settings.NEAR_ZERO_PRICE_THRESHOLD, 0)
        self.assertLess(paper_settings.NEAR_ZERO_PRICE_THRESHOLD, 1.0)

    # ------------------------------------------------------------------ #
    # PAPER_CATEGORY_MAX_ENTRY                                             #
    # ------------------------------------------------------------------ #

    def test_category_max_entry_is_dict(self):
        self.assertIsInstance(paper_settings.PAPER_CATEGORY_MAX_ENTRY, dict)

    def test_category_max_entry_contains_geopolitics(self):
        self.assertIn("geopolitics", paper_settings.PAPER_CATEGORY_MAX_ENTRY)

    def test_category_max_entry_contains_crypto_commodity(self):
        self.assertIn("crypto/commodity", paper_settings.PAPER_CATEGORY_MAX_ENTRY)

    def test_category_max_entry_contains_tech_ai_launch(self):
        self.assertIn("tech/ai_launch", paper_settings.PAPER_CATEGORY_MAX_ENTRY)

    def test_category_max_entry_geopolitics_default(self):
        """45¢ cap on geopolitics — empirically negative edge above this price."""
        self.assertAlmostEqual(paper_settings.PAPER_CATEGORY_MAX_ENTRY["geopolitics"], 0.45)

    def test_category_max_entry_crypto_default(self):
        """40¢ cap on crypto/commodity — binary price-target markets showed
        -43.5% avg ROI, 40% WR across 5 closed trades."""
        self.assertAlmostEqual(paper_settings.PAPER_CATEGORY_MAX_ENTRY["crypto/commodity"], 0.40)

    def test_category_max_entry_tech_default(self):
        self.assertAlmostEqual(paper_settings.PAPER_CATEGORY_MAX_ENTRY["tech/ai_launch"], 0.50)

    def test_all_category_caps_are_valid_prices(self):
        for cat, cap in paper_settings.PAPER_CATEGORY_MAX_ENTRY.items():
            self.assertGreater(cap, 0, f"{cat} cap must be > 0")
            self.assertLess(cap, 1.0, f"{cat} cap must be < 1.0")
            self.assertIsInstance(cap, float, f"{cat} cap must be float")


class TestNewConstantsUsedInPaperTrader(unittest.TestCase):
    """Verify paper_trader imports all new constants (import smoke test).

    If paper_trader fails to import them, this module-level import will raise
    ImportError and the entire test class will error out with a clear message.
    """

    def test_paper_trader_imports_new_constants(self):
        import paper_trader
        # Verify the names are actually accessible at module scope
        self.assertTrue(hasattr(paper_trader, "PAPER_MIN_CONFIDENCE_SCORE"))
        self.assertTrue(hasattr(paper_trader, "PAPER_LOW_SIGNAL_MIN_SAMPLE"))
        self.assertTrue(hasattr(paper_trader, "PAPER_LOW_SIGNAL_MIN_WIN_RATE"))
        self.assertTrue(hasattr(paper_trader, "PAPER_MAX_SPECULATOR_SCORE"))
        self.assertTrue(hasattr(paper_trader, "NEAR_ZERO_PRICE_THRESHOLD"))
        self.assertTrue(hasattr(paper_trader, "PAPER_CATEGORY_MAX_ENTRY"))
        self.assertTrue(hasattr(paper_trader, "PAPER_BANKROLL_USDC"))

    def test_paper_trader_values_match_paper_settings(self):
        """paper_trader and paper_settings must agree on every new constant."""
        import paper_trader
        import sniper.settings.paper as paper_settings
        self.assertEqual(paper_trader.PAPER_MIN_CONFIDENCE_SCORE, paper_settings.PAPER_MIN_CONFIDENCE_SCORE)
        self.assertEqual(paper_trader.PAPER_LOW_SIGNAL_MIN_SAMPLE, paper_settings.PAPER_LOW_SIGNAL_MIN_SAMPLE)
        self.assertEqual(paper_trader.PAPER_LOW_SIGNAL_MIN_WIN_RATE, paper_settings.PAPER_LOW_SIGNAL_MIN_WIN_RATE)
        self.assertEqual(paper_trader.PAPER_MAX_SPECULATOR_SCORE, paper_settings.PAPER_MAX_SPECULATOR_SCORE)
        self.assertEqual(paper_trader.NEAR_ZERO_PRICE_THRESHOLD, paper_settings.NEAR_ZERO_PRICE_THRESHOLD)
        self.assertEqual(paper_trader.PAPER_BANKROLL_USDC, paper_settings.PAPER_BANKROLL_USDC)
        self.assertEqual(paper_trader.PAPER_CATEGORY_MAX_ENTRY, paper_settings.PAPER_CATEGORY_MAX_ENTRY)


if __name__ == "__main__":
    unittest.main()
