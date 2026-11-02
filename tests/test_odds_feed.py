"""Tests for odds_snapshots DB helpers and OddsFeedAdapter.

TDD: tests written before implementation.
"""
from __future__ import annotations

import io
import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.error
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from db import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_odds_snapshot(
    conn,
    *,
    instance_tag: str = "test",
    provider: str = "the_odds_api",
    sport_key: str = "basketball_nba",
    sport_title: str = "NBA",
    event_id: str = "evt_001",
    home_team: str = "Lakers",
    away_team: str = "Celtics",
    commence_time: str | None = None,
    bookmaker_key: str = "draftkings",
    bookmaker_title: str = "DraftKings",
    market_key: str = "h2h",
    outcome_name: str = "Lakers",
    price_decimal: float = 1.91,
    implied_prob_raw: float = 0.5236,
    market_ts: str | None = None,
    canonical_event_hash: str | None = None,
    payload_json: str = "{}",
) -> int | None:
    """Seed a single odds_snapshots row.  Returns rowid or None."""
    from db import ensure_odds_snapshots_table, insert_odds_snapshot

    ensure_odds_snapshots_table(conn)

    if commence_time is None:
        commence_time = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if market_ts is None:
        market_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if canonical_event_hash is None:
        raw = f"{home_team.lower().strip()}|{away_team.lower().strip()}|{commence_time}"
        canonical_event_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

    return insert_odds_snapshot(
        conn,
        instance_tag=instance_tag,
        provider=provider,
        sport_key=sport_key,
        sport_title=sport_title,
        event_id=event_id,
        home_team=home_team,
        away_team=away_team,
        commence_time=commence_time,
        bookmaker_key=bookmaker_key,
        bookmaker_title=bookmaker_title,
        market_key=market_key,
        outcome_name=outcome_name,
        price_decimal=price_decimal,
        implied_prob_raw=implied_prob_raw,
        market_ts=market_ts,
        canonical_event_hash=canonical_event_hash,
        payload_json=payload_json,
    )


# ---------------------------------------------------------------------------
# Task 1: DB helpers
# ---------------------------------------------------------------------------

class TestOddsSnapshotsTable(unittest.TestCase):
    """ensure_odds_snapshots_table + insert + dedup."""

    def test_ensure_creates_table(self):
        from db import ensure_odds_snapshots_table

        conn = _in_memory_conn()
        ensure_odds_snapshots_table(conn)

        # Table should exist
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='odds_snapshots'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_insert_snapshot_returns_rowid(self):
        conn = _in_memory_conn()
        rowid = _seed_odds_snapshot(conn)
        self.assertIsNotNone(rowid)
        self.assertGreater(rowid, 0)

    def test_insert_dedup_same_snapshot(self):
        conn = _in_memory_conn()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        kwargs = dict(
            bookmaker_key="draftkings",
            market_key="h2h",
            market_ts=ts,
            provider="the_odds_api",
            event_id="evt_dup",
        )
        rowid1 = _seed_odds_snapshot(conn, **kwargs)
        rowid2 = _seed_odds_snapshot(conn, **kwargs)
        self.assertIsNotNone(rowid1)
        self.assertIsNone(rowid2)  # duplicate → None


class TestOddsQueryHelpers(unittest.TestCase):
    """recent_odds_events and odds_event_history."""

    def test_recent_odds_events_returns_latest_per_event(self):
        from db import ensure_odds_snapshots_table, recent_odds_events

        conn = _in_memory_conn()
        ensure_odds_snapshots_table(conn)

        # Two snapshots for same event at different market_ts
        ts1 = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ts2 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence = (datetime.now(timezone.utc) + timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _seed_odds_snapshot(
            conn, event_id="evt_a", market_ts=ts1, commence_time=commence,
            bookmaker_key="dk", outcome_name="Lakers",
        )
        _seed_odds_snapshot(
            conn, event_id="evt_a", market_ts=ts2, commence_time=commence,
            bookmaker_key="dk", outcome_name="Lakers",
        )

        results = recent_odds_events(conn, ["basketball_nba"])
        # Should get the latest snapshot
        self.assertGreater(len(results), 0)
        # Market_ts of the returned row should be the newer one
        self.assertEqual(results[0]["market_ts"], ts2)

    def test_recent_odds_events_filters_by_sport(self):
        from db import ensure_odds_snapshots_table, recent_odds_events

        conn = _in_memory_conn()
        ensure_odds_snapshots_table(conn)

        _seed_odds_snapshot(conn, sport_key="basketball_nba", event_id="evt_nba")
        _seed_odds_snapshot(
            conn, sport_key="americanfootball_nfl", event_id="evt_nfl",
            bookmaker_key="fanduel",
        )

        nba_results = recent_odds_events(conn, ["basketball_nba"])
        nfl_results = recent_odds_events(conn, ["americanfootball_nfl"])

        nba_event_ids = {r["event_id"] for r in nba_results}
        nfl_event_ids = {r["event_id"] for r in nfl_results}

        self.assertIn("evt_nba", nba_event_ids)
        self.assertNotIn("evt_nfl", nba_event_ids)
        self.assertIn("evt_nfl", nfl_event_ids)
        self.assertNotIn("evt_nba", nfl_event_ids)

    def test_odds_event_history_returns_chronological(self):
        from db import ensure_odds_snapshots_table, odds_event_history

        conn = _in_memory_conn()
        ensure_odds_snapshots_table(conn)

        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        raw = f"lakers|celtics|{commence}"
        ev_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

        ts_old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ts_mid = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        ts_new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for i, ts in enumerate([ts_new, ts_old, ts_mid]):  # insert out of order
            _seed_odds_snapshot(
                conn,
                canonical_event_hash=ev_hash,
                market_ts=ts,
                bookmaker_key=f"bk_{i}",
                commence_time=commence,
            )

        rows = odds_event_history(conn, ev_hash, hours=24)
        self.assertEqual(len(rows), 3)
        # Should be chronological: ts_old, ts_mid, ts_new
        self.assertEqual(rows[0]["market_ts"], ts_old)
        self.assertEqual(rows[1]["market_ts"], ts_mid)
        self.assertEqual(rows[2]["market_ts"], ts_new)


# ---------------------------------------------------------------------------
# Task 2: OddsFeedAdapter
# ---------------------------------------------------------------------------

class TestOddsFeedAdapter(unittest.TestCase):
    """OddsFeedAdapter metadata, config, and tick behaviour."""

    def test_feed_metadata(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        self.assertEqual(adapter.feed_id, "odds")
        self.assertEqual(adapter.display_name, "Sports Odds")
        self.assertEqual(adapter.default_poll_seconds, 1800)
        self.assertEqual(adapter.mode, "poll")

    def test_validate_config_missing_key(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(adapter.validate_config())

    def test_validate_config_with_key(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        with patch.dict("os.environ", {"ODDS_API_KEY": "test-key-123"}):
            self.assertTrue(adapter.validate_config())

    def test_warmup_creates_table(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)

        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='odds_snapshots'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_canonical_event_hash_deterministic(self):
        from sniper.feeds.odds import canonical_event_hash

        h1 = canonical_event_hash("Lakers", "Celtics", "2026-04-01T19:00:00Z")
        h2 = canonical_event_hash("Lakers", "Celtics", "2026-04-01T19:00:00Z")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_canonical_event_hash_case_insensitive(self):
        from sniper.feeds.odds import canonical_event_hash

        h1 = canonical_event_hash("Lakers", "Celtics", "2026-04-01T19:00:00Z")
        h2 = canonical_event_hash("lakers", "CELTICS", "2026-04-01T19:00:00Z")
        self.assertEqual(h1, h2)

    def test_normalize_h2h_outcomes(self):
        from sniper.feeds.odds import normalize_h2h_outcomes

        raw = [
            {"name": "Lakers", "price": 1.91},
            {"name": "Celtics", "price": 2.05},
        ]
        result = normalize_h2h_outcomes(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "Lakers")
        self.assertAlmostEqual(result[0]["price_decimal"], 1.91)
        self.assertAlmostEqual(result[0]["implied_prob_raw"], 1.0 / 1.91, places=4)
        self.assertEqual(result[1]["name"], "Celtics")
        self.assertAlmostEqual(result[1]["price_decimal"], 2.05)
        self.assertAlmostEqual(result[1]["implied_prob_raw"], 1.0 / 2.05, places=4)

    def test_normalize_h2h_american_odds(self):
        """American-style odds should not be present in The Odds API decimal format,
        but if price <= 1.0 we skip it (guard rail)."""
        from sniper.feeds.odds import normalize_h2h_outcomes

        raw = [
            {"name": "Team A", "price": 0.0},   # invalid
            {"name": "Team B", "price": 2.50},
        ]
        result = normalize_h2h_outcomes(raw)
        # Only valid entries are returned
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Team B")

    def test_tick_parses_api_response(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)

        commence = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        mock_response = [
            {
                "id": "api_event_1",
                "sport_key": "basketball_nba",
                "sport_title": "NBA",
                "home_team": "Lakers",
                "away_team": "Celtics",
                "commence_time": commence,
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "last_update": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": datetime.now(timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                ),
                                "outcomes": [
                                    {"name": "Lakers", "price": 1.91},
                                    {"name": "Celtics", "price": 2.05},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        with patch.dict("os.environ", {"ODDS_SPORT_KEYS": "basketball_nba"}):
            with patch.object(adapter, "_fetch_odds", return_value=mock_response):
                adapter.tick(conn)

        # Verify rows were inserted
        rows = conn.execute("SELECT * FROM odds_snapshots").fetchall()
        self.assertEqual(len(rows), 2)  # 2 outcomes (Lakers + Celtics)

        # Check content
        row_dicts = [dict(r) for r in rows]
        names = {r["outcome_name"] for r in row_dicts}
        self.assertEqual(names, {"Lakers", "Celtics"})

        # Verify sport_key stored correctly
        for r in row_dicts:
            self.assertEqual(r["sport_key"], "basketball_nba")
            self.assertEqual(r["bookmaker_key"], "draftkings")
            self.assertEqual(r["market_key"], "h2h")

    def test_tick_enters_backoff_on_quota_error(self):
        from sniper.feeds.odds import OddsFeedAdapter, _OddsFeedCooldownError

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)

        def exhaust_and_raise(sport_key):
            key = adapter._keyring.current_key()
            if key:
                adapter._keyring.mark_exhausted(key)
            raise _OddsFeedCooldownError("out_of_usage_credits", backoff_seconds=123)

        with patch.dict("os.environ", {"ODDS_API_KEY": "test-key", "ODDS_SPORT_KEYS": "basketball_nba", "ODDS_API_KEYS_FILE": ""}):
            with patch.object(adapter, "_fetch_odds", side_effect=exhaust_and_raise):
                adapter.tick(conn)

        self.assertGreater(adapter._cooldown_until_ts, 0)

    def test_tick_skips_fetch_while_in_backoff(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)
        adapter._cooldown_until_ts = 9999999999

        with patch.object(adapter, "_fetch_odds") as mock_fetch:
            adapter.tick(conn)

        mock_fetch.assert_not_called()

    def test_fetch_odds_translates_out_of_credits_to_cooldown(self):
        from sniper.feeds.odds import OddsFeedAdapter, _OddsFeedCooldownError

        adapter = OddsFeedAdapter()
        body = io.BytesIO(
            b'{"message":"Usage quota has been reached","error_code":"OUT_OF_USAGE_CREDITS"}'
        )
        http_error = urllib.error.HTTPError(
            url="https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=body,
        )

        with patch.dict("os.environ", {"ODDS_API_KEY": "test-key"}):
            adapter._keyring.load()
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(_OddsFeedCooldownError) as ctx:
                    adapter._fetch_odds("basketball_nba")

        self.assertEqual(ctx.exception.reason, "out_of_usage_credits")


class TestKeyRing(unittest.TestCase):
    """_KeyRing multi-key rotation logic."""

    def test_load_single_key(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEY": "key-single"}, clear=True):
            ring.load()
        self.assertTrue(ring.has_keys)
        self.assertEqual(ring.active_count, 1)
        self.assertEqual(ring.current_key(), "key-single")

    def test_load_multi_keys(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2,k3"}, clear=True):
            ring.load()
        self.assertTrue(ring.has_keys)
        self.assertEqual(ring.active_count, 3)
        self.assertIn(ring.current_key(), {"k1", "k2", "k3"})

    def test_load_multi_takes_priority_over_single(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "multi1,multi2", "ODDS_API_KEY": "single"}, clear=True):
            ring.load()
        self.assertEqual(ring.active_count, 2)
        self.assertIn(ring.current_key(), {"multi1", "multi2"})

    def test_load_empty(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {}, clear=True):
            ring.load()
        self.assertFalse(ring.has_keys)
        self.assertIsNone(ring.current_key())

    def test_mark_exhausted_rotates(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2,k3"}, clear=True):
            ring.load()
        first = ring.current_key()
        ring.mark_exhausted(first)
        second = ring.current_key()
        self.assertNotEqual(first, second)
        self.assertEqual(ring.active_count, 2)

    def test_all_exhausted(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2"}, clear=True):
            ring.load()
        ring.mark_exhausted("k1")
        self.assertFalse(ring.all_exhausted)
        ring.mark_exhausted("k2")
        self.assertTrue(ring.all_exhausted)
        self.assertIsNone(ring.current_key())

    def test_reset_exhausted(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2"}, clear=True):
            ring.load()
        ring.mark_exhausted("k1")
        ring.mark_exhausted("k2")
        self.assertTrue(ring.all_exhausted)
        ring.reset_exhausted()
        self.assertFalse(ring.all_exhausted)
        self.assertEqual(ring.active_count, 2)
        self.assertIsNotNone(ring.current_key())

    def test_load_prunes_stale_exhausted(self):
        """After reload with different keys, old exhaustion flags are pruned."""
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "old1,old2"}, clear=True):
            ring.load()
        ring.mark_exhausted("old1")
        # Reload with new keys
        with patch.dict("os.environ", {"ODDS_API_KEYS": "new1,new2"}, clear=True):
            ring.load()
        self.assertEqual(ring.active_count, 2)  # old exhaustion cleared

    def test_load_from_file(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("file-key-1\nfile-key-2\n# comment line\n\nfile-key-3\n")
            path = f.name
        try:
            with patch.dict("os.environ", {"ODDS_API_KEYS_FILE": path}, clear=True):
                ring.load()
            self.assertEqual(ring.active_count, 3)
            self.assertIn(ring.current_key(), {"file-key-1", "file-key-2", "file-key-3"})
        finally:
            os.unlink(path)

    def test_file_takes_priority_over_env(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("from-file\n")
            path = f.name
        try:
            with patch.dict("os.environ", {"ODDS_API_KEYS_FILE": path, "ODDS_API_KEYS": "from-env"}, clear=True):
                ring.load()
            self.assertEqual(ring.current_key(), "from-file")
        finally:
            os.unlink(path)

    def test_file_appending_picks_up_new_keys(self):
        """Simulates appending keys to the file between ticks."""
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("morning-key\n")
            path = f.name
        try:
            with patch.dict("os.environ", {"ODDS_API_KEYS_FILE": path}, clear=True):
                ring.load()
                self.assertEqual(ring.active_count, 1)
                ring.mark_exhausted("morning-key")
                self.assertTrue(ring.all_exhausted)

                # Append a new key (simulates user pasting one in)
                with open(path, "a") as fh:
                    fh.write("afternoon-key\n")
                ring.load()

            self.assertEqual(ring.active_count, 1)  # morning still exhausted, afternoon fresh
            self.assertEqual(ring.current_key(), "afternoon-key")
        finally:
            os.unlink(path)

    def test_file_missing_falls_back_to_env(self):
        from sniper.feeds.odds import _KeyRing

        ring = _KeyRing()
        with patch.dict("os.environ", {"ODDS_API_KEYS_FILE": "/nonexistent/keys.txt", "ODDS_API_KEY": "env-fallback"}, clear=True):
            ring.load()
        self.assertEqual(ring.current_key(), "env-fallback")


class TestKeyRotationIntegration(unittest.TestCase):
    """OddsFeedAdapter integration with _KeyRing."""

    def test_validate_config_multi_keys(self):
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2,k3"}):
            self.assertTrue(adapter.validate_config())

    def test_tick_rotates_key_on_quota_error(self):
        """When first key is exhausted, tick retries with next key."""
        from sniper.feeds.odds import OddsFeedAdapter, _OddsFeedCooldownError

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)

        call_count = 0
        def mock_fetch(sport_key):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                adapter._keyring.mark_exhausted("k1")
                raise _OddsFeedCooldownError("out_of_usage_credits")
            return []  # success on retry

        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2", "ODDS_SPORT_KEYS": "basketball_nba"}):
            adapter._keyring.load()
            with patch.object(adapter, "_fetch_odds", side_effect=mock_fetch):
                adapter.tick(conn)

        self.assertEqual(call_count, 2)
        # Should NOT be in cooldown since k2 succeeded
        self.assertEqual(adapter._cooldown_until_ts, 0.0)

    def test_tick_enters_cooldown_when_all_keys_exhausted(self):
        """When all keys are exhausted, tick enters cooldown."""
        from sniper.feeds.odds import OddsFeedAdapter, _OddsFeedCooldownError

        adapter = OddsFeedAdapter()
        conn = _in_memory_conn()
        adapter.warmup(conn)

        def mock_fetch(sport_key):
            key = adapter._keyring.current_key()
            if key:
                adapter._keyring.mark_exhausted(key)
            raise _OddsFeedCooldownError("out_of_usage_credits", backoff_seconds=600)

        with patch.dict("os.environ", {"ODDS_API_KEYS": "k1,k2", "ODDS_SPORT_KEYS": "basketball_nba", "ODDS_API_KEYS_FILE": ""}):
            adapter._keyring.load()
            with patch.object(adapter, "_fetch_odds", side_effect=mock_fetch):
                adapter.tick(conn)

        self.assertGreater(adapter._cooldown_until_ts, 0)

    def test_fetch_odds_uses_keyring_key(self):
        """_fetch_odds should use the key from _keyring, not raw env."""
        from sniper.feeds.odds import OddsFeedAdapter

        adapter = OddsFeedAdapter()
        with patch.dict("os.environ", {"ODDS_API_KEYS": "ring-key-1,ring-key-2", "ODDS_API_KEYS_FILE": ""}):
            adapter._keyring.load()

        captured_urls = []

        def mock_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            resp = io.BytesIO(b"[]")
            resp.status = 200
            resp.headers = {}
            resp.read = resp.read
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            adapter._fetch_odds("basketball_nba")

        self.assertEqual(len(captured_urls), 1)
        self.assertIn("ring-key-1", captured_urls[0])

    def test_fetch_odds_marks_invalid_key_exhausted(self):
        """INVALID_KEY error should mark the key as exhausted."""
        from sniper.feeds.odds import OddsFeedAdapter, _OddsFeedCooldownError

        adapter = OddsFeedAdapter()
        body = io.BytesIO(
            b'{"message":"Invalid API key","error_code":"INVALID_KEY"}'
        )
        http_error = urllib.error.HTTPError(
            url="https://api.the-odds-api.com/v4/sports/basketball_nba/odds/",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=body,
        )

        with patch.dict("os.environ", {"ODDS_API_KEYS": "bad-key,good-key", "ODDS_API_KEYS_FILE": ""}):
            adapter._keyring.load()
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(_OddsFeedCooldownError) as ctx:
                    adapter._fetch_odds("basketball_nba")

        self.assertEqual(ctx.exception.reason, "invalid_key")
        self.assertEqual(adapter._keyring.active_count, 1)


class TestOddsFeedRegistry(unittest.TestCase):
    """Odds feed adapter should be discoverable via the registry."""

    def test_registry_lookup(self):
        from sniper.feeds.registry import get_feed_adapter

        adapter = get_feed_adapter("odds")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "odds")


if __name__ == "__main__":
    unittest.main()
