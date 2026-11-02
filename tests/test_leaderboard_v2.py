"""Tests for the V2 leaderboard: official discovery + internal proof."""

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import pytest

from db import (
    init_db,
    upsert_external_leaderboard_entry,
    latest_external_leaderboard_rows,
    wallet_leaderboard_v2_rows,
)
from db_discovery import enqueue_wallets_for_analysis, init_discovery_schema


@pytest.fixture(autouse=True)
def _allow_legacy_discovery_writes(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)


def _make_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    init_discovery_schema(conn)
    return conn, path


def _seed_official(conn, wallet, category="OVERALL", time_period="ALL",
                   rank_pnl=1, rank_vol=1, pnl=100000, volume=500000):
    """Seed both PNL and VOL rows for a wallet."""
    now = int(time.time())
    upsert_external_leaderboard_entry(
        conn, wallet=wallet, category=category, time_period=time_period,
        order_by="PNL", rank=rank_pnl, official_pnl=pnl,
        official_volume=volume, username=f"user_{wallet[:6]}",
        fetched_at=now,
    )
    upsert_external_leaderboard_entry(
        conn, wallet=wallet, category=category, time_period=time_period,
        order_by="VOL", rank=rank_vol, official_pnl=pnl,
        official_volume=volume, username=f"user_{wallet[:6]}",
        fetched_at=now,
    )
    conn.commit()


def _seed_analysis(conn, wallet, *, closed_sample_size=60,
                   qualifying_capital=30000, qualifying_roi=25.0,
                   qualifying_pnl=7500, win_rate=0.65,
                   classification="insider", insider_score=70,
                   speculator_score=20):
    """Insert a user_analysis row for a wallet."""
    conn.execute(
        """INSERT OR REPLACE INTO user_analysis
           (wallet, analyzed_ts, position_count, pnl, max_position_value,
            total_open_value, trade_count, insider_score, speculator_score,
            classification, win_rate, closed_sample_size, total_closed_count,
            qualifying_roi, qualifying_pnl, qualifying_capital,
            roi_percent, matches_criteria, strategy_group, strategy_confidence)
           VALUES (?, ?, 100, ?, 0, 0, 200, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', 0)""",
        (wallet, int(time.time()), qualifying_pnl,
         insider_score, speculator_score, classification,
         win_rate, closed_sample_size, closed_sample_size,
         qualifying_roi, qualifying_pnl, qualifying_capital,
         qualifying_roi),
    )
    conn.commit()


class TestUpsertExternalLeaderboardEntry(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_inserts_and_updates(self):
        w = "0x" + "a1" * 20
        upsert_external_leaderboard_entry(
            self.conn, wallet=w, category="OVERALL", time_period="ALL",
            order_by="PNL", rank=5, official_pnl=50000, official_volume=200000,
            username="alice", fetched_at=1000,
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM external_leaderboard_entries WHERE wallet=?", (w,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 5)
        self.assertEqual(rows[0]["username"], "alice")

        # Update same key
        upsert_external_leaderboard_entry(
            self.conn, wallet=w, category="OVERALL", time_period="ALL",
            order_by="PNL", rank=3, official_pnl=60000, official_volume=250000,
            username="alice_v2", fetched_at=2000,
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT * FROM external_leaderboard_entries WHERE wallet=?", (w,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], 3)
        self.assertEqual(rows[0]["official_pnl"], 60000)


class TestLatestExternalLeaderboardRows(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_coalesces_pnl_and_vol(self):
        w = "0x" + "b2" * 20
        now = int(time.time())
        upsert_external_leaderboard_entry(
            self.conn, wallet=w, category="OVERALL", time_period="ALL",
            order_by="PNL", rank=2, official_pnl=80000, official_volume=300000,
            fetched_at=now,
        )
        upsert_external_leaderboard_entry(
            self.conn, wallet=w, category="OVERALL", time_period="ALL",
            order_by="VOL", rank=5, official_pnl=80000, official_volume=300000,
            fetched_at=now,
        )
        self.conn.commit()

        rows = latest_external_leaderboard_rows(
            self.conn, category="OVERALL", time_period="ALL"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["official_rank_pnl"], 2)
        self.assertEqual(rows[0]["official_rank_vol"], 5)
        self.assertAlmostEqual(rows[0]["official_pnl"], 80000)
        self.assertAlmostEqual(rows[0]["official_volume"], 300000)


class TestWalletLeaderboardV2Qualification(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_qualified_wallet(self):
        w = "0x" + "c3" * 20
        _seed_official(self.conn, w, pnl=200000)
        _seed_analysis(self.conn, w, closed_sample_size=60, qualifying_capital=30000)

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 1)
        self.assertEqual(len(uq), 0)
        self.assertEqual(q[0]["qualification_status"], "qualified")
        self.assertEqual(meta["qualified_count"], 1)

    def test_sample_below_threshold_goes_unqualified(self):
        w = "0x" + "d4" * 20
        _seed_official(self.conn, w, pnl=150000)
        _seed_analysis(self.conn, w, closed_sample_size=10, qualifying_capital=30000)

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 1)
        self.assertEqual(uq[0]["qualification_reason"], "sample_below_threshold")

    def test_capital_below_threshold_goes_unqualified(self):
        w = "0x" + "e5" * 20
        _seed_official(self.conn, w, pnl=90000)
        _seed_analysis(self.conn, w, closed_sample_size=60, qualifying_capital=5000)

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 1)
        self.assertEqual(uq[0]["qualification_reason"], "capital_below_threshold")

    def test_missing_analysis_goes_unqualified(self):
        w = "0x" + "f6" * 20
        _seed_official(self.conn, w, pnl=300000)
        # No user_analysis row

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 1)
        self.assertEqual(uq[0]["qualification_reason"], "missing_analysis")

    def test_tracked_wallet_without_official_rank_goes_unqualified(self):
        w = "0x" + "a7" * 20
        # Add to watchlist but no official row
        self.conn.execute(
            "INSERT INTO watchlist_wallets (wallet, label, added_ts) VALUES (?, '', ?)",
            (w, int(time.time())),
        )
        self.conn.commit()
        _seed_analysis(self.conn, w, closed_sample_size=100, qualifying_capital=50000)

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 1)
        self.assertEqual(uq[0]["qualification_reason"], "missing_official_rank")


class TestWalletLeaderboardV2Blocked(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_blocked_wallet_excluded(self):
        w = "0x" + "b8" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w)
        self.conn.execute(
            "INSERT INTO blocked_wallets (wallet, reason, blocked_ts) VALUES (?, 'test', ?)",
            (w, int(time.time())),
        )
        self.conn.commit()

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 0)

    def test_ignored_wallet_excluded(self):
        w = "0x" + "c9" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w)
        self.conn.execute(
            "INSERT INTO ignored_wallets (wallet, reason, ignored_ts) VALUES (?, 'test', ?)",
            (w, int(time.time())),
        )
        self.conn.commit()

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 0)

    def test_blocked_classification_excluded(self):
        w = "0x" + "d0" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w, classification="blocked")

        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertEqual(len(uq), 0)


class TestWalletLeaderboardV2CopyabilityLabel(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_arb_fund_is_do_not_copy(self):
        w = "0x" + "e1" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w, classification="arb_fund")

        q, uq, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["copyability_label"], "do_not_copy")

    def test_speculator_bot_is_do_not_copy(self):
        w = "0x" + "e2" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w, classification="speculator_bot")

        q, uq, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["copyability_label"], "do_not_copy")

    def test_good_insider_is_copyable(self):
        w = "0x" + "e3" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w, classification="insider",
                       qualifying_roi=30.0, insider_score=70)

        q, uq, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["copyability_label"], "copyable")

    def test_low_insider_score_is_review(self):
        w = "0x" + "e4" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w, classification="insider",
                       qualifying_roi=30.0, insider_score=30)

        q, uq, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["copyability_label"], "review")


class TestWalletLeaderboardV2Sort(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def _seed_two(self):
        w1 = "0x" + "a1" * 20
        w2 = "0x" + "a2" * 20
        _seed_official(self.conn, w1, pnl=200000, volume=100000, rank_pnl=1, rank_vol=2)
        _seed_official(self.conn, w2, pnl=100000, volume=300000, rank_pnl=2, rank_vol=1)
        _seed_analysis(self.conn, w1, qualifying_roi=40.0, win_rate=0.70)
        _seed_analysis(self.conn, w2, qualifying_roi=60.0, win_rate=0.55)
        return w1, w2

    def test_sort_official_pnl(self):
        w1, w2 = self._seed_two()
        q, _, _ = wallet_leaderboard_v2_rows(self.conn, sort_by="official_pnl")
        self.assertEqual(q[0]["wallet"], w1)

    def test_sort_official_volume(self):
        w1, w2 = self._seed_two()
        q, _, _ = wallet_leaderboard_v2_rows(self.conn, sort_by="official_volume")
        self.assertEqual(q[0]["wallet"], w2)

    def test_sort_fit_roi(self):
        w1, w2 = self._seed_two()
        q, _, _ = wallet_leaderboard_v2_rows(self.conn, sort_by="fit_roi")
        self.assertEqual(q[0]["wallet"], w2)

    def test_sort_win_rate(self):
        w1, w2 = self._seed_two()
        q, _, _ = wallet_leaderboard_v2_rows(self.conn, sort_by="win_rate")
        self.assertEqual(q[0]["wallet"], w1)


class TestWalletLeaderboardV2NullFields(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_null_fields_stay_null(self):
        w = "0x" + "f1" * 20
        _seed_official(self.conn, w)
        # No analysis → internal fields should be None

        _, uq, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(uq), 1)
        self.assertIsNone(uq[0]["qualifying_roi"])
        self.assertIsNone(uq[0]["win_rate"])
        self.assertIsNone(uq[0]["qualifying_capital"])
        self.assertIsNone(uq[0]["closed_sample_size"])
        self.assertIsNone(uq[0]["classification"])


class TestWalletLeaderboardV2IdentityPrecedence(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_wallet_profile_takes_precedence(self):
        w = "0x" + "f2" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w)
        # Add wallet_profiles with username
        self.conn.execute(
            """INSERT INTO wallet_profiles (wallet, username, pseudonym, profile_image,
                   profile_image_optimized, bio, first_activity_ts, last_profile_sync_ts,
                   profile_joined_ts, profile_prediction_count, profile_enriched_ts,
                   profile_enrichment_source)
               VALUES (?, 'profile_name', 'pseudo', '', '', '', 0, ?, 0, 0, 0, '')""",
            (w, int(time.time())),
        )
        self.conn.commit()

        q, _, _ = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(q[0]["display_name"], "profile_name")


class TestEnqueueWalletsForAnalysis(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_enqueues_new_wallets(self):
        wallets = ["0x" + "a1" * 20, "0x" + "a2" * 20]
        count = enqueue_wallets_for_analysis(self.conn, wallets, priority_score=1100)
        self.assertEqual(count, 2)

        rows = self.conn.execute("SELECT * FROM wallet_analysis_queue ORDER BY wallet").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["priority_score"], 1100)
        self.assertEqual(rows[0]["state"], "queued")

    def test_resets_done_to_queued(self):
        w = "0x" + "a3" * 20
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO wallet_analysis_queue
               (wallet, priority_score, first_event_ts, last_event_ts, state)
               VALUES (?, 500, ?, ?, 'done')""",
            (w, now - 1000, now - 1000),
        )
        self.conn.commit()

        enqueue_wallets_for_analysis(self.conn, [w], priority_score=1100)
        row = self.conn.execute(
            "SELECT * FROM wallet_analysis_queue WHERE wallet=?", (w,)
        ).fetchone()
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["priority_score"], 1100)


class TestFetchLeaderboardRankings(unittest.TestCase):
    def test_parses_official_response(self):
        from sniper.markets.polymarket_api import fetch_leaderboard_rankings

        mock_data = [
            {
                "rank": "1",
                "proxyWallet": "0xAbC123" + "0" * 34,
                "userName": "TopTrader",
                "pnl": 150000.5,
                "vol": 800000.0,
                "profileImage": "https://example.com/img.jpg",
                "xUsername": "toptrader",
                "verifiedBadge": True,
            }
        ]
        session = MagicMock()
        with patch("sniper.markets.polymarket_api._request_json", return_value=mock_data):
            results = fetch_leaderboard_rankings(session, category="OVERALL", time_period="ALL")

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["wallet"], ("0xabc123" + "0" * 34).lower())
        self.assertEqual(r["rank"], 1)
        self.assertAlmostEqual(r["official_pnl"], 150000.5)
        self.assertAlmostEqual(r["official_volume"], 800000.0)
        self.assertEqual(r["username"], "TopTrader")
        self.assertTrue(r["verified_badge"])


class TestLeaderboardV2Meta(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _make_conn()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_empty_cache_is_stale(self):
        """No official data → meta.official_data_stale should be True."""
        q, uq, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertEqual(len(q), 0)
        self.assertTrue(meta["official_data_stale"])
        self.assertEqual(meta["official_source"], "polymarket_public_leaderboard")

    def test_fresh_data_not_stale(self):
        w = "0x" + "aa" * 20
        _seed_official(self.conn, w)
        _seed_analysis(self.conn, w)

        _, _, meta = wallet_leaderboard_v2_rows(self.conn)
        self.assertFalse(meta["official_data_stale"])


if __name__ == "__main__":
    unittest.main()
