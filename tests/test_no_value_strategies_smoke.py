"""Smoke tests for political_no_value_v1 and sports_no_value_v1 strategy plugins.

These two files were at 14% and 15% coverage respectively. The main risks
from the code review changes:
  1. Import path for PAPER_BANKROLL_USDC moved from os.environ.get to
     from sniper.settings.paper import PAPER_BANKROLL_USDC — must not break.
  2. Import path for kelly_size moved to from filters import kelly_size — ditto.
  3. kelly_size(bankroll=PAPER_BANKROLL_USDC) must produce a valid non-zero
     bet size when there is genuine edge.

These tests are *smoke tests* — they verify the plugin instantiates, key
constants are reachable, and the core sizing path produces a sensible result.
Full strategy entry/exit scenario tests would require a running DB + signal
harness; that's a separate effort.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from sniper.strategies.political_no_value_v1 import (
    PoliticalNoValueV1Strategy,
    DEFAULTS as POL_DEFAULTS,
)
from sniper.strategies.sports_no_value_v1 import (
    SportsNoValueV1Strategy,
    DEFAULTS as SPO_DEFAULTS,
)
from sniper.filters import kelly_size
import sniper.settings.paper as paper_settings


# ---------------------------------------------------------------------------
# Import / instantiation smoke tests
# ---------------------------------------------------------------------------

class TestPoliticalNoValueV1Import(unittest.TestCase):

    def test_plugin_instantiates(self):
        plugin = PoliticalNoValueV1Strategy()
        self.assertIsNotNone(plugin)

    def test_plugin_id(self):
        self.assertEqual(PoliticalNoValueV1Strategy.plugin_id, "political_no_value_v1")

    def test_paper_bankroll_usdc_is_importable(self):
        """PAPER_BANKROLL_USDC must come from paper_settings, not inline os.environ."""
        import sniper.strategies.political_no_value_v1 as mod
        # The module must reference PAPER_BANKROLL_USDC at import time (not os.environ)
        self.assertTrue(
            hasattr(mod, "PAPER_BANKROLL_USDC"),
            "PAPER_BANKROLL_USDC must be importable at module scope"
        )
        self.assertEqual(mod.PAPER_BANKROLL_USDC, paper_settings.PAPER_BANKROLL_USDC)

    def test_kelly_size_is_shared_from_filters(self):
        """kelly_size must come from filters, not be defined locally."""
        import sniper.strategies.political_no_value_v1 as mod
        import sniper.filters as filters
        # Module alias _kelly_size must point to the same function object
        self.assertIs(mod._kelly_size, filters.kelly_size)

    def test_defaults_are_valid(self):
        self.assertGreater(POL_DEFAULTS["min_yes_price"], 0)
        self.assertLess(POL_DEFAULTS["max_yes_price"], 1.0)
        self.assertGreater(POL_DEFAULTS["max_yes_price"], POL_DEFAULTS["min_yes_price"])
        self.assertGreater(POL_DEFAULTS["kelly_fraction"], 0)
        self.assertGreater(POL_DEFAULTS["estimated_win_prob"], 0.5)  # must be >50% to have edge


class TestSportsNoValueV1Import(unittest.TestCase):

    def test_plugin_instantiates(self):
        plugin = SportsNoValueV1Strategy()
        self.assertIsNotNone(plugin)

    def test_plugin_id(self):
        self.assertEqual(SportsNoValueV1Strategy.plugin_id, "sports_no_value_v1")

    def test_paper_bankroll_usdc_is_importable(self):
        import sniper.strategies.sports_no_value_v1 as mod
        self.assertTrue(hasattr(mod, "PAPER_BANKROLL_USDC"))
        self.assertEqual(mod.PAPER_BANKROLL_USDC, paper_settings.PAPER_BANKROLL_USDC)

    def test_kelly_size_is_shared_from_filters(self):
        import sniper.strategies.sports_no_value_v1 as mod
        import sniper.filters as filters
        self.assertIs(mod._kelly_size, filters.kelly_size)

    def test_defaults_are_valid(self):
        self.assertGreater(SPO_DEFAULTS["min_yes_price"], 0)
        self.assertLess(SPO_DEFAULTS["max_yes_price"], 1.0)
        self.assertGreater(SPO_DEFAULTS["max_yes_price"], SPO_DEFAULTS["min_yes_price"])
        self.assertGreater(SPO_DEFAULTS["estimated_win_prob"], 0.5)


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok
        self.status_code = 200 if ok else 500

    def json(self):
        return self._payload


class TestSportsNoValueV1Discover(unittest.TestCase):
    def _market(self, *, tags=None):
        end_dt = datetime.now(timezone.utc) + timedelta(hours=24)
        return {
            "question": "Will Arsenal win on 2026-03-26",
            "outcomes": ["Yes", "No"],
            "volume24hr": 250.0,
            "endDate": end_dt.isoformat(),
            "conditionId": "0x" + "1" * 64,
            "outcomePrices": [0.12, 0.88],
            "category": "Sports",
            "tags": tags or ["soccer", "premier league"],
        }

    @patch("requests.get")
    def test_discover_applies_allowed_categories_filter(self, mock_get):
        mock_get.return_value = _FakeResponse([self._market(tags=["soccer", "premier league"])])

        plugin = SportsNoValueV1Strategy()
        signals = plugin.discover(
            conn=None,
            strategy={"parameters": {"allowed_categories": ["soccer"]}},
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].metadata["yes_price"], 0.12)

    @patch("requests.get")
    def test_discover_rejects_non_matching_categories(self, mock_get):
        mock_get.return_value = _FakeResponse([self._market(tags=["tennis"])])

        plugin = SportsNoValueV1Strategy()
        signals = plugin.discover(
            conn=None,
            strategy={"parameters": {"allowed_categories": ["soccer"]}},
        )

        self.assertEqual(signals, [])

    @patch("requests.get")
    def test_discover_supports_legacy_allowedcategories_alias(self, mock_get):
        mock_get.return_value = _FakeResponse([self._market(tags=["soccer"])])

        plugin = SportsNoValueV1Strategy()
        signals = plugin.discover(
            conn=None,
            strategy={"parameters": {"allowedcategories": ["soccer"]}},
        )

        self.assertEqual(len(signals), 1)


# ---------------------------------------------------------------------------
# kelly_size with PAPER_BANKROLL_USDC — core sizing path
# ---------------------------------------------------------------------------

class TestKellySizeWithBankroll(unittest.TestCase):
    """Verify the shared kelly_size function produces valid results when called
    with PAPER_BANKROLL_USDC as the bankroll parameter — exactly as both
    strategy plugins do it."""

    def setUp(self):
        self.bankroll = paper_settings.PAPER_BANKROLL_USDC
        self.kelly_frac = 0.10  # both plugins default to 10% Kelly

    def test_political_typical_scenario_produces_positive_size(self):
        """YES=0.30 (dramatic political event), win_prob=0.88 → strong NO edge → positive bet."""
        size = kelly_size(
            yes_price=0.30,
            estimated_win_prob=0.88,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertGreater(size, 0, "Political NO bet at YES=0.30 with 88% win prob must produce positive size")
        self.assertGreaterEqual(size, 10.0, "Must respect min_size")
        self.assertLessEqual(size, 50.0, "Must respect max_size")

    def test_sports_typical_scenario_produces_positive_size(self):
        """YES=0.20 (underdog team), win_prob=0.85 → strong NO edge → positive bet."""
        size = kelly_size(
            yes_price=0.20,
            estimated_win_prob=0.85,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertGreater(size, 0)
        self.assertGreaterEqual(size, 10.0)
        self.assertLessEqual(size, 50.0)

    def test_no_edge_returns_zero(self):
        """When YES=0.50 and win_prob=0.50, Kelly edge is 0 → size must be 0."""
        size = kelly_size(
            yes_price=0.50,
            estimated_win_prob=0.50,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertEqual(size, 0.0, "Zero edge must produce size=0 (not min_size)")

    def test_negative_edge_returns_zero(self):
        """When win_prob < 0.5 at YES=0.50, edge is negative → size must be 0."""
        size = kelly_size(
            yes_price=0.50,
            estimated_win_prob=0.30,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertEqual(size, 0.0)

    def test_size_scales_with_bankroll(self):
        """Larger bankroll → proportionally larger bet (Kelly is a fraction of bankroll)."""
        size_small = kelly_size(
            yes_price=0.30, estimated_win_prob=0.88,
            bankroll=100.0, kelly_fraction=self.kelly_frac,
            min_size=1.0, max_size=500.0,
        )
        size_large = kelly_size(
            yes_price=0.30, estimated_win_prob=0.88,
            bankroll=1000.0, kelly_fraction=self.kelly_frac,
            min_size=1.0, max_size=500.0,
        )
        self.assertGreater(size_large, size_small,
                           "Larger bankroll must produce larger bet at same edge")

    def test_min_size_respected_even_with_small_edge(self):
        """If Kelly says bet $2 but min_size=$10, result should be $10."""
        size = kelly_size(
            yes_price=0.45,
            estimated_win_prob=0.60,
            bankroll=50.0,           # tiny bankroll → tiny raw Kelly
            kelly_fraction=0.05,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertGreaterEqual(size, 10.0)

    def test_max_size_respected_with_large_bankroll(self):
        """Even with a huge bankroll, bet must be capped at max_size."""
        size = kelly_size(
            yes_price=0.20,
            estimated_win_prob=0.95,
            bankroll=1_000_000.0,    # massive bankroll
            kelly_fraction=0.25,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertLessEqual(size, 50.0)

    def test_zero_yes_price_returns_min_size(self):
        """Edge case: yes_price=0 → no_price=1 → Kelly undefined → return min_size."""
        size = kelly_size(
            yes_price=0.0,
            estimated_win_prob=0.90,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        self.assertEqual(size, 10.0)

    def test_result_is_rounded_to_cents(self):
        """Bet sizes must be rounded to 2 decimal places (cents precision)."""
        size = kelly_size(
            yes_price=0.30,
            estimated_win_prob=0.88,
            bankroll=self.bankroll,
            kelly_fraction=self.kelly_frac,
            min_size=10.0,
            max_size=50.0,
        )
        # Check it's a round number to 2 decimal places
        self.assertAlmostEqual(size, round(size, 2), places=5)


# ---------------------------------------------------------------------------
# _category_matches — unit tests for the sports_no_value_v1 helper
# ---------------------------------------------------------------------------

from sniper.strategies.sports_no_value_v1 import _category_matches


class TestCategoryMatches(unittest.TestCase):
    """Unit tests for _category_matches() — the previously-dead allowed_categories gate."""

    def test_empty_allowed_passes_any_market(self):
        """Empty list = allow all. Nothing should be filtered."""
        market = {"category": "baseball"}
        self.assertTrue(_category_matches(market, []))

    def test_matching_category_field_passes(self):
        market = {"category": "Soccer", "question": "Will Arsenal win on 2026-04-01?"}
        self.assertTrue(_category_matches(market, ["soccer"]))

    def test_non_matching_category_field_blocked(self):
        market = {"category": "baseball", "question": "Will Arsenal win on 2026-04-01?"}
        # "soccer" not in "baseball" and not in title
        self.assertFalse(_category_matches(market, ["soccer"]))

    def test_case_insensitive_match(self):
        market = {"category": "SOCCER"}
        self.assertTrue(_category_matches(market, ["Soccer"]))
        self.assertTrue(_category_matches(market, ["soccer"]))
        self.assertTrue(_category_matches(market, ["SOCCER"]))

    def test_category_in_tags_list_passes(self):
        market = {"tags": ["sports", "champions league", "ucl"]}
        self.assertTrue(_category_matches(market, ["champions league"]))

    def test_category_in_tags_string_passes(self):
        market = {"tags": "premier league"}
        self.assertTrue(_category_matches(market, ["premier league"]))

    def test_no_category_fields_falls_back_to_title(self):
        """When Gamma API returns no category fields, title is the fallback."""
        market = {"question": "Will Liverpool win on 2026-04-05?"}
        # "soccer" is not in the title, but "liverpool" triggers no match — filtered
        self.assertFalse(_category_matches(market, ["soccer"]))

    def test_title_fallback_matches_sport_keyword(self):
        """'nba' in title should match allowed_categories=['nba']."""
        market = {"question": "Will Celtics win the NBA game on 2026-04-10?"}
        self.assertTrue(_category_matches(market, ["nba"]))

    def test_multiple_allowed_any_match_passes(self):
        market = {"category": "nhl"}
        # Either "soccer" or "nhl" in allowed → passes
        self.assertTrue(_category_matches(market, ["soccer", "football", "nhl"]))

    def test_multiple_allowed_no_match_blocked(self):
        market = {"category": "esports"}
        self.assertFalse(_category_matches(market, ["soccer", "football", "nhl", "nba"]))

    def test_substring_match_in_category(self):
        """'champions league' should match a category string containing it."""
        market = {"category": "UEFA Champions League 2025-26"}
        self.assertTrue(_category_matches(market, ["champions league"]))

    def test_sport_field_checked(self):
        market = {"sport": "football"}
        self.assertTrue(_category_matches(market, ["football"]))

    def test_league_field_checked(self):
        market = {"league": "La Liga"}
        self.assertTrue(_category_matches(market, ["la liga"]))


# ---------------------------------------------------------------------------
# discover() integration — category filter actually blocks/passes signals
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


def _make_gamma_market(
    title="Will Arsenal win on 2026-04-01?",
    condition_id="cond_abc123",
    category="soccer",
    yes_price=0.12,
    volume=500.0,
    hours_from_now=24,
):
    """Build a minimal Gamma API market dict for discover() tests."""
    end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return {
        "question": title,
        "conditionId": condition_id,
        "outcomes": '["Yes", "No"]',
        "volume24hr": volume,
        "endDate": end_dt.isoformat(),
        "outcomePrices": [str(yes_price), str(round(1.0 - yes_price, 4))],
        "category": category,
    }


def _make_strategy_row(params: dict) -> dict:
    import json
    return {"id": 18, "strategy_type": "sports_no_value_v1", "parameters": json.dumps(params)}


class TestSportsNoValueV1DiscoverCategoryFilter(unittest.TestCase):
    """Tests that discover() honours allowed_categories at signal-generation time."""

    def _run_discover(self, gamma_markets, strategy_params):
        plugin = SportsNoValueV1Strategy()
        strategy = _make_strategy_row(strategy_params)

        yes_price = strategy_params.get("_test_yes_price", 0.12)
        clob_prices = {"yes": yes_price, "no": round(1.0 - yes_price, 4)}

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = gamma_markets

        # requests is imported locally inside discover(), so patch at the package level.
        with patch("requests.get", return_value=mock_resp), \
             patch("sniper.strategies.sports_no_value_v1._fetch_all_prices_for_market",
                   return_value=clob_prices):
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            return plugin.discover(conn, strategy)

    def test_matching_category_produces_signal(self):
        market = _make_gamma_market(category="soccer")
        params = {
            "min_yes_price": 0.05, "max_yes_price": 0.14,
            "min_volume_24h": 100.0, "max_hours_to_end": 72,
            "min_hours_to_end": 0.5, "allowed_categories": ["soccer"],
        }
        signals = self._run_discover([market], params)
        self.assertEqual(len(signals), 1, "Soccer market with soccer in allowed_categories must produce a signal")
        self.assertEqual(signals[0].outcome, "No")

    def test_non_matching_category_blocked(self):
        market = _make_gamma_market(category="esports")
        params = {
            "min_yes_price": 0.05, "max_yes_price": 0.14,
            "min_volume_24h": 100.0, "max_hours_to_end": 72,
            "min_hours_to_end": 0.5, "allowed_categories": ["soccer", "football"],
        }
        signals = self._run_discover([market], params)
        self.assertEqual(len(signals), 0, "Esports market must be filtered when allowed_categories=['soccer','football']")

    def test_empty_allowed_categories_passes_any_sport(self):
        market = _make_gamma_market(category="curling")
        params = {
            "min_yes_price": 0.05, "max_yes_price": 0.14,
            "min_volume_24h": 100.0, "max_hours_to_end": 72,
            "min_hours_to_end": 0.5, "allowed_categories": [],
        }
        signals = self._run_discover([market], params)
        self.assertEqual(len(signals), 1, "Empty allowed_categories must allow all sports through")

    def test_mixed_markets_only_matching_pass(self):
        markets = [
            _make_gamma_market(title="Will Arsenal win on 2026-04-01?",
                               condition_id="cond_001", category="soccer"),
            _make_gamma_market(title="Will Lakers win on 2026-04-01?",
                               condition_id="cond_002", category="nba"),
            _make_gamma_market(title="Will Chiefs win on 2026-04-01?",
                               condition_id="cond_003", category="nfl"),
        ]
        params = {
            "min_yes_price": 0.05, "max_yes_price": 0.14,
            "min_volume_24h": 100.0, "max_hours_to_end": 72,
            "min_hours_to_end": 0.5, "allowed_categories": ["soccer", "nba"],
        }
        signals = self._run_discover(markets, params)
        condition_ids = {s.condition_id for s in signals}
        self.assertIn("cond_001", condition_ids, "Soccer market must pass")
        self.assertIn("cond_002", condition_ids, "NBA market must pass")
        self.assertNotIn("cond_003", condition_ids, "NFL market must be filtered")


if __name__ == "__main__":
    unittest.main()
