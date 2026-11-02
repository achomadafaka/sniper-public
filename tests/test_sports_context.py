"""Tests for sports context query helpers."""
from __future__ import annotations

import sqlite3
import time
import unittest
from datetime import datetime, timezone, timedelta

from db import init_db, ensure_odds_snapshots_table


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
    volume_24h: float = 1000.0,
    end_date: str = "2026-04-01T00:00:00Z",
) -> None:
    """Insert a row into market_metadata for testing."""
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, is_open, volume_24h, end_date_iso,
            last_synced_ts)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (condition_id, title, is_open, volume_24h, end_date, int(time.time())),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Task 3: Polymarket h2h matching helpers
# ---------------------------------------------------------------------------

class TestFindMatchingPolymarketH2H(unittest.TestCase):
    """find_matching_polymarket_h2h_markets tests."""

    def test_exact_team_match(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_1", "Boston Celtics vs. Milwaukee Bucks",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition_id"], "cid_1")

    def test_reversed_team_order_still_matches(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_2", "Milwaukee Bucks vs. Boston Celtics",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["condition_id"], "cid_2")

    def test_skips_spread_markets(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_spread",
            "Boston Celtics -3.5 spread vs. Milwaukee Bucks",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 0)

    def test_skips_over_under(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_ou",
            "Celtics vs Bucks over/under 220.5",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 0)

    def test_skips_series_winner(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_series",
            "Celtics vs Bucks series winner",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 0)

    def test_skips_closed_market(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_closed",
            "Boston Celtics vs. Milwaukee Bucks",
            is_open=0,
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 0)

    def test_skips_team_total(self):
        from db import find_matching_polymarket_h2h_markets

        conn = _in_memory_conn()
        _seed_market(
            conn, "cid_tt",
            "Celtics vs Bucks team total over 220.5",
            end_date="2026-04-01T20:00:00Z",
        )

        results = find_matching_polymarket_h2h_markets(
            conn,
            home_team="Boston Celtics",
            away_team="Milwaukee Bucks",
            commence_time="2026-04-01T19:00:00Z",
        )
        self.assertEqual(len(results), 0)


class TestPolymarketMarketOutcomes(unittest.TestCase):
    """polymarket_market_outcomes tests."""

    def test_outcomes_from_market_outcomes_table(self):
        from db import polymarket_market_outcomes

        conn = _in_memory_conn()
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome, resolved_price) "
            "VALUES (?, ?, ?)",
            ("cid_out1", "Boston Celtics", 0.0),
        )
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome, resolved_price) "
            "VALUES (?, ?, ?)",
            ("cid_out1", "Milwaukee Bucks", 0.0),
        )
        conn.commit()

        results = polymarket_market_outcomes(conn, "cid_out1")
        outcomes = {r["outcome"] for r in results}
        self.assertEqual(outcomes, {"Boston Celtics", "Milwaukee Bucks"})

    def test_outcomes_fallback_to_position_signals(self):
        from db import polymarket_market_outcomes

        conn = _in_memory_conn()
        # No market_outcomes row — insert into position_signals instead
        conn.execute(
            "INSERT INTO position_signals "
            "(wallet, condition_id, outcome, title) "
            "VALUES (?, ?, ?, ?)",
            ("0xabc", "cid_out2", "Team Alpha", "Some market"),
        )
        conn.execute(
            "INSERT INTO position_signals "
            "(wallet, condition_id, outcome, title) "
            "VALUES (?, ?, ?, ?)",
            ("0xdef", "cid_out2", "Team Beta", "Some market"),
        )
        conn.commit()

        results = polymarket_market_outcomes(conn, "cid_out2")
        outcomes = {r["outcome"] for r in results}
        self.assertEqual(outcomes, {"Team Alpha", "Team Beta"})

    def test_outcomes_empty_when_no_data_and_clob_fails(self):
        from unittest.mock import patch
        from db import polymarket_market_outcomes

        conn = _in_memory_conn()
        with patch("db._fetch_clob_outcomes", return_value=[]):
            results = polymarket_market_outcomes(conn, "cid_nonexistent")
        self.assertEqual(results, [])

    def test_outcomes_clob_fallback(self):
        """CLOB API fallback fires when DB tables have no outcomes."""
        from unittest.mock import patch
        from db import polymarket_market_outcomes

        conn = _in_memory_conn()
        clob_outcomes = [{"outcome": "Team A"}, {"outcome": "Team B"}]
        with patch("db._fetch_clob_outcomes", return_value=clob_outcomes):
            results = polymarket_market_outcomes(conn, "cid_sports_game")
        outcomes = {r["outcome"] for r in results}
        self.assertEqual(outcomes, {"Team A", "Team B"})

    def test_clob_fallback_not_called_when_db_has_data(self):
        """CLOB API should NOT be called if market_outcomes has data."""
        from unittest.mock import patch
        from db import polymarket_market_outcomes

        conn = _in_memory_conn()
        conn.execute(
            "INSERT INTO market_outcomes (condition_id, outcome, resolved_price) "
            "VALUES (?, ?, ?)",
            ("cid_db_has", "Yes", 0.0),
        )
        conn.commit()

        with patch("db._fetch_clob_outcomes") as mock_clob:
            results = polymarket_market_outcomes(conn, "cid_db_has")
        mock_clob.assert_not_called()
        self.assertEqual(len(results), 1)


class TestFetchClobOutcomes(unittest.TestCase):
    """Unit tests for _fetch_clob_outcomes."""

    def test_parses_tokens_response(self):
        from unittest.mock import patch, MagicMock
        from db import _fetch_clob_outcomes

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "Boston Celtics", "price": "0.65"},
                {"outcome": "Milwaukee Bucks", "price": "0.35"},
            ]
        }
        with patch("requests.get", return_value=mock_resp):
            results = _fetch_clob_outcomes("cid_test")
        outcomes = {r["outcome"] for r in results}
        self.assertEqual(outcomes, {"Boston Celtics", "Milwaukee Bucks"})
        # Check prices are included
        price_map = {r["outcome"]: r["price"] for r in results}
        self.assertAlmostEqual(price_map["Boston Celtics"], 0.65)
        self.assertAlmostEqual(price_map["Milwaukee Bucks"], 0.35)

    def test_returns_empty_on_network_error(self):
        from unittest.mock import patch
        from db import _fetch_clob_outcomes

        with patch("requests.get", side_effect=Exception("timeout")):
            results = _fetch_clob_outcomes("cid_fail")
        self.assertEqual(results, [])

    def test_skips_empty_outcome_labels(self):
        from unittest.mock import patch, MagicMock
        from db import _fetch_clob_outcomes

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "Team A", "price": "0.50"},
                {"outcome": "", "price": "0.50"},
                {"price": "0.50"},
            ]
        }
        with patch("requests.get", return_value=mock_resp):
            results = _fetch_clob_outcomes("cid_partial")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "Team A")


# ---------------------------------------------------------------------------
# Task 4: Sports context module imports
# ---------------------------------------------------------------------------

class TestSportsContextModule(unittest.TestCase):
    """Verify re-exports from context.sports_context and context.__init__."""

    def test_import_from_context_sports_context(self):
        from sniper.context.sports_context import (
            find_matching_polymarket_h2h_markets,
            insider_convergence_check,
            odds_event_history,
            polymarket_market_outcomes,
            recent_odds_events,
        )
        # Verify all are callable
        self.assertTrue(callable(find_matching_polymarket_h2h_markets))
        self.assertTrue(callable(insider_convergence_check))
        self.assertTrue(callable(odds_event_history))
        self.assertTrue(callable(polymarket_market_outcomes))
        self.assertTrue(callable(recent_odds_events))

    def test_import_from_context_init(self):
        from sniper.context import (
            find_matching_polymarket_h2h_markets,
            odds_event_history,
            polymarket_market_outcomes,
            recent_odds_events,
        )
        self.assertTrue(callable(find_matching_polymarket_h2h_markets))
        self.assertTrue(callable(odds_event_history))
        self.assertTrue(callable(polymarket_market_outcomes))
        self.assertTrue(callable(recent_odds_events))


if __name__ == "__main__":
    unittest.main()
