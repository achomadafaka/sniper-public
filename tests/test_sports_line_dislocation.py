# tests/test_sports_line_dislocation.py
"""Tests for sports_line_dislocation_v1 strategy plugin."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from db import init_db, ensure_odds_snapshots_table, insert_odds_snapshot
from sniper.strategies.base import StrategyManageState
from sniper.strategies.sports_line_dislocation_v1 import DEFAULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_odds_snapshots_table(conn)
    return conn


def _seed_market(
    conn,
    condition_id: str,
    title: str,
    is_open: int = 1,
    volume_24h: float = 5000.0,
    end_date: str = "2026-04-01T00:00:00Z",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, end_date_iso, category,
            category_source, volume_24h, is_open, last_synced_ts)
           VALUES (?, ?, '', '', ?, '', '', ?, ?, ?)""",
        (condition_id, title, end_date, volume_24h, is_open, int(time.time())),
    )
    conn.commit()


def _seed_odds_pair(
    conn,
    event_id: str = "evt_001",
    sport_key: str = "basketball_nba",
    home_team: str = "Boston Celtics",
    away_team: str = "Milwaukee Bucks",
    bookmaker_key: str = "fanduel",
    commence_time: str | None = None,
    home_prob: float = 0.592,
    away_prob: float = 0.444,
    canonical_event_hash: str = "hash_001",
    market_ts: str | None = None,
) -> None:
    """Seed a pair of odds snapshot rows (one per outcome) for a bookmaker."""
    if commence_time is None:
        commence_time = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if market_ts is None:
        market_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    home_dec = round(1.0 / home_prob, 4) if home_prob > 0 else 100.0
    away_dec = round(1.0 / away_prob, 4) if away_prob > 0 else 100.0

    # Insert home outcome row
    insert_odds_snapshot(
        conn,
        instance_tag="test",
        event_id=event_id,
        sport_key=sport_key,
        commence_time=commence_time,
        home_team=home_team,
        away_team=away_team,
        bookmaker_key=bookmaker_key,
        outcome_name=home_team,
        price_decimal=home_dec,
        implied_prob_raw=round(home_prob, 4),
        canonical_event_hash=canonical_event_hash,
        market_ts=market_ts,
    )
    # Insert away outcome row
    insert_odds_snapshot(
        conn,
        instance_tag="test",
        event_id=event_id,
        sport_key=sport_key,
        commence_time=commence_time,
        home_team=home_team,
        away_team=away_team,
        bookmaker_key=bookmaker_key,
        outcome_name=away_team,
        price_decimal=away_dec,
        implied_prob_raw=round(away_prob, 4),
        canonical_event_hash=canonical_event_hash,
        market_ts=market_ts,
    )


class TestNoVigMath(unittest.TestCase):
    """No-vig fair probability calculation."""

    def test_remove_vig_binary(self):
        from sniper.strategies.sports_line_dislocation_v1 import remove_vig_binary
        # Raw probs: 0.592 + 0.444 = 1.036 (3.6% vig)
        fair_home, fair_away = remove_vig_binary(0.592, 0.444)
        self.assertAlmostEqual(fair_home + fair_away, 1.0, places=5)
        self.assertAlmostEqual(fair_home, 0.592 / 1.036, places=3)
        self.assertAlmostEqual(fair_away, 0.444 / 1.036, places=3)

    def test_remove_vig_zero_prob(self):
        from sniper.strategies.sports_line_dislocation_v1 import remove_vig_binary
        # Edge case: one side is zero
        fair_home, fair_away = remove_vig_binary(0.0, 1.0)
        self.assertAlmostEqual(fair_home, 0.0, places=5)
        self.assertAlmostEqual(fair_away, 1.0, places=5)

    def test_remove_vig_both_zero(self):
        from sniper.strategies.sports_line_dislocation_v1 import remove_vig_binary
        # Edge case: both zero → 50/50 fallback
        fair_home, fair_away = remove_vig_binary(0.0, 0.0)
        self.assertAlmostEqual(fair_home, 0.5, places=5)
        self.assertAlmostEqual(fair_away, 0.5, places=5)


class TestConsensus(unittest.TestCase):
    """Bookmaker consensus calculation."""

    def test_median_consensus_odd_count(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_consensus
        fair_probs = [0.55, 0.60, 0.57]  # median = 0.57
        result = compute_consensus(fair_probs)
        self.assertAlmostEqual(result["median"], 0.57, places=3)
        self.assertEqual(result["books_used"], 3)

    def test_median_consensus_even_count(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_consensus
        fair_probs = [0.55, 0.60, 0.57, 0.58]  # median = (0.57+0.58)/2 = 0.575
        result = compute_consensus(fair_probs)
        self.assertAlmostEqual(result["median"], 0.575, places=3)

    def test_dispersion_calculation(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_consensus
        fair_probs = [0.55, 0.60, 0.57]
        result = compute_consensus(fair_probs)
        self.assertAlmostEqual(result["dispersion"], 0.05, places=3)  # 0.60 - 0.55

    def test_consensus_single_book(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_consensus
        fair_probs = [0.60]
        result = compute_consensus(fair_probs)
        self.assertAlmostEqual(result["median"], 0.60)
        self.assertAlmostEqual(result["dispersion"], 0.0)

    def test_consensus_empty_list(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_consensus
        result = compute_consensus([])
        self.assertEqual(result["median"], 0.0)
        self.assertEqual(result["dispersion"], 0.0)
        self.assertEqual(result["books_used"], 0)


class TestMapTeamsToOutcomes(unittest.TestCase):
    """Team name to Polymarket outcome label mapping."""

    def test_exact_match(self):
        from sniper.strategies.sports_line_dislocation_v1 import _map_teams_to_outcomes
        outcomes = [{"outcome": "Boston Celtics"}, {"outcome": "Milwaukee Bucks"}]
        result = _map_teams_to_outcomes("Boston Celtics", "Milwaukee Bucks", outcomes)
        self.assertEqual(result.get("boston celtics"), "Boston Celtics")
        self.assertEqual(result.get("milwaukee bucks"), "Milwaukee Bucks")

    def test_short_name_match(self):
        from sniper.strategies.sports_line_dislocation_v1 import _map_teams_to_outcomes
        # Polymarket might use just "Celtics" and "Bucks"
        outcomes = [{"outcome": "Celtics"}, {"outcome": "Bucks"}]
        result = _map_teams_to_outcomes("Boston Celtics", "Milwaukee Bucks", outcomes)
        self.assertEqual(result.get("boston celtics"), "Celtics")
        self.assertEqual(result.get("milwaukee bucks"), "Bucks")

    def test_yes_no_fallback(self):
        from sniper.strategies.sports_line_dislocation_v1 import _map_teams_to_outcomes
        outcomes = [{"outcome": "Yes"}, {"outcome": "No"}]
        result = _map_teams_to_outcomes("Boston Celtics", "Milwaukee Bucks", outcomes)
        self.assertEqual(result.get("boston celtics"), "Yes")
        self.assertEqual(result.get("milwaukee bucks"), "No")

    def test_empty_outcomes(self):
        from sniper.strategies.sports_line_dislocation_v1 import _map_teams_to_outcomes
        result = _map_teams_to_outcomes("Boston Celtics", "Milwaukee Bucks", [])
        self.assertEqual(result, {})


class TestParseParams(unittest.TestCase):
    """Parameter parsing helper."""

    def test_dict_passthrough(self):
        from sniper.strategies.sports_line_dislocation_v1 import _parse_params
        strategy = {"parameters": {"min_edge_pts": 0.08}}
        result = _parse_params(strategy)
        self.assertEqual(result["min_edge_pts"], 0.08)

    def test_json_string(self):
        from sniper.strategies.sports_line_dislocation_v1 import _parse_params
        strategy = {"parameters": '{"min_edge_pts": 0.08}'}
        result = _parse_params(strategy)
        self.assertEqual(result["min_edge_pts"], 0.08)

    def test_invalid_json(self):
        from sniper.strategies.sports_line_dislocation_v1 import _parse_params
        strategy = {"parameters": "not json"}
        result = _parse_params(strategy)
        self.assertEqual(result, {})

    def test_none_params(self):
        from sniper.strategies.sports_line_dislocation_v1 import _parse_params
        strategy = {"parameters": None}
        result = _parse_params(strategy)
        self.assertEqual(result, {})


class TestDiscover(unittest.TestCase):
    """discover() signal generation."""

    def _make_strategy(self, **overrides) -> dict:
        params = {
            "min_edge_pts": 0.06,
            "min_books": 3,
            "max_consensus_dispersion_pts": 0.04,
            "max_entry_price": 0.75,
            "hours_ahead": 72,
            "sport_keys": "basketball_nba",
        }
        params.update(overrides)
        return {"id": 1, "strategy_type": "sports_line_dislocation_v1", "parameters": params}

    def test_discover_emits_signal_when_edge_exceeds_threshold(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # 3 books agree Celtics ~57% fair prob (raw ~59.2% with vig)
        for i, bm in enumerate(["fanduel", "draftkings", "betmgm"]):
            _seed_odds_pair(
                conn, bookmaker_key=bm,
                home_prob=0.592, away_prob=0.444,
                commence_time=commence,
                market_ts=datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:%M:{i:02d}Z"),
            )
        # Polymarket has Celtics at 0.43 (edge = ~0.57 - 0.43 = 0.14)
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        # Seed Polymarket outcome labels (team names, not Yes/No)
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Boston Celtics"),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Milwaukee Bucks"),
        )
        conn.commit()

        strategy = self._make_strategy()
        plugin = SportsLineDislocationV1Strategy()

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            side_effect=lambda cid, outcome: 0.43 if "celtics" in outcome.lower() else 0.57,
        ):
            signals = plugin.discover(conn, strategy)

        self.assertGreater(len(signals), 0)
        sig = signals[0]
        self.assertEqual(sig.condition_id, "cond_cel")
        self.assertGreater(sig.metadata.get("edge_pts", 0), 0.06)
        # Outcome should be the team name, not YES/NO
        self.assertIn("Celtics", sig.outcome)

    def test_discover_skips_below_min_books(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Only 2 books (threshold is 3)
        for bm in ["fanduel", "draftkings"]:
            _seed_odds_pair(
                conn, bookmaker_key=bm, home_prob=0.592, away_prob=0.444,
                commence_time=commence,
                market_ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Boston Celtics"),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Milwaukee Bucks"),
        )
        conn.commit()

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_books=3)

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=0.43,
        ):
            signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_skips_high_dispersion(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # 3 books but wide disagreement on home team
        probs = [(0.55, 0.45), (0.65, 0.35), (0.60, 0.40)]
        for i, (hp, ap) in enumerate(probs):
            _seed_odds_pair(
                conn, bookmaker_key=f"book{i}",
                home_prob=hp, away_prob=ap,
                commence_time=commence,
                market_ts=datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:%M:{i:02d}Z"),
            )
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Boston Celtics"),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Milwaukee Bucks"),
        )
        conn.commit()

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(max_consensus_dispersion_pts=0.04)

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=0.43,
        ):
            signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_skips_no_edge(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # 3 books with tight consensus (low dispersion)
        for i, bm in enumerate(["fanduel", "draftkings", "betmgm"]):
            _seed_odds_pair(
                conn, bookmaker_key=bm,
                home_prob=0.55, away_prob=0.48,
                commence_time=commence,
                market_ts=datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:%M:{i:02d}Z"),
            )
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        # Seed market outcomes so mapping works
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Boston Celtics"),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Milwaukee Bucks"),
        )
        conn.commit()

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_edge_pts=0.06)

        # Polymarket price close to fair value — no edge
        # Fair prob home ~0.534 (0.55/1.03), so Poly at 0.52 → edge ~0.014 < 0.06
        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            side_effect=lambda cid, outcome: 0.52 if "celtics" in outcome.lower() else 0.48,
        ):
            signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_skips_when_no_matching_polymarket_market(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for bm in ["fanduel", "draftkings", "betmgm"]:
            _seed_odds_pair(
                conn, bookmaker_key=bm,
                home_prob=0.592, away_prob=0.444,
                commence_time=commence,
            )
        # No matching market_metadata seeded → find_matching_polymarket_h2h_markets returns []

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=0.43,
        ):
            signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_skips_when_no_outcome_mapping(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for bm in ["fanduel", "draftkings", "betmgm"]:
            _seed_odds_pair(
                conn, bookmaker_key=bm,
                home_prob=0.592, away_prob=0.444,
                commence_time=commence,
            )
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        # No market_outcomes seeded → _map_teams_to_outcomes returns {}

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=0.43,
        ):
            signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_returns_empty_when_no_snapshots(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()
        signals = plugin.discover(conn, strategy)
        self.assertEqual(len(signals), 0)

    def test_discover_signal_metadata_fields(self):
        """Verify that emitted signals include expected metadata keys."""
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        for i, bm in enumerate(["fanduel", "draftkings", "betmgm"]):
            _seed_odds_pair(
                conn, bookmaker_key=bm,
                home_prob=0.592, away_prob=0.444,
                commence_time=commence,
                market_ts=datetime.now(timezone.utc).strftime(f"%Y-%m-%dT%H:%M:{i:02d}Z"),
            )
        _seed_market(conn, "cond_cel", "Boston Celtics vs. Milwaukee Bucks",
                     end_date=commence)
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Boston Celtics"),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome) VALUES (?, ?)",
            ("cond_cel", "Milwaukee Bucks"),
        )
        conn.commit()

        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()

        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            side_effect=lambda cid, outcome: 0.43 if "celtics" in outcome.lower() else 0.57,
        ):
            signals = plugin.discover(conn, strategy)

        self.assertGreater(len(signals), 0)
        meta = signals[0].metadata
        expected_keys = {
            "canonical_event_hash", "sport_key", "home_team", "away_team",
            "commence_time", "selected_team", "selected_side",
            "polymarket_price", "fair_prob", "edge_pts",
            "books_used", "consensus_dispersion", "insider_convergence",
        }
        self.assertTrue(expected_keys.issubset(set(meta.keys())),
                        f"Missing keys: {expected_keys - set(meta.keys())}")


def _now_ts():
    return int(datetime.now(timezone.utc).timestamp())


class TestValidateParameters(unittest.TestCase):
    """validate_parameters() validation."""

    def test_valid_params(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        errors = plugin.validate_parameters({"min_edge_pts": 0.06, "min_books": 3})
        self.assertEqual(errors, [])

    def test_invalid_numeric_param(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        errors = plugin.validate_parameters({"min_edge_pts": "not_a_number"})
        self.assertEqual(len(errors), 1)
        self.assertIn("min_edge_pts", errors[0])


class TestPluginAttributes(unittest.TestCase):
    """Verify class attributes match contract."""

    def test_plugin_id(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        self.assertEqual(plugin.plugin_id, "sports_line_dislocation_v1")

    def test_strategy_family(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        self.assertEqual(plugin.strategy_family, "sports_line_dislocation")

    def test_required_feeds(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        self.assertIn("odds", plugin.required_feeds)
        self.assertIn("polymarket", plugin.required_feeds)

    def test_replay_adapter_returns_none(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        self.assertIsNone(plugin.replay_adapter())

    def test_default_loop_seconds(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        plugin = SportsLineDislocationV1Strategy()
        self.assertEqual(plugin.default_loop_seconds, 300)


class TestEnter(unittest.TestCase):
    """enter() gate logic."""

    def _make_strategy(self, **overrides) -> dict:
        params = dict(DEFAULTS)
        params.update(overrides)
        return {"id": 1, "strategy_type": "sports_line_dislocation_v1", "parameters": params}

    def _make_signal(self, **overrides) -> "StrategySignal":
        from sniper.strategies.base import StrategySignal
        defaults = dict(
            wallet="odds_hash001",
            condition_id="cond_001",
            outcome="Boston Celtics",
            title="Boston Celtics vs. Milwaukee Bucks",
            current_price=0.43,
            position_value=0.0,
            analyzed_ts=_now_ts(),
            metadata={
                "canonical_event_hash": "hash_001",
                "sport_key": "basketball_nba",
                "home_team": "Boston Celtics",
                "away_team": "Milwaukee Bucks",
                "commence_time": (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "selected_team": "Boston Celtics",
                "selected_side": "yes",
                "polymarket_price": 0.43,
                "fair_prob": 0.57,
                "edge_pts": 0.14,
                "books_used": 4,
                "consensus_dispersion": 0.02,
            },
        )
        defaults.update(overrides)
        return StrategySignal(**defaults)

    def test_enter_approves_valid_signal(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()
        signal = self._make_signal()
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertTrue(decision.should_enter)
        self.assertAlmostEqual(decision.size_usdc, 10.0)

    def test_enter_blocks_duplicate_position(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()
        signal = self._make_signal()
        open_positions = [{"condition_id": "cond_001", "outcome": "Boston Celtics"}]
        decision = plugin.enter(conn, strategy, signal, open_positions)
        self.assertFalse(decision.should_enter)
        self.assertIn("duplicate", decision.reason.lower())

    def test_enter_blocks_max_concurrent(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(max_concurrent_positions=2)
        signal = self._make_signal()
        open_positions = [
            {"condition_id": "cond_a", "outcome": "YES"},
            {"condition_id": "cond_b", "outcome": "YES"},
        ]
        decision = plugin.enter(conn, strategy, signal, open_positions)
        self.assertFalse(decision.should_enter)
        self.assertIn("concurrent", decision.reason.lower())

    def test_enter_blocks_high_price(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(max_entry_price=0.50)
        signal = self._make_signal(current_price=0.65)
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("price", decision.reason.lower())

    def test_enter_blocks_late_entry(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_minutes_before_start=30)
        # Event starts in 10 minutes
        commence = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        signal = self._make_signal()
        signal.metadata["commence_time"] = commence
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("start", decision.reason.lower())

    def test_enter_blocks_insufficient_edge(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_edge_pts=0.10)
        signal = self._make_signal()
        signal.metadata["edge_pts"] = 0.05
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("edge", decision.reason.lower())

    def test_enter_blocks_nba_with_3_books_by_default(self):
        """NBA games only have 3 books — default min_books=4 blocks them."""
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy()  # default min_books=4
        signal = self._make_signal()
        signal.metadata["books_used"] = 3
        signal.metadata["sport_key"] = "basketball_nba"
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("books", decision.reason.lower())

    def test_enter_allows_nba_with_3_books_via_override(self):
        """min_books_override={"basketball_nba": 3} unlocks 3-book NBA signals."""
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_books_override={"basketball_nba": 3})
        signal = self._make_signal()
        signal.metadata["books_used"] = 3
        signal.metadata["sport_key"] = "basketball_nba"
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertTrue(decision.should_enter, decision.reason)

    def test_enter_still_blocks_ncaab_with_3_books_despite_nba_override(self):
        """NBA override must not bleed into NCAAB — NCAAB keeps min_books=4."""
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        strategy = self._make_strategy(min_books_override={"basketball_nba": 3})
        signal = self._make_signal()
        signal.metadata["books_used"] = 3
        signal.metadata["sport_key"] = "basketball_ncaab"
        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("books", decision.reason.lower())


class TestManageExit(unittest.TestCase):
    """manage() and exit() lifecycle."""

    def _make_strategy(self, **overrides) -> dict:
        params = dict(DEFAULTS)
        params.update(overrides)
        return {"id": 1, "strategy_type": "sports_line_dislocation_v1", "parameters": params}

    def _make_position(self, **overrides) -> dict:
        defaults = {
            "id": 1,
            "strategy_id": 1,
            "condition_id": "cond_001",
            "outcome": "Boston Celtics",
            "avg_entry_price": 0.43,
            "entry_price": 0.43,
            "size_usdc": 10.0,
            "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "plugin_state": json.dumps({
                "canonical_event_hash": "hash_001",
                "commence_time": (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fair_prob": 0.57,
                "edge_pts": 0.14,
                "selected_team": "Boston Celtics",
            }),
        }
        defaults.update(overrides)
        return defaults

    def test_manage_returns_live_price(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=0.55,
        ):
            state = plugin.manage(conn, self._make_strategy(), pos)
        self.assertAlmostEqual(state.current_price, 0.55)

    def test_manage_falls_back_to_entry_price(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        with patch(
            "sniper.strategies.sports_line_dislocation_v1.fetch_live_clob_price",
            return_value=None,
        ):
            state = plugin.manage(conn, self._make_strategy(), pos)
        self.assertAlmostEqual(state.current_price, 0.43)

    def test_exit_on_resolution_win(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        state = StrategyManageState(current_price=0.98, signal_open=True)
        decision = plugin.exit(conn, self._make_strategy(), pos, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_resolved")
        self.assertEqual(decision.status, "closed")  # NOT resolution_win

    def test_exit_on_resolution_loss(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        state = StrategyManageState(current_price=0.02, signal_open=True)
        decision = plugin.exit(conn, self._make_strategy(), pos, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_resolved")
        self.assertEqual(decision.status, "closed")  # NOT resolution_loss

    def test_exit_on_market_closed(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        # Market closed but price between 0.03 and 0.97 (not resolved)
        state = StrategyManageState(current_price=0.55, signal_open=False)
        decision = plugin.exit(conn, self._make_strategy(), pos, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_closed")
        self.assertEqual(decision.status, "closed")

    def test_exit_on_convergence_take_profit(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        # Entry at 0.43, fair prob 0.57, close_gap_pts 0.02
        pos = self._make_position(entry_price=0.43)
        # Current price = 0.56 → within 0.01 of fair_prob 0.57, and in profit
        state = StrategyManageState(current_price=0.56, signal_open=True)
        decision = plugin.exit(
            conn, self._make_strategy(close_gap_pts=0.02), pos, state
        )
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "convergence_take_profit")

    def test_no_exit_when_convergence_but_not_in_profit(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        # Entry at 0.60, fair prob 0.57 → converged but not in profit
        pos = self._make_position(entry_price=0.60, avg_entry_price=0.60)
        state = StrategyManageState(current_price=0.56, signal_open=True)
        decision = plugin.exit(
            conn, self._make_strategy(close_gap_pts=0.02), pos, state
        )
        self.assertFalse(decision.should_exit)

    def test_exit_on_stale_postponed(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        # Event was supposed to start 30 hours ago
        past_commence = (
            datetime.now(timezone.utc) - timedelta(hours=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        pos = self._make_position()
        pos["plugin_state"] = json.dumps({
            "commence_time": past_commence,
            "fair_prob": 0.57,
        })
        state = StrategyManageState(current_price=0.50, signal_open=True)
        decision = plugin.exit(
            conn, self._make_strategy(postponed_grace_hours=24), pos, state
        )
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "stale_postponed")

    def test_no_exit_during_normal_hold(self):
        from sniper.strategies.sports_line_dislocation_v1 import SportsLineDislocationV1Strategy
        conn = _in_memory_conn()
        plugin = SportsLineDislocationV1Strategy()
        pos = self._make_position()
        # Price moved up but not near fair prob
        state = StrategyManageState(current_price=0.48, signal_open=True)
        decision = plugin.exit(conn, self._make_strategy(), pos, state)
        self.assertFalse(decision.should_exit)


class TestIntegration(unittest.TestCase):
    """Registry and runner integration."""

    def test_strategy_registered(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "sports_line_dislocation_v1")

    def test_feed_registered(self):
        from sniper.feeds.registry import get_feed_adapter
        adapter = get_feed_adapter("odds")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "odds")

    def test_required_feeds(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        self.assertIn("polymarket", plugin.required_feeds)
        self.assertIn("odds", plugin.required_feeds)

    def test_supported_execution_modes_paper_only(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        self.assertEqual(plugin.supported_execution_modes, ("paper",))

    def test_replay_adapter_returns_none(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        self.assertIsNone(plugin.replay_adapter())

    def test_validate_parameters_accepts_valid(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        errors = plugin.validate_parameters({"min_edge_pts": 0.06, "min_books": 3})
        self.assertEqual(errors, [])

    def test_validate_parameters_rejects_invalid(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("sports_line_dislocation_v1")
        errors = plugin.validate_parameters({"min_edge_pts": "not_a_number"})
        self.assertTrue(len(errors) > 0)


class TestConfidenceTier(unittest.TestCase):
    """Tests for the confidence tier scoring system."""

    def test_high_confidence_many_books_strong_edge(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_confidence_tier
        result = compute_confidence_tier(edge_pts=0.08, books_used=8)
        self.assertEqual(result["tier"], "high")
        self.assertEqual(result["size_multiplier"], 1.5)
        self.assertGreaterEqual(result["confidence_score"], 2.0)

    def test_medium_confidence_moderate_signal(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_confidence_tier
        # 7 books (factor=1.25) * 0.05 edge (factor=1.25) = 1.5625 → medium
        result = compute_confidence_tier(edge_pts=0.05, books_used=7)
        self.assertEqual(result["tier"], "medium")
        self.assertEqual(result["size_multiplier"], 1.0)

    def test_low_confidence_few_books(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_confidence_tier
        result = compute_confidence_tier(edge_pts=0.04, books_used=4)
        self.assertEqual(result["tier"], "low")
        self.assertEqual(result["size_multiplier"], 0.5)

    def test_8_books_4pct_beats_3_books_6pct(self):
        """Core insight: 4% edge w/ 8 books > 6% edge w/ 3 books."""
        from sniper.strategies.sports_line_dislocation_v1 import compute_confidence_tier
        deep = compute_confidence_tier(edge_pts=0.04, books_used=8)
        thin = compute_confidence_tier(edge_pts=0.06, books_used=3)
        self.assertGreater(deep["confidence_score"], thin["confidence_score"])

    def test_result_keys(self):
        from sniper.strategies.sports_line_dislocation_v1 import compute_confidence_tier
        result = compute_confidence_tier(edge_pts=0.05, books_used=5)
        expected_keys = {"tier", "confidence_score", "size_multiplier", "book_factor", "edge_factor"}
        self.assertEqual(set(result.keys()), expected_keys)


class TestUpdatedDefaults(unittest.TestCase):
    """Verify the tightened defaults match our analysis."""

    def test_min_books_raised_to_4(self):
        self.assertEqual(DEFAULTS["min_books"], 4)

    def test_min_edge_lowered_to_4pct(self):
        self.assertAlmostEqual(DEFAULTS["min_edge_pts"], 0.04)

    def test_min_minutes_lowered_to_15(self):
        self.assertEqual(DEFAULTS["min_minutes_before_start"], 15)

    def test_only_basketball_sports(self):
        sports = DEFAULTS["sport_keys"].split(",")
        self.assertEqual(set(sports), {"basketball_nba", "basketball_ncaab"})
        self.assertNotIn("icehockey_nhl", sports)
        self.assertNotIn("baseball_mlb", sports)
        self.assertNotIn("americanfootball_nfl", sports)
