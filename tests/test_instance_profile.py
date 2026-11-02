"""Tests for instance_profile.py"""

import unittest

from sniper.settings.instance_profile import load_instance_profile


class TestInstanceProfileDefaults(unittest.TestCase):
    """Default profile loads correctly with minimal env."""

    def test_default_profile_minimal_env(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": "test-node"})
        self.assertEqual(profile.instance_tag, "test-node")
        self.assertEqual(profile.display_name, "test-node")  # falls back to instance_tag
        self.assertEqual(profile.role, "paper")
        self.assertTrue(profile.enabled)
        self.assertEqual(profile.enabled_strategies, ())
        self.assertEqual(profile.enabled_feeds, ())
        self.assertTrue(profile.paper_only)
        self.assertFalse(profile.real_trading_enabled)
        self.assertAlmostEqual(profile.bankroll_usdc, 1000.0)
        self.assertAlmostEqual(profile.max_total_exposure_usdc, 1000.0)
        self.assertAlmostEqual(profile.max_market_exposure_usdc, 200.0)
        self.assertEqual(profile.max_concurrent_positions, 20)
        self.assertAlmostEqual(profile.daily_loss_limit_usdc, 50.0)
        self.assertEqual(profile.default_loop_seconds, 60)

    def test_instance_tag_defaults_to_default_local_when_missing(self):
        profile = load_instance_profile(env={})
        self.assertEqual(profile.instance_tag, "default-local")

    def test_instance_tag_defaults_to_default_local_when_empty_string(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": ""})
        self.assertEqual(profile.instance_tag, "default-local")

    def test_instance_tag_defaults_to_default_local_when_whitespace_only(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": "   "})
        self.assertEqual(profile.instance_tag, "default-local")


class TestInstanceProfileDisplayName(unittest.TestCase):
    """display_name falls back to instance_tag when not set."""

    def test_display_name_falls_back_to_instance_tag(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": "my-vps"})
        self.assertEqual(profile.display_name, "my-vps")

    def test_display_name_explicit(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "my-vps", "INSTANCE_DISPLAY_NAME": "My VPS Node"}
        )
        self.assertEqual(profile.display_name, "My VPS Node")

    def test_display_name_empty_falls_back_to_instance_tag(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "my-vps", "INSTANCE_DISPLAY_NAME": ""}
        )
        self.assertEqual(profile.display_name, "my-vps")


class TestCSVParsing(unittest.TestCase):
    """CSV parsing normalises case and whitespace."""

    def test_csv_normalises_case_and_spacing(self):
        profile = load_instance_profile(
            env={
                "INSTANCE_TAG": "t",
                "ENABLED_STRATEGIES": " Insider_Copy_V1 , fade_v1 ",
            }
        )
        self.assertEqual(profile.enabled_strategies, ("insider_copy_v1", "fade_v1"))

    def test_csv_empty_string_produces_empty_tuple(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "ENABLED_STRATEGIES": ""}
        )
        self.assertEqual(profile.enabled_strategies, ())

    def test_csv_missing_key_produces_empty_tuple(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": "t"})
        self.assertEqual(profile.enabled_strategies, ())
        self.assertEqual(profile.enabled_feeds, ())

    def test_csv_single_value(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "ENABLED_FEEDS": "polymarket_clob"}
        )
        self.assertEqual(profile.enabled_feeds, ("polymarket_clob",))

    def test_csv_filters_empty_segments(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "ENABLED_STRATEGIES": "alpha,,beta,"}
        )
        self.assertEqual(profile.enabled_strategies, ("alpha", "beta"))

    def test_csv_feeds_normalises_case(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "ENABLED_FEEDS": "  CLOB_Feed , Gamma_Feed  "}
        )
        self.assertEqual(profile.enabled_feeds, ("clob_feed", "gamma_feed"))


class TestPaperOnlyOverride(unittest.TestCase):
    """paper_only=True forces real_trading_enabled=False."""

    def test_paper_only_forces_real_trading_off(self):
        profile = load_instance_profile(
            env={
                "INSTANCE_TAG": "t",
                "PAPER_ONLY": "1",
                "REAL_TRADING_ENABLED": "1",
            }
        )
        self.assertTrue(profile.paper_only)
        self.assertFalse(profile.real_trading_enabled)

    def test_paper_only_true_string_forces_real_trading_off(self):
        profile = load_instance_profile(
            env={
                "INSTANCE_TAG": "t",
                "PAPER_ONLY": "true",
                "REAL_TRADING_ENABLED": "true",
            }
        )
        self.assertFalse(profile.real_trading_enabled)

    def test_paper_only_false_allows_real_trading(self):
        profile = load_instance_profile(
            env={
                "INSTANCE_TAG": "t",
                "PAPER_ONLY": "0",
                "REAL_TRADING_ENABLED": "1",
            }
        )
        self.assertFalse(profile.paper_only)
        self.assertTrue(profile.real_trading_enabled)

    def test_paper_only_false_real_trading_off_by_default(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "PAPER_ONLY": "0"}
        )
        self.assertFalse(profile.paper_only)
        self.assertFalse(profile.real_trading_enabled)


class TestBoolParsing(unittest.TestCase):
    """Bool fields parse truthy/falsy string values correctly."""

    def test_enabled_false(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_ENABLED": "false"}
        )
        self.assertFalse(profile.enabled)

    def test_enabled_zero(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_ENABLED": "0"}
        )
        self.assertFalse(profile.enabled)

    def test_enabled_no(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_ENABLED": "no"}
        )
        self.assertFalse(profile.enabled)

    def test_enabled_yes(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_ENABLED": "yes"}
        )
        self.assertTrue(profile.enabled)


class TestNumericParsing(unittest.TestCase):
    """Numeric fields parse correctly and raise ValueError on invalid input."""

    def test_bankroll_parsed(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_BANKROLL_USDC": "500.0"}
        )
        self.assertAlmostEqual(profile.bankroll_usdc, 500.0)

    def test_max_concurrent_positions_parsed(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "INSTANCE_MAX_CONCURRENT_POSITIONS": "10"}
        )
        self.assertEqual(profile.max_concurrent_positions, 10)

    def test_default_loop_seconds_parsed(self):
        profile = load_instance_profile(
            env={"INSTANCE_TAG": "t", "STRATEGY_LOOP_SECONDS": "30"}
        )
        self.assertEqual(profile.default_loop_seconds, 30)

    def test_invalid_bankroll_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "INSTANCE_BANKROLL_USDC": "not-a-number"}
            )

    def test_invalid_max_total_exposure_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "INSTANCE_MAX_TOTAL_EXPOSURE_USDC": "abc"}
            )

    def test_invalid_max_market_exposure_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "INSTANCE_MAX_MARKET_EXPOSURE_USDC": "??"}
            )

    def test_invalid_max_concurrent_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "INSTANCE_MAX_CONCURRENT_POSITIONS": "ten"}
            )

    def test_invalid_daily_loss_limit_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "INSTANCE_DAILY_LOSS_LIMIT_USDC": "nan-ish"}
            )

    def test_invalid_loop_seconds_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_instance_profile(
                env={"INSTANCE_TAG": "t", "STRATEGY_LOOP_SECONDS": "fast"}
            )

    def test_all_numeric_fields_explicit(self):
        profile = load_instance_profile(
            env={
                "INSTANCE_TAG": "t",
                "INSTANCE_BANKROLL_USDC": "2000.0",
                "INSTANCE_MAX_TOTAL_EXPOSURE_USDC": "1500.0",
                "INSTANCE_MAX_MARKET_EXPOSURE_USDC": "300.0",
                "INSTANCE_MAX_CONCURRENT_POSITIONS": "15",
                "INSTANCE_DAILY_LOSS_LIMIT_USDC": "75.0",
                "STRATEGY_LOOP_SECONDS": "120",
            }
        )
        self.assertAlmostEqual(profile.bankroll_usdc, 2000.0)
        self.assertAlmostEqual(profile.max_total_exposure_usdc, 1500.0)
        self.assertAlmostEqual(profile.max_market_exposure_usdc, 300.0)
        self.assertEqual(profile.max_concurrent_positions, 15)
        self.assertAlmostEqual(profile.daily_loss_limit_usdc, 75.0)
        self.assertEqual(profile.default_loop_seconds, 120)


class TestImmutability(unittest.TestCase):
    """InstanceProfile is frozen (immutable)."""

    def test_profile_is_frozen(self):
        profile = load_instance_profile(env={"INSTANCE_TAG": "t"})
        with self.assertRaises((AttributeError, TypeError)):
            profile.instance_tag = "mutated"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
