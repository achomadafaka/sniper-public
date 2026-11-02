"""Tests for the followable leaderboard (V3)."""

import importlib
import math
import os
import sys
import tempfile
import time
import unittest

from db import (
    get_connection,
    init_db,
    wallet_followable_leaderboard_rows,
)


def _make_wallet(n: int) -> str:
    return f"0x{n:040x}"


def _insert_qualifying_wallet(
    conn,
    wallet: str,
    *,
    classification: str = "high_winrate_review",
    insider_score: float = 70.0,
    speculator_score: float = 30.0,
    win_rate: float = 0.75,
    closed_sample_size: int = 50,
    qualifying_roi: float = 20.0,
    qualifying_capital: float = 50000.0,
    qualifying_pnl: float = 10000.0,
    position_count: int = 5,
    trade_count: int = 100,
    low_buy_hits_14d: int = 3,
    pnl: float = 5000.0,
    lb_pnl: float | None = None,
    closed_realized_pnl: float = 0.0,
    open_cash_pnl: float = 0.0,
):
    """Insert a wallet into user_analysis that passes all qualification filters."""
    conn.execute(
        """INSERT OR REPLACE INTO user_analysis (
            wallet, analyzed_ts, position_count, pnl, max_position_value,
            total_open_value, trade_count, insider_score, speculator_score,
            classification, win_rate, wins, losses, closed_sample_size,
            high_winrate_qualified, matches_criteria, qualifying_roi,
            qualifying_pnl, qualifying_capital, low_buy_hits_14d,
            lb_pnl, closed_realized_pnl, open_cash_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            wallet, int(time.time()), position_count, pnl, 1000.0,
            500.0, trade_count, insider_score, speculator_score,
            classification, win_rate,
            int(win_rate * closed_sample_size),
            closed_sample_size - int(win_rate * closed_sample_size),
            closed_sample_size, 1, 1, qualifying_roi,
            qualifying_pnl, qualifying_capital, low_buy_hits_14d,
            lb_pnl, closed_realized_pnl, open_cash_pnl,
        ),
    )
    conn.commit()


class FollowableLeaderboardDBTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    # --- Filtering tests ---

    def test_followable_excludes_arb_fund(self):
        w = _make_wallet(1)
        _insert_qualifying_wallet(self.conn, w, classification="arb_fund")
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["filter_reason"], "arb_fund")

    def test_followable_excludes_speculator_bot(self):
        w = _make_wallet(2)
        _insert_qualifying_wallet(self.conn, w, classification="speculator_bot")
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "speculator_bot")

    def test_followable_excludes_blocked_wallet(self):
        w = _make_wallet(3)
        _insert_qualifying_wallet(self.conn, w)
        self.conn.execute(
            "INSERT INTO blocked_wallets (wallet, reason, blocked_ts) VALUES (?, ?, ?)",
            (w, "test", int(time.time())),
        )
        self.conn.commit()
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "blocked")

    def test_followable_excludes_ignored_wallet(self):
        w = _make_wallet(4)
        _insert_qualifying_wallet(self.conn, w)
        self.conn.execute(
            "INSERT INTO ignored_wallets (wallet, reason, ignored_ts) VALUES (?, ?, ?)",
            (w, "test", int(time.time())),
        )
        self.conn.commit()
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "ignored")

    def test_followable_requires_min_sample(self):
        w = _make_wallet(5)
        _insert_qualifying_wallet(self.conn, w, closed_sample_size=10)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn, min_sample=20)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "sample_below_min")

    def test_followable_requires_capital_band(self):
        w_low = _make_wallet(6)
        w_high = _make_wallet(7)
        _insert_qualifying_wallet(self.conn, w_low, qualifying_capital=1000.0)
        _insert_qualifying_wallet(self.conn, w_high, qualifying_capital=500000.0)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        reasons = {f["filter_reason"] for f in filtered}
        self.assertIn("capital_below_min", reasons)
        self.assertIn("capital_above_max", reasons)

    def test_followable_requires_non_null_roi(self):
        w = _make_wallet(8)
        _insert_qualifying_wallet(self.conn, w, qualifying_roi=None)
        # qualifying_roi=None means we need to insert directly
        self.conn.execute(
            "UPDATE user_analysis SET qualifying_roi = NULL WHERE wallet = ?", (w,)
        )
        self.conn.commit()
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "qualifying_roi_missing")

    def test_followable_requires_positive_roi(self):
        w = _make_wallet(9)
        _insert_qualifying_wallet(self.conn, w, qualifying_roi=-5.0)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "qualifying_roi_nonpositive")

    def test_followable_requires_min_win_rate(self):
        w = _make_wallet(10)
        _insert_qualifying_wallet(self.conn, w, win_rate=0.50)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "win_rate_below_min")

    def test_followable_allows_low_signal_if_metrics_strong(self):
        w = _make_wallet(11)
        _insert_qualifying_wallet(self.conn, w, classification="low_signal")
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["classification"], "low_signal")

    # --- Activity tests ---

    def test_followable_activity_recent_watchlist_trade(self):
        w = _make_wallet(20)
        _insert_qualifying_wallet(self.conn, w, low_buy_hits_14d=0, position_count=0)
        # Insert a recent watchlist trade
        self.conn.execute(
            "INSERT INTO watchlist_trades (wallet, transaction_hash, price, size, notional, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (w, "0xtx1", 0.5, 100, 50, int(time.time()) - 86400),
        )
        self.conn.commit()
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertIn("recent_watchlist_trade", items[0]["activity_sources"])

    def test_followable_activity_official_month_presence(self):
        w = _make_wallet(21)
        _insert_qualifying_wallet(self.conn, w, low_buy_hits_14d=0, position_count=0)
        self.conn.execute(
            "INSERT INTO external_leaderboard_entries (wallet, category, time_period, order_by, rank, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (w, "OVERALL", "MONTH", "PNL", 1, int(time.time())),
        )
        self.conn.commit()
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertIn("official_month_presence", items[0]["activity_sources"])

    def test_followable_activity_low_buy_hits_14d(self):
        w = _make_wallet(22)
        _insert_qualifying_wallet(self.conn, w, low_buy_hits_14d=5, position_count=0)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertIn("low_buy_hits_14d", items[0]["activity_sources"])

    def test_followable_activity_open_positions(self):
        w = _make_wallet(23)
        _insert_qualifying_wallet(self.conn, w, low_buy_hits_14d=0, position_count=3)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertIn("has_open_positions", items[0]["activity_sources"])

    def test_followable_inactive_when_no_activity_sources(self):
        w = _make_wallet(24)
        _insert_qualifying_wallet(self.conn, w, low_buy_hits_14d=0, position_count=0)
        items, filtered, meta = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 0)
        self.assertEqual(filtered[0]["filter_reason"], "inactive_30d")

    # --- Score tests ---

    def test_followability_score_formula_matches_spec(self):
        w = _make_wallet(30)
        _insert_qualifying_wallet(
            self.conn, w,
            classification="high_winrate_review",
            win_rate=0.80,
            qualifying_roi=30.0,
            qualifying_capital=50000.0,
            closed_sample_size=100,
            insider_score=70.0,
            speculator_score=30.0,
        )
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        item = items[0]

        # Manually compute expected score from the current P&L-first formula.
        pnl_score = 50.0  # no leaderboard P&L provided
        wr_score = max(0, min(100, (((0.80 * 0.85) - 0.50) / 0.40) * 100))
        roi_score = max(0, min(100, (30.0 / 40.0) * 100))
        sample_score = max(0, min(100,
            ((math.log(100) - math.log(20)) / (math.log(200) - math.log(20))) * 100
        ))
        capital_fit = 100.0  # 10k-100k

        expected = max(0, min(100,
            0.45 * pnl_score
            + 0.15 * roi_score
            + 0.15 * wr_score
            + 0.15 * sample_score
            + 0.10 * capital_fit
        ))

        self.assertAlmostEqual(item["followability_score"], round(expected, 1), places=1)

    def test_followability_score_sort_order(self):
        w1 = _make_wallet(31)
        w2 = _make_wallet(32)
        _insert_qualifying_wallet(self.conn, w1, win_rate=0.90, qualifying_roi=35.0)
        _insert_qualifying_wallet(self.conn, w2, win_rate=0.60, qualifying_roi=5.0)
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 2)
        self.assertGreater(items[0]["followability_score"], items[1]["followability_score"])
        self.assertEqual(items[0]["wallet"], w1)

    def test_followable_score_breakdown_fields_present(self):
        w = _make_wallet(33)
        _insert_qualifying_wallet(self.conn, w)
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        breakdown = items[0]["score_breakdown"]
        expected_keys = {
            "pnl_score",
            "win_rate_score",
            "roi_score",
            "sample_score",
            "capital_fit_score",
            "open_loss_penalty",
            "pnl_source",
        }
        self.assertEqual(set(breakdown.keys()), expected_keys)

    def test_followable_confidence_label_rules(self):
        w1 = _make_wallet(34)
        w2 = _make_wallet(35)
        w3 = _make_wallet(36)
        _insert_qualifying_wallet(self.conn, w1, classification="high_winrate_review")
        _insert_qualifying_wallet(self.conn, w2, classification="low_signal", speculator_score=65.0, lb_pnl=100_000.0)
        _insert_qualifying_wallet(self.conn, w3, classification="low_signal", speculator_score=30.0)
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        by_wallet = {i["wallet"]: i for i in items}
        self.assertEqual(by_wallet[w1]["confidence_label"], "reviewed")
        self.assertEqual(by_wallet[w2]["confidence_label"], "pnl-verified")
        self.assertEqual(by_wallet[w3]["confidence_label"], "metric-qualified")

    def test_followable_why_ranked_chip_rules(self):
        w = _make_wallet(37)
        _insert_qualifying_wallet(
            self.conn, w,
            win_rate=0.70, qualifying_roi=20.0,
            closed_sample_size=60, qualifying_capital=50000.0,
            insider_score=65.0,
        )
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        why = items[0]["why_ranked"]
        self.assertIn("high hit rate", why)
        self.assertIn("strong qualifying ROI", why)
        self.assertIn("large enough sample", why)
        self.assertIn("capital in followable band", why)
        # Max 4 chips even though insider_score >= 60 qualifies
        self.assertLessEqual(len(why), 4)

    # --- Identity test ---

    def test_followable_identity_precedence_uses_wallet_profiles(self):
        w = _make_wallet(40)
        _insert_qualifying_wallet(self.conn, w)
        self.conn.execute(
            """INSERT OR REPLACE INTO wallet_profiles
               (wallet, username, pseudonym, profile_image, profile_image_optimized)
               VALUES (?, ?, ?, ?, ?)""",
            (w, "testuser", "testpseudo", "img.png", "img_opt.png"),
        )
        self.conn.commit()
        items, _, _ = wallet_followable_leaderboard_rows(self.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["display_name"], "testuser")
        self.assertEqual(items[0]["profile_image"], "img_opt.png")

    # --- Filtered reason values ---

    def test_followable_filtered_reason_values(self):
        valid_reasons = {
            "ignored", "blocked", "arb_fund", "speculator_bot",
            "sample_below_min", "capital_below_min", "capital_above_max",
            "qualifying_roi_missing", "qualifying_roi_nonpositive",
            "win_rate_below_min", "inactive_30d",
        }
        # Insert wallets that trigger different reasons
        _insert_qualifying_wallet(self.conn, _make_wallet(50), classification="arb_fund")
        _insert_qualifying_wallet(self.conn, _make_wallet(51), qualifying_capital=1000.0)
        _insert_qualifying_wallet(self.conn, _make_wallet(52), win_rate=0.40)
        _, filtered, _ = wallet_followable_leaderboard_rows(self.conn)
        for f in filtered:
            self.assertIn(f["filter_reason"], valid_reasons)


class FollowableLeaderboardAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_followable.db")
        os.environ["SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST"] = "0"
        os.environ["SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST"] = "0"
        if "sniper.web.server" in sys.modules:
            del sys.modules["sniper.web.server"]
        cls.web = importlib.import_module("sniper.web.server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for table in ["user_analysis", "wallet_profiles", "watchlist_trades",
                       "external_leaderboard_entries", "blocked_wallets", "ignored_wallets"]:
            self.web.conn.execute(f"DELETE FROM {table}")
        self.web.conn.commit()

    def _seed_qualifying(self, wallet="0xaaa0000000000000000000000000000000000001"):
        _insert_qualifying_wallet(self.web.conn, wallet)
        self.web.conn.commit()

    def test_api_followable_response_shape(self):
        self._seed_qualifying()
        resp = self.client.get("/api/wallet-followable-leaderboard")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("items", data)
        self.assertIn("filtered_items", data)
        self.assertIn("meta", data)
        meta = data["meta"]
        self.assertIn("candidate_count", meta)
        self.assertIn("qualified_count", meta)
        self.assertIn("filtered_total_count", meta)
        self.assertIn("filtered_returned_count", meta)
        self.assertEqual(meta["board_goal"], "followable_wallets")
        self.assertTrue(data["items"])
        first = data["items"][0]
        self.assertIn("pnl", first)
        self.assertEqual(first["pnl"], first["total_pnl"])

    def test_api_followable_limit_clamped(self):
        self._seed_qualifying()
        resp = self.client.get("/api/wallet-followable-leaderboard?limit=0")
        data = resp.get_json()
        # limit=0 clamped to 1, so at most 1 item
        self.assertLessEqual(len(data["items"]), 1)

        resp2 = self.client.get("/api/wallet-followable-leaderboard?limit=9999")
        data2 = resp2.get_json()
        # Still works (clamped to 500)
        self.assertEqual(resp2.status_code, 200)

    def test_api_followable_filtered_limit_clamped(self):
        # Seed one wallet that will be filtered (arb_fund)
        _insert_qualifying_wallet(
            self.web.conn, _make_wallet(60), classification="arb_fund"
        )
        self.web.conn.commit()
        resp = self.client.get("/api/wallet-followable-leaderboard?filtered_limit=0")
        data = resp.get_json()
        self.assertEqual(len(data["filtered_items"]), 0)
        self.assertGreater(data["meta"]["filtered_total_count"], 0)

    def test_api_followable_include_filtered_false(self):
        _insert_qualifying_wallet(
            self.web.conn, _make_wallet(61), classification="arb_fund"
        )
        self.web.conn.commit()
        resp = self.client.get("/api/wallet-followable-leaderboard?include_filtered=false")
        data = resp.get_json()
        self.assertEqual(len(data["filtered_items"]), 0)

    def test_api_followable_defaults_match_policy(self):
        resp = self.client.get("/api/wallet-followable-leaderboard")
        meta = resp.get_json()["meta"]
        self.assertEqual(meta["min_sample"], 20)
        self.assertEqual(meta["min_capital"], 5000)
        self.assertEqual(meta["max_capital"], 250000)
        self.assertEqual(meta["min_win_rate"], 0.55)
        self.assertEqual(meta["activity_window_days"], 30)

    def test_api_followable_invalid_float_params_fall_back_to_defaults(self):
        resp = self.client.get(
            "/api/wallet-followable-leaderboard?min_capital=notanumber&min_win_rate=abc"
        )
        self.assertEqual(resp.status_code, 200)
        meta = resp.get_json()["meta"]
        # optional_float returns None for bad input, so defaults apply
        self.assertEqual(meta["min_capital"], 5000)
        self.assertEqual(meta["min_win_rate"], 0.55)


if __name__ == "__main__":
    unittest.main()
