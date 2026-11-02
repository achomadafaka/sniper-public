import time
import unittest
from unittest.mock import patch

from sniper.analysis.legacy_wallet_scoring import (
    _around_position_metrics,
    _classify_open_position_state,
    _classify_position_signal,
    _classify_wallet,
    _closed_outcome_metrics,
    _score_position_insider,
    _score_position_speculator,
    _score_insider,
    _score_speculator,
    _should_suppress_position_insider_candidate,
    _speculative_overlay_insider_penalty,
)
from analyze_users import analyze_wallet
from db import get_connection, init_db


class ScoringTests(unittest.TestCase):
    def test_speculator_score_rises_with_bursts(self) -> None:
        low_score, _ = _score_speculator(
            trade_count=10,
            strategy_group="balanced_mixed",
            strategy_confidence=40.0,
            max_trades_60s=2,
            rapid_burst_trade_ratio=0.05,
            rapid_cluster_count=0,
            rapid_cluster_trades=0,
            median_gap_seconds=40.0,
        )
        high_score, _ = _score_speculator(
            trade_count=80,
            strategy_group="execution_bot",
            strategy_confidence=90.0,
            max_trades_60s=24,
            rapid_burst_trade_ratio=0.62,
            rapid_cluster_count=6,
            rapid_cluster_trades=48,
            median_gap_seconds=1.5,
        )
        self.assertGreater(high_score, low_score)
        self.assertGreaterEqual(high_score, 65.0)

    def test_insider_score_boosts_for_fresh_and_high_winrate(self) -> None:
        # _score_insider uses TYPE1 (fresh conviction) and TYPE2 (established insider) signals.
        # Parameters that drive differentiation: account_age_days, resolution_wins,
        # concentration_ratio, max_position_value, top_theme_open_share.
        # Note: is_fresh_account / high_winrate_qualified are position-level signals
        # used by _score_position_insider, not _score_insider.
        base_score, _ = _score_insider(
            position_count=4,
            pnl=30000,
            max_position_value=2600,
            total_open_value=7000,
            low_buy_hits_14d=3,
            avg_low_buy_notional_14d=2100,
            market_diversity_14d=5,
            concentration_ratio=0.38,
            strategy_group="single_thesis",
            strategy_confidence=52,
            max_trades_60s=4,
            rapid_burst_trade_ratio=0.18,
            top_theme_open_share=0.4,
            top_theme_market_count=2,
            is_fresh_account=False,
            win_rate=0.55,
            closed_sample_size=12,
            high_winrate_qualified=False,
            speculator_score=35,
            serious_trade_count_14d=3,
            median_trade_notional_14d=2200,
            max_trade_notional_14d=4600,
            account_age_days=180,
            resolution_wins=0,
            resolution_losses=0,
        )
        boosted_score, _ = _score_insider(
            position_count=4,
            pnl=30000,
            max_position_value=15000,
            total_open_value=20000,
            low_buy_hits_14d=3,
            avg_low_buy_notional_14d=2100,
            market_diversity_14d=5,
            concentration_ratio=0.65,
            strategy_group="single_thesis",
            strategy_confidence=52,
            max_trades_60s=4,
            rapid_burst_trade_ratio=0.18,
            top_theme_open_share=0.6,
            top_theme_market_count=3,
            is_fresh_account=True,
            win_rate=0.75,
            closed_sample_size=30,
            high_winrate_qualified=True,
            speculator_score=35,
            serious_trade_count_14d=6,
            median_trade_notional_14d=2800,
            max_trade_notional_14d=7000,
            account_age_days=14,
            resolution_wins=8,
            resolution_losses=2,
        )
        self.assertGreater(boosted_score, base_score)

    def test_speculative_overlay_penalty_rises_for_bursty_many_position_wallets(self) -> None:
        low_penalty, _ = _speculative_overlay_insider_penalty(
            position_count=4,
            rapid_burst_trade_ratio=0.08,
            max_trades_60s=2,
            speculator_score=18.0,
            position_speculator_count=0,
            max_position_speculator_score=15.0,
        )
        high_penalty, reasons = _speculative_overlay_insider_penalty(
            position_count=14,
            rapid_burst_trade_ratio=0.63,
            max_trades_60s=26,
            speculator_score=88.0,
            position_speculator_count=3,
            max_position_speculator_score=94.0,
        )
        self.assertGreater(high_penalty, low_penalty)
        self.assertGreaterEqual(high_penalty, 20.0)
        self.assertIn("many_positions_overlay_penalty", reasons)

    def test_classification_boundaries(self) -> None:
        classification, matches, _, _tier = _classify_wallet(
            insider_score=85,
            speculator_score=20,
            position_count=3,
            pnl=30000,
            max_position_value=7000,
            total_open_value=18000,
            low_buy_hits_14d=4,
            serious_trade_count_14d=5,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
        )
        self.assertEqual(classification, "insider_candidate")
        self.assertTrue(matches)

        classification, matches, _, _tier = _classify_wallet(
            insider_score=85,
            speculator_score=80,
            position_count=3,
            pnl=30000,
            max_position_value=7000,
            total_open_value=18000,
            low_buy_hits_14d=4,
            serious_trade_count_14d=5,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
        )
        self.assertEqual(classification, "mixed_review")
        self.assertFalse(matches)

        classification, matches, _, _tier = _classify_wallet(
            insider_score=40,
            speculator_score=35,
            position_count=8,
            pnl=30000,
            max_position_value=3500,
            total_open_value=12000,
            low_buy_hits_14d=1,
            serious_trade_count_14d=4,
            max_trade_notional_14d=5000,
            high_winrate_qualified=True,
            closed_sample_size=20,  # Must be >= HIGH_WINRATE_REVIEW_MIN_SAMPLE (15)
        )
        self.assertEqual(classification, "high_winrate_review")
        self.assertTrue(matches, "high_winrate_review with position-value gates met should now be qualified")

    def test_insider_candidate_can_pass_with_strong_open_value_and_low_realized_pnl(self) -> None:
        classification, matches, checks, _tier = _classify_wallet(
            insider_score=82,
            speculator_score=20,
            position_count=4,
            pnl=5000,
            max_position_value=6200,
            total_open_value=18000,
            low_buy_hits_14d=5,
            serious_trade_count_14d=5,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
        )
        self.assertEqual(classification, "insider_candidate")
        self.assertTrue(matches)
        self.assertTrue(checks["top_open_value_ok"])
        self.assertTrue(checks["total_open_value_ok"])

    def test_insider_candidate_fails_when_top_open_value_too_small(self) -> None:
        classification, matches, checks, _tier = _classify_wallet(
            insider_score=82,
            speculator_score=20,
            position_count=4,
            pnl=5000,
            max_position_value=2500,
            total_open_value=18000,
            low_buy_hits_14d=5,
            serious_trade_count_14d=5,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
        )
        self.assertNotEqual(classification, "insider_candidate")
        self.assertFalse(matches)
        self.assertFalse(checks["top_open_value_ok"])

    def test_insider_candidate_fails_when_total_open_value_too_small(self) -> None:
        classification, matches, checks, _tier = _classify_wallet(
            insider_score=82,
            speculator_score=20,
            position_count=4,
            pnl=5000,
            max_position_value=6200,
            total_open_value=8000,
            low_buy_hits_14d=5,
            serious_trade_count_14d=5,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
        )
        self.assertNotEqual(classification, "insider_candidate")
        self.assertFalse(matches)
        self.assertFalse(checks["total_open_value_ok"])

    def test_emerging_insider_when_repeat_hits_are_thin_but_position_signal_is_strong(self) -> None:
        classification, matches, checks, _tier = _classify_wallet(
            insider_score=67,
            speculator_score=22,
            position_count=4,
            pnl=12000,
            max_position_value=6400,
            total_open_value=18000,
            low_buy_hits_14d=0,
            serious_trade_count_14d=1,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
            position_insider_candidate_count=1,
            position_speculator_count=0,
            max_position_insider_score=72,
            max_position_speculator_score=18,
        )
        self.assertEqual(classification, "emerging_insider")
        self.assertFalse(matches)
        self.assertTrue(checks["position_signal_strength_ok"])
        self.assertTrue(checks["repeat_or_position_signal_ok"])
        self.assertTrue(checks["serious_or_position_signal_ok"])

    def test_emerging_insider_blocked_by_position_speculator_guard(self) -> None:
        classification, matches, checks, _tier = _classify_wallet(
            insider_score=68,
            speculator_score=30,
            position_count=4,
            pnl=12000,
            max_position_value=6400,
            total_open_value=18000,
            low_buy_hits_14d=0,
            serious_trade_count_14d=1,
            max_trade_notional_14d=7000,
            high_winrate_qualified=False,
            position_insider_candidate_count=1,
            position_speculator_count=2,
            max_position_insider_score=72,
            max_position_speculator_score=86,
        )
        self.assertNotEqual(classification, "emerging_insider")
        self.assertFalse(matches)
        self.assertFalse(checks["position_speculator_guard_ok"])

    def test_winrate_sample_floor(self) -> None:
        # Updated for WINRATE_MIN_SAMPLE=10 (was 20)
        under_sample = [{"realizedPnl": 100}] * 5 + [{"realizedPnl": -25}] * 4  # 9 samples, below threshold
        metrics = _closed_outcome_metrics(under_sample)
        self.assertAlmostEqual(metrics["win_rate"], 5 / 9)
        self.assertEqual(metrics["high_winrate_qualified"], 0.0)

        at_sample = [{"realizedPnl": 100}] * 8 + [{"realizedPnl": -25}] * 2  # 10 samples, at threshold, 80% win rate
        metrics = _closed_outcome_metrics(at_sample)
        self.assertAlmostEqual(metrics["win_rate"], 0.8)
        self.assertEqual(metrics["high_winrate_qualified"], 1.0)

    def test_open_position_state_classification(self) -> None:
        now_ts = int(time.time())
        pending_win_state, _ = _classify_open_position_state(
            {
                "curPrice": 1.0,
                "currentValue": 1000,
                "size": 1000,
                "cashPnl": 120,
                "endDate": "2020-01-01T00:00:00Z",
            },
            now_ts=now_ts,
        )
        self.assertEqual(pending_win_state, "pending_settlement_win")

        active_state, _ = _classify_open_position_state(
            {
                "curPrice": 1.0,
                "currentValue": 1000,
                "size": 1000,
                "cashPnl": 120,
                "endDate": "2035-01-01T00:00:00Z",
            },
            now_ts=now_ts,
        )
        self.assertEqual(active_state, "active")


class FreshnessFallbackTests(unittest.TestCase):
    def test_activity_fallback_to_candidate_first_seen(self) -> None:
        conn = get_connection(":memory:")
        init_db(conn)

        wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        first_seen = int(time.time()) - 86400
        conn.execute(
            """
            INSERT INTO candidate_users (
                wallet,
                first_seen_ts,
                last_seen_ts,
                low_buy_hits,
                last_condition_id,
                last_title
            ) VALUES (?, ?, ?, 1, '', '')
            """,
            (wallet, first_seen, first_seen),
        )
        conn.commit()

        with patch("analyze_users.fetch_user_trades", return_value=[]), patch(
            "analyze_users.fetch_user_positions", return_value=[]
        ), patch("analyze_users.fetch_user_value", return_value=0.0), patch(
            "analyze_users.fetch_user_activity", side_effect=RuntimeError("activity unavailable")
        ), patch(
            "analyze_users.wallet_low_buy_features",
            return_value={"hits": 0.0, "avg_notional": 0.0, "total_notional": 0.0, "market_diversity": 0.0},
        ), patch(
            "analyze_users.get_wallet_profile", return_value=None
        ), patch(
            "analyze_users.upsert_wallet_profile", return_value=None
        ):
            result = analyze_wallet(conn, http_session=None, wallet=wallet)

        self.assertEqual(result["first_activity_ts"], first_seen)
        self.assertGreaterEqual(result["account_age_days"], 0.9)
        conn.close()

    def test_profile_joined_ts_overrides_recent_first_seen_age(self) -> None:
        conn = get_connection(":memory:")
        init_db(conn)

        wallet = "0xabababababababababababababababababababab"
        now_ts = int(time.time())
        first_seen = now_ts - 86400  # observed recently in our local stream
        joined_ts = now_ts - (40 * 86400)  # true account age from profile enrichment
        conn.execute(
            """
            INSERT INTO candidate_users (
                wallet,
                first_seen_ts,
                last_seen_ts,
                low_buy_hits,
                last_condition_id,
                last_title
            ) VALUES (?, ?, ?, 1, '', '')
            """,
            (wallet, first_seen, first_seen),
        )
        conn.commit()

        with patch("analyze_users.fetch_user_trades", return_value=[]), patch(
            "analyze_users.fetch_user_positions", return_value=[]
        ), patch("analyze_users.fetch_user_value", return_value=0.0), patch(
            "analyze_users.fetch_user_activity", return_value=[]
        ), patch(
            "analyze_users.fetch_wallet_profile_enrichment",
            return_value={
                "profile_joined_ts": joined_ts,
                "profile_prediction_count": 1597,
                "profile_enriched_ts": now_ts,
                "profile_enrichment_source": "test_fixture",
            },
        ), patch(
            "analyze_users.wallet_low_buy_features",
            return_value={"hits": 0.0, "avg_notional": 0.0, "total_notional": 0.0, "market_diversity": 0.0},
        ):
            result = analyze_wallet(conn, http_session=object(), wallet=wallet)

        self.assertEqual(result["profile_joined_ts"], joined_ts)
        self.assertEqual(result["profile_prediction_count"], 1597)
        # Age should come from profile_joined_ts (about 40d), not first_seen fallback (~1d).
        self.assertGreater(result["account_age_days"], 30.0)
        profile_row = conn.execute(
            "SELECT profile_joined_ts, profile_prediction_count FROM wallet_profiles WHERE wallet = ?",
            (wallet,),
        ).fetchone()
        self.assertIsNotNone(profile_row)
        self.assertEqual(int(profile_row["profile_joined_ts"] or 0), joined_ts)
        self.assertEqual(int(profile_row["profile_prediction_count"] or 0), 1597)
        conn.close()


class PositionSignalScoringTests(unittest.TestCase):
    def test_around_position_windows(self) -> None:
        anchor = 1_700_000_000
        trades = [
            {"timestamp": anchor, "price": 0.2, "size": 500},
            {"timestamp": anchor + 120, "price": 0.22, "size": 300},
            {"timestamp": anchor + 1800, "price": 0.24, "size": 100},
            {"timestamp": anchor + 8000, "price": 0.3, "size": 40},
        ]
        # now_ts must be passed explicitly — trades use a 2023 anchor timestamp.
        # Trades AFTER now_ts get recency clamped to 0 (treated as "just now") and
        # fall inside all windows. With now_ts=anchor+520:
        #   anchor+0   → recency 520s → outside 5m (>300s), outside 1h (>3600? no, 520<3600 so IN 1h)
        #   anchor+120 → recency 400s → outside 5m, inside 1h
        #   anchor+1800 → future → clamped to 0 → inside 5m
        #   anchor+8000 → future → clamped to 0 → inside 5m
        # buys_5m=2 (the two future trades), buys_1h=4 (all trades), buys_24h=4
        now_ts = anchor + 520
        metrics = _around_position_metrics(trades, anchor_ts=anchor, now_ts=now_ts)
        self.assertEqual(metrics["buys_5m"], 2.0)
        self.assertEqual(metrics["buys_24h"], 4.0)
        self.assertGreater(metrics["buys_24h"], metrics["buys_5m"])
        self.assertGreater(metrics["notional_24h"], metrics["notional_5m"])
        self.assertGreaterEqual(metrics["max_trades_60s"], 1.0)

    def test_position_speculator_score_rises_for_bursts(self) -> None:
        low = _score_position_speculator(
            buys_5m=1,
            buys_1h=2,
            unique_sizes_5m=1,
            max_trades_60s=1,
            notional_5m=500,
            position_value=5000,
        )
        high = _score_position_speculator(
            buys_5m=12,
            buys_1h=18,
            unique_sizes_5m=7,
            max_trades_60s=11,
            notional_5m=6500,
            position_value=7000,
        )
        self.assertGreater(high, low)
        self.assertGreaterEqual(high, 65.0)

    def test_position_insider_score_improves_with_fresh_focused_wallet(self) -> None:
        low = _score_position_insider(
            position_value=2500,
            position_share=0.2,
            position_count_wallet=9,
            low_buy_hits_14d=1,
            is_fresh_account=False,
            win_rate=0.45,
            closed_sample_size=9,
            high_winrate_qualified=False,
            buys_1h=1,
            buys_5m=1,
            buys_24h=1,
            max_trades_60s=1,
            position_speculator_score=20.0,
        )
        high = _score_position_insider(
            position_value=12000,
            position_share=0.65,
            position_count_wallet=3,
            low_buy_hits_14d=5,
            is_fresh_account=True,
            win_rate=0.78,
            closed_sample_size=32,
            high_winrate_qualified=True,
            buys_1h=4,
            buys_5m=2,
            buys_24h=6,
            max_trades_60s=3,
            position_speculator_score=25.0,
        )
        self.assertGreater(high, low)

    def test_position_classification_boundaries(self) -> None:
        self.assertEqual(
            _classify_position_signal(
                position_insider_score=64.9,
                position_speculator_score=20.0,
                buys_1h=3,
            ),
            "position_watch",
        )
        self.assertEqual(
            _classify_position_signal(
                position_insider_score=65.0,
                position_speculator_score=64.9,
                buys_1h=2,
            ),
            "position_insider_candidate",
        )
        self.assertEqual(
            _classify_position_signal(
                position_insider_score=70.0,
                position_speculator_score=70.0,
                buys_1h=4,
            ),
            "position_speculator_ladder",
        )

    def test_extreme_execution_bot_suppresses_position_insider_candidate(self) -> None:
        self.assertTrue(
            _should_suppress_position_insider_candidate(
                strategy_group="execution_bot",
                strategy_confidence=96.0,
                rapid_burst_trade_ratio=0.72,
                max_trades_60s=30,
                speculator_score=98.0,
            )
        )
        self.assertFalse(
            _should_suppress_position_insider_candidate(
                strategy_group="execution_bot",
                strategy_confidence=72.0,
                rapid_burst_trade_ratio=0.72,
                max_trades_60s=30,
                speculator_score=98.0,
            )
        )
        self.assertFalse(
            _should_suppress_position_insider_candidate(
                strategy_group="single_thesis",
                strategy_confidence=96.0,
                rapid_burst_trade_ratio=0.72,
                max_trades_60s=30,
                speculator_score=98.0,
            )
        )

    def test_analyze_wallet_uses_active_positions_only_for_core_counts(self) -> None:
        conn = get_connection(":memory:")
        init_db(conn)
        wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        now_ts = int(time.time())

        open_positions = [
            {
                "conditionId": "cond-resolved",
                "outcome": "Yes",
                "title": "Resolved market",
                "eventSlug": "resolved-market",
                "currentValue": 10000,
                "size": 10000,
                "cashPnl": 2200,
                "curPrice": 1.0,
                "endDate": "2020-01-01T00:00:00Z",
            },
            {
                "conditionId": "cond-active",
                "outcome": "Yes",
                "title": "Active market",
                "eventSlug": "active-market",
                "currentValue": 3000,
                "size": 5000,
                "cashPnl": 150,
                "curPrice": 0.6,
                "endDate": "2035-01-01T00:00:00Z",
            },
        ]
        trades = [
            {
                "conditionId": "cond-active",
                "outcome": "Yes",
                "side": "BUY",
                "price": 0.6,
                "size": 5000,
                "timestamp": now_ts - 120,
                "title": "Active market",
                "eventSlug": "active-market",
            }
        ]

        with patch("analyze_users.fetch_user_trades", return_value=trades), patch(
            "analyze_users.fetch_user_positions",
            side_effect=[open_positions, []],
        ), patch("analyze_users.fetch_user_value", return_value=0.0), patch(
            "analyze_users.fetch_user_activity",
            return_value=[],
        ), patch(
            "analyze_users.wallet_low_buy_features",
            return_value={"hits": 0.0, "avg_notional": 0.0, "total_notional": 0.0, "market_diversity": 1.0},
        ), patch(
            "analyze_users.get_wallet_profile", return_value=None
        ), patch(
            "analyze_users.upsert_wallet_profile", return_value=None
        ):
            result = analyze_wallet(conn, http_session=None, wallet=wallet)

        self.assertEqual(result["position_count"], 1)
        self.assertEqual(result["total_position_count"], 2)
        self.assertEqual(result["pending_settlement_win_count"], 1)
        self.assertEqual(result["pending_settlement_loss_count"], 0)
        self.assertEqual(result["max_position_value"], 3000)
        self.assertEqual(result["top_open_position_title"], "Active market")
        self.assertEqual(result["total_open_value"], 3000)
        signal_keys = set(result["position_signal_open_keys"])
        self.assertIn(("cond-active", "yes"), signal_keys)
        self.assertNotIn(("cond-resolved", "yes"), signal_keys)
        conn.close()


if __name__ == "__main__":
    unittest.main()
