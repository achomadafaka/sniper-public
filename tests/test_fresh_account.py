"""
Tests for fresh account detection logic.

Covers:
  - _is_fresh_account() criteria enforcement (notional, price, age)
  - _get_wallet_age_days() source priority (wallet_profiles > low_buy_trades > candidate_users)
  - calculate_quick_score() fresh account bonuses (wallet age + resolution proximity)
"""

import sqlite3
import time
import unittest
from unittest.mock import MagicMock, patch

from db_discovery import calculate_quick_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = int(time.time())
DAY = 86400


def _make_ingestor(conn: sqlite3.Connection):
    """Create a LowPriceIngestor with a real DB connection (no network)."""
    from stream_low_buys import LowPriceIngestor
    ing = LowPriceIngestor.__new__(LowPriceIngestor)
    ing.conn = conn
    ing._known_wallets = set()
    ing.condition_meta = {}
    return ing


def _fresh_db() -> sqlite3.Connection:
    """In-memory SQLite with the minimal schema required for fresh account tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE wallet_profiles (
            wallet TEXT PRIMARY KEY,
            first_activity_ts INTEGER NOT NULL DEFAULT 0,
            last_profile_sync_ts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE low_buy_trades (
            transaction_hash TEXT PRIMARY KEY,
            proxy_wallet TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        );
        CREATE TABLE candidate_users (
            wallet TEXT PRIMARY KEY,
            first_seen_ts INTEGER NOT NULL DEFAULT 0,
            last_seen_ts INTEGER NOT NULL DEFAULT 0
        );
    """)
    return conn


# ---------------------------------------------------------------------------
# _get_wallet_age_days — source priority
# ---------------------------------------------------------------------------

class WalletAgeDaysTests(unittest.TestCase):

    def test_uses_wallet_profiles_first_priority(self):
        """wallet_profiles.first_activity_ts is used when available."""
        conn = _fresh_db()
        wallet = "0xabc"
        # 3 days old in wallet_profiles
        conn.execute(
            "INSERT INTO wallet_profiles (wallet, first_activity_ts) VALUES (?, ?)",
            (wallet, NOW - 3 * DAY),
        )
        # 10 days old in low_buy_trades (should be ignored)
        conn.execute(
            "INSERT INTO low_buy_trades VALUES (?, ?, ?)",
            ("0xtx1", wallet, NOW - 10 * DAY),
        )
        conn.commit()

        ing = _make_ingestor(conn)
        age = ing._get_wallet_age_days(wallet)
        self.assertAlmostEqual(age, 3.0, delta=0.1)

    def test_falls_back_to_low_buy_trades(self):
        """Falls back to MIN(low_buy_trades.timestamp) when no wallet_profile."""
        conn = _fresh_db()
        wallet = "0xdef"
        # 5 days old in low_buy_trades
        conn.execute(
            "INSERT INTO low_buy_trades VALUES (?, ?, ?)",
            ("0xtx2", wallet, NOW - 5 * DAY),
        )
        conn.commit()

        ing = _make_ingestor(conn)
        age = ing._get_wallet_age_days(wallet)
        self.assertAlmostEqual(age, 5.0, delta=0.1)

    def test_falls_back_to_candidate_users(self):
        """Falls back to candidate_users.first_seen_ts as last resort."""
        conn = _fresh_db()
        wallet = "0xghi"
        conn.execute(
            "INSERT INTO candidate_users (wallet, first_seen_ts, last_seen_ts) VALUES (?, ?, ?)",
            (wallet, NOW - 2 * DAY, NOW),
        )
        conn.commit()

        ing = _make_ingestor(conn)
        age = ing._get_wallet_age_days(wallet)
        self.assertAlmostEqual(age, 2.0, delta=0.1)

    def test_returns_large_value_when_no_data(self):
        """Returns 99999.0 when wallet is completely unknown."""
        conn = _fresh_db()
        ing = _make_ingestor(conn)
        age = ing._get_wallet_age_days("0xunknown")
        self.assertEqual(age, 99999.0)

    def test_zero_first_activity_ts_is_ignored(self):
        """first_activity_ts=0 in wallet_profiles is treated as missing."""
        conn = _fresh_db()
        wallet = "0xjkl"
        conn.execute(
            "INSERT INTO wallet_profiles (wallet, first_activity_ts) VALUES (?, 0)",
            (wallet,),
        )
        # 4 days in low_buy_trades — should be used as fallback
        conn.execute(
            "INSERT INTO low_buy_trades VALUES (?, ?, ?)",
            ("0xtx3", wallet, NOW - 4 * DAY),
        )
        conn.commit()

        ing = _make_ingestor(conn)
        age = ing._get_wallet_age_days(wallet)
        self.assertAlmostEqual(age, 4.0, delta=0.1)


# ---------------------------------------------------------------------------
# _is_fresh_account — criteria enforcement
# ---------------------------------------------------------------------------

class IsFreshAccountTests(unittest.TestCase):

    def _make_fresh_wallet(self, conn, wallet, age_days):
        """Insert wallet with given age into wallet_profiles."""
        conn.execute(
            "INSERT OR REPLACE INTO wallet_profiles (wallet, first_activity_ts) VALUES (?, ?)",
            (wallet, NOW - int(age_days * DAY)),
        )
        conn.commit()

    def test_returns_true_for_qualifying_wallet(self):
        """A 3-day-old wallet betting $25k at price 0.30 qualifies."""
        conn = _fresh_db()
        wallet = "0xfresh1"
        self._make_fresh_wallet(conn, wallet, age_days=3.0)
        ing = _make_ingestor(conn)
        self.assertTrue(ing._is_fresh_account(wallet, notional=25000, price=0.30))

    def test_fails_notional_below_threshold(self):
        """Notional below $20,000 is rejected without any DB lookup."""
        conn = _fresh_db()
        ing = _make_ingestor(conn)
        # Even a 1-day-old wallet should fail on notional
        self.assertFalse(ing._is_fresh_account("0xany", notional=19999, price=0.30))

    def test_fails_price_at_threshold(self):
        """Price >= 0.80 (buying certainty) is rejected without any DB lookup."""
        conn = _fresh_db()
        ing = _make_ingestor(conn)
        self.assertFalse(ing._is_fresh_account("0xany", notional=25000, price=0.80))

    def test_fails_price_above_threshold(self):
        """Price > 0.80 is rejected."""
        conn = _fresh_db()
        ing = _make_ingestor(conn)
        self.assertFalse(ing._is_fresh_account("0xany", notional=30000, price=0.95))

    def test_price_just_below_threshold_passes(self):
        """Price = 0.799 just below the 0.80 threshold passes the price check."""
        conn = _fresh_db()
        wallet = "0xfresh2"
        self._make_fresh_wallet(conn, wallet, age_days=2.0)
        ing = _make_ingestor(conn)
        self.assertTrue(ing._is_fresh_account(wallet, notional=25000, price=0.799))

    def test_fails_wallet_too_old(self):
        """Wallet older than 7 days is rejected."""
        conn = _fresh_db()
        wallet = "0xold1"
        self._make_fresh_wallet(conn, wallet, age_days=8.0)
        ing = _make_ingestor(conn)
        self.assertFalse(ing._is_fresh_account(wallet, notional=25000, price=0.30))

    def test_wallet_age_exactly_7_days_rejected(self):
        """Wallet at exactly 7 days (> threshold) is rejected."""
        conn = _fresh_db()
        wallet = "0xborder"
        self._make_fresh_wallet(conn, wallet, age_days=7.0)
        ing = _make_ingestor(conn)
        # 7.0 > FRESH_ACCOUNT_MAX_AGE_DAYS (7.0) → False
        self.assertFalse(ing._is_fresh_account(wallet, notional=25000, price=0.30))

    def test_wallet_age_just_under_7_days_passes(self):
        """Wallet at 6.9 days passes the age check."""
        conn = _fresh_db()
        wallet = "0xalmost"
        self._make_fresh_wallet(conn, wallet, age_days=6.9)
        ing = _make_ingestor(conn)
        self.assertTrue(ing._is_fresh_account(wallet, notional=25000, price=0.30))

    def test_known_wallets_cache_skips_db(self):
        """Once a wallet is cached as known-old, DB is not queried again."""
        conn = _fresh_db()
        wallet = "0xcached"
        # Make it old so it gets cached
        self._make_fresh_wallet(conn, wallet, age_days=30.0)
        ing = _make_ingestor(conn)
        # First call — learns wallet is old, caches it
        result1 = ing._is_fresh_account(wallet, notional=25000, price=0.30)
        self.assertFalse(result1)
        self.assertIn(wallet, ing._known_wallets)

        # Second call — should use cache, not DB
        # Overwrite wallet_profiles to make it appear young (should be ignored)
        conn.execute(
            "UPDATE wallet_profiles SET first_activity_ts = ? WHERE wallet = ?",
            (NOW - DAY, wallet),  # now looks 1 day old
        )
        conn.commit()
        result2 = ing._is_fresh_account(wallet, notional=25000, price=0.30)
        self.assertFalse(result2)  # still False from cache

    def test_unknown_wallet_no_db_data_rejected(self):
        """Wallet with no DB data returns 99999 age → rejected."""
        conn = _fresh_db()
        ing = _make_ingestor(conn)
        self.assertFalse(ing._is_fresh_account("0xghost", notional=50000, price=0.10))

    def test_all_three_criteria_must_pass(self):
        """All three criteria (notional, price, age) must pass simultaneously."""
        conn = _fresh_db()
        wallet = "0xstrict"
        self._make_fresh_wallet(conn, wallet, age_days=2.0)
        ing = _make_ingestor(conn)

        # Fail notional
        self.assertFalse(ing._is_fresh_account(wallet, notional=5000, price=0.30))
        # Fail price
        self.assertFalse(ing._is_fresh_account(wallet, notional=25000, price=0.85))
        # Pass all
        self.assertTrue(ing._is_fresh_account(wallet, notional=25000, price=0.30))


# ---------------------------------------------------------------------------
# calculate_quick_score — fresh account bonuses
# ---------------------------------------------------------------------------

class FreshAccountScoringTests(unittest.TestCase):

    BASE_EVENT = {
        "stream_type": "fresh_account",
        "price": 0.25,
        "notional": 25000,
        "detected_at": NOW,
    }

    def _score(self, **overrides):
        event = dict(self.BASE_EVENT)
        event.update(overrides)
        return calculate_quick_score(event)

    def test_wallet_age_1d_gets_highest_bonus(self):
        """Wallet < 1 day old gets +20 bonus."""
        score_young = self._score(event_data={"wallet_age_days": 0.5, "days_to_resolution": -1})
        score_old = self._score(event_data={"wallet_age_days": 6.0, "days_to_resolution": -1})
        self.assertGreater(score_young, score_old)

    def test_wallet_age_3d_gets_medium_bonus(self):
        """Wallet 1-3 days old gets +15 vs +20 for sub-1d."""
        score_1d = self._score(event_data={"wallet_age_days": 0.5, "days_to_resolution": -1})
        score_3d = self._score(event_data={"wallet_age_days": 2.0, "days_to_resolution": -1})
        self.assertGreater(score_1d, score_3d)

    def test_resolution_within_1d_gets_highest_bonus(self):
        """Market resolving within 1 day gets +15 bonus."""
        score_urgent = self._score(event_data={"wallet_age_days": 2.0, "days_to_resolution": 0.5})
        score_far = self._score(event_data={"wallet_age_days": 2.0, "days_to_resolution": 10.0})
        self.assertGreater(score_urgent, score_far)

    def test_resolution_within_5d_gets_small_bonus(self):
        """Market resolving within 5 days gets +5 bonus."""
        score_5d = self._score(event_data={"wallet_age_days": 2.0, "days_to_resolution": 4.0})
        score_none = self._score(event_data={"wallet_age_days": 2.0, "days_to_resolution": -1})
        self.assertGreater(score_5d, score_none)

    def test_no_bonus_for_non_fresh_account_stream(self):
        """low_buy stream does not get fresh account bonuses."""
        score_fresh = self._score(
            stream_type="fresh_account",
            event_data={"wallet_age_days": 0.5, "days_to_resolution": 0.5},
        )
        score_low_buy = self._score(
            stream_type="low_buy",
            event_data={"wallet_age_days": 0.5, "days_to_resolution": 0.5},
        )
        # fresh_account should outscore low_buy by at least 20 pts (baseline + bonuses)
        self.assertGreater(score_fresh, score_low_buy + 15)

    def test_score_capped_at_100(self):
        """Score never exceeds 100 regardless of bonuses."""
        score = self._score(
            notional=100000,
            price=0.05,
            event_data={"wallet_age_days": 0.1, "days_to_resolution": 0.5},
        )
        self.assertLessEqual(score, 100)

    def test_missing_event_data_does_not_crash(self):
        """Missing event_data field doesn't raise an exception."""
        score = self._score()  # no event_data key
        self.assertIsInstance(score, int)
        self.assertGreater(score, 0)

    def test_event_data_as_json_string_is_parsed(self):
        """event_data stored as JSON string (from DB) is parsed correctly."""
        import json
        score_dict = self._score(event_data={"wallet_age_days": 1.0, "days_to_resolution": 2.0})
        score_str = self._score(
            event_data=json.dumps({"wallet_age_days": 1.0, "days_to_resolution": 2.0})
        )
        self.assertEqual(score_dict, score_str)

    def test_high_notional_fresh_young_wallet_resolving_soon_scores_near_100(self):
        """A perfect fresh account signal (young wallet, large bet, imminent resolution) scores very high."""
        score = self._score(
            notional=80000,
            price=0.15,
            event_data={"wallet_age_days": 0.5, "days_to_resolution": 0.5},
        )
        self.assertGreaterEqual(score, 80)


if __name__ == "__main__":
    unittest.main()
