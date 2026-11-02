import sqlite3
import unittest

import pytest

from db import init_db
from db_discovery import calculate_quick_score, init_discovery_schema, record_discovery_event
from stream_low_buys import FRESH_ACCOUNT_MAX_AGE_DAYS, FRESH_ACCOUNT_MIN_NOTIONAL, FRESH_ACCOUNT_MAX_ENTRY_PRICE, LowPriceIngestor


@pytest.fixture(autouse=True)
def _allow_legacy_discovery_writes(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)


class DiscoveryScoringTests(unittest.TestCase):
    def test_large_bet_resolved_market_requires_override_notional(self) -> None:
        low_notional = calculate_quick_score(
            {
                "stream_type": "large_bet",
                "price": 0.97,
                "notional": 40000,
                "detected_at": 2_000_000_000,
            }
        )
        high_notional = calculate_quick_score(
            {
                "stream_type": "large_bet",
                "price": 0.97,
                "notional": 80000,
                "detected_at": 2_000_000_000,
            }
        )
        self.assertEqual(low_notional, 0)
        self.assertGreater(high_notional, 0)

    def test_trigger_decision_large_bet_paths(self) -> None:
        is_interesting, trigger_type, _reason = LowPriceIngestor._trigger_decision(
            price=0.62,
            notional=15000,
            impact_ratio=0.01,
        )
        self.assertTrue(is_interesting)
        self.assertEqual(trigger_type, "large_bet")

        # Resolved-market large bet below override threshold should be ignored.
        is_interesting, trigger_type, _reason = LowPriceIngestor._trigger_decision(
            price=0.98,
            notional=15000,
            impact_ratio=0.01,
        )
        self.assertFalse(is_interesting)
        self.assertEqual(trigger_type, "")

        # Resolved-market large bet above override threshold should pass.
        is_interesting, trigger_type, _reason = LowPriceIngestor._trigger_decision(
            price=0.98,
            notional=60000,
            impact_ratio=0.01,
        )
        self.assertTrue(is_interesting)
        self.assertEqual(trigger_type, "large_bet")


class DiscoveryDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        init_discovery_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_record_discovery_event_dedupes_on_fingerprint_when_tx_missing(self) -> None:
        kwargs = {
            "wallet": "0x1111111111111111111111111111111111111111",
            "stream_type": "large_bet",
            "detected_at": 1_700_000_000,
            "source_tx_hash": "",
            "condition_id": "0xcond1",
            "outcome": "yes",
            "side": "BUY",
            "price": 0.42,
            "notional": 25000,
            "trigger_reason": "large bet",
        }
        first_id = record_discovery_event(self.conn, **kwargs)
        second_id = record_discovery_event(self.conn, **kwargs)
        self.assertIsNotNone(first_id)
        self.assertIsNone(second_id)

        count = self.conn.execute("SELECT COUNT(*) FROM discovery_events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_record_discovery_event_dedupes_on_tx_hash_when_present(self) -> None:
        first_id = record_discovery_event(
            self.conn,
            wallet="0x2222222222222222222222222222222222222222",
            stream_type="low_buy",
            detected_at=1_700_000_001,
            source_tx_hash="0xtxsame",
            condition_id="0xcond2",
            outcome="yes",
            side="BUY",
            price=0.14,
            notional=9000,
            trigger_reason="low-buy",
        )
        second_id = record_discovery_event(
            self.conn,
            wallet="0x2222222222222222222222222222222222222222",
            stream_type="low_buy",
            detected_at=1_700_000_002,
            source_tx_hash="0xtxsame",
            condition_id="0xcond2",
            outcome="yes",
            side="BUY",
            price=0.15,
            notional=9100,
            trigger_reason="low-buy",
        )
        self.assertIsNotNone(first_id)
        self.assertIsNone(second_id)

        count = self.conn.execute("SELECT COUNT(*) FROM discovery_events").fetchone()[0]
        self.assertEqual(count, 1)


class DiscoverySchemaMigrationTests(unittest.TestCase):
    def test_init_discovery_schema_upgrades_legacy_tables_before_index_creation(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            # Legacy discovery_events (missing event_fingerprint).
            conn.execute(
                """
                CREATE TABLE discovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet TEXT NOT NULL,
                    stream_type TEXT NOT NULL,
                    detected_at INTEGER NOT NULL,
                    source_tx_hash TEXT NOT NULL DEFAULT '',
                    condition_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    price REAL,
                    notional REAL,
                    trigger_reason TEXT NOT NULL DEFAULT '',
                    event_data_json TEXT NOT NULL DEFAULT '{}',
                    quick_score INTEGER NOT NULL DEFAULT 0,
                    score_version INTEGER NOT NULL DEFAULT 0,
                    scored_at INTEGER NOT NULL DEFAULT 0,
                    analysis_status TEXT NOT NULL DEFAULT 'new',
                    analysis_version INTEGER NOT NULL DEFAULT 0,
                    analyzed_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            # Legacy wallet_analysis_queue (missing retry/lease columns).
            conn.execute(
                """
                CREATE TABLE wallet_analysis_queue (
                    wallet TEXT PRIMARY KEY,
                    priority_score INTEGER NOT NULL,
                    first_event_ts INTEGER NOT NULL,
                    last_event_ts INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    locked_by TEXT NOT NULL DEFAULT '',
                    locked_ts INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

            # Should not fail due to index creation order.
            init_discovery_schema(conn)

            discovery_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(discovery_events)").fetchall()
            }
            queue_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(wallet_analysis_queue)").fetchall()
            }

            self.assertIn("event_fingerprint", discovery_cols)
            self.assertIn("next_retry_ts", queue_cols)
            self.assertIn("lease_expires_ts", queue_cols)
            self.assertIn("last_error", queue_cols)
            self.assertIn("last_analysis_version", queue_cols)
            self.assertIn("updated_ts", queue_cols)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

class FreshAccountStreamTests(unittest.TestCase):
    """Tests for the fresh_account discovery stream in LowPriceIngestor."""

    def _make_ingestor_with_db(self, db_path=":memory:"):
        """Create an ingestor wired to an in-memory SQLite DB."""
        import os
        os.environ.setdefault("ANALYSIS_SOURCE", "legacy")
        ingestor = object.__new__(LowPriceIngestor)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        init_discovery_schema(conn, commit=True)
        ingestor.conn = conn
        ingestor._known_wallets = set()
        return ingestor

    def _insert_wallet_profile(self, ingestor, wallet, first_activity_ts):
        """Helper: insert a wallet_profiles row with a given first_activity_ts."""
        ingestor.conn.execute(
            "INSERT OR REPLACE INTO wallet_profiles (wallet, first_activity_ts, last_profile_sync_ts) "
            "VALUES (?, ?, ?)",
            (wallet, first_activity_ts, first_activity_ts),
        )
        ingestor.conn.commit()

    def test_fresh_account_detected_young_wallet_large_bet(self):
        """A wallet < 7 days old with notional >= $20k and price < 0.80 qualifies."""
        import time
        ingestor = self._make_ingestor_with_db()
        wallet = "0xdeadbeef00000000000000000000000000000001"
        # 2 days old
        self._insert_wallet_profile(ingestor, wallet, int(time.time()) - 2 * 86400)
        result = ingestor._is_fresh_account(
            wallet, FRESH_ACCOUNT_MIN_NOTIONAL, price=0.30
        )
        self.assertTrue(result, "Young wallet with qualifying notional+price should be fresh")

    def test_fresh_account_rejected_below_notional_threshold(self):
        """Notional below $20,000 is rejected immediately without DB lookup."""
        ingestor = self._make_ingestor_with_db()
        wallet = "0xdeadbeef00000000000000000000000000000002"
        result = ingestor._is_fresh_account(
            wallet, FRESH_ACCOUNT_MIN_NOTIONAL - 1, price=0.30
        )
        self.assertFalse(result, "Notional below threshold should not trigger fresh_account")

    def test_fresh_account_rejected_when_price_at_threshold(self):
        """Price >= 0.80 (buying certainty) is rejected without DB lookup."""
        ingestor = self._make_ingestor_with_db()
        wallet = "0xdeadbeef00000000000000000000000000000003"
        result = ingestor._is_fresh_account(
            wallet, FRESH_ACCOUNT_MIN_NOTIONAL, price=FRESH_ACCOUNT_MAX_ENTRY_PRICE
        )
        self.assertFalse(result, "Price at/above 0.80 threshold should not qualify")

    def test_fresh_account_rejected_when_wallet_too_old(self):
        """A wallet older than 7 days is NOT fresh regardless of bet size."""
        import time
        ingestor = self._make_ingestor_with_db()
        wallet = "0xdeadbeef00000000000000000000000000000004"
        # 10 days old
        self._insert_wallet_profile(ingestor, wallet, int(time.time()) - 10 * 86400)
        result = ingestor._is_fresh_account(
            wallet, FRESH_ACCOUNT_MIN_NOTIONAL, price=0.20
        )
        self.assertFalse(result, "Wallet older than 7 days should NOT be fresh")

    def test_fresh_account_known_wallet_cache_skips_db(self):
        """Once a wallet is in _known_wallets, DB is not queried again."""
        ingestor = self._make_ingestor_with_db()
        wallet = "0xdeadbeef00000000000000000000000000000005"
        ingestor._known_wallets.add(wallet)
        # Even with a huge notional and low price, cached wallet returns False
        result = ingestor._is_fresh_account(wallet, 999999.0, price=0.10)
        self.assertFalse(result, "Cached known-old wallet should be skipped without DB hit")

    def test_fresh_account_quick_score_is_nonzero(self):
        """fresh_account stream events get a meaningful quick_score."""
        score = calculate_quick_score({
            "stream_type": "fresh_account",
            "price": 0.25,
            "notional": 7500,
            "detected_at": 2_000_000_000,
        })
        self.assertGreater(score, 0, "fresh_account events should have positive quick_score")

    def test_fresh_account_and_large_bet_quick_score_ordering(self):
        """large_bet should outscore fresh_account at same notional (higher baseline weight)."""
        fresh_score = calculate_quick_score({
            "stream_type": "fresh_account",
            "price": 0.25,
            "notional": 15000,
            "detected_at": 2_000_000_000,
        })
        large_bet_score = calculate_quick_score({
            "stream_type": "large_bet",
            "price": 0.25,
            "notional": 15000,
            "detected_at": 2_000_000_000,
        })
        self.assertGreater(large_bet_score, fresh_score,
                           "large_bet has higher baseline weight than fresh_account")


if __name__ == '__main__':
    unittest.main()
