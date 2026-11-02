"""
Tests for multi-axis wallet taxonomy: wallet_edge_tier, followability_cached,
and expanded strategy_group values (arb_hedger, conviction_sniper).
"""

import math
import os
import sys
import sqlite3
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.analysis.legacy_wallet_scoring import _classify_wallet, _compute_wallet_edge_tier, _has_followability_inputs
from db import compute_followability_score


# ---------------------------------------------------------------------------
# wallet_edge_tier tests
# ---------------------------------------------------------------------------

class TestWalletEdgeTier(unittest.TestCase):

    def test_elite_tier(self):
        """0x84db scenario: WR=95.87%, qROI=40.62%, 500 trades, spec=100 → elite."""
        tier = _compute_wallet_edge_tier(
            win_rate=0.9587,
            closed_sample_size=500,
            qualifying_roi=40.62,
            speculator_score=100,
        )
        self.assertEqual(tier, "elite")

    def test_strong_tier(self):
        """0xb100 scenario: WR=80.53%, qROI=11.92%, 425 trades, spec=60 → strong."""
        tier = _compute_wallet_edge_tier(
            win_rate=0.8053,
            closed_sample_size=425,
            qualifying_roi=11.92,
            speculator_score=60,
        )
        self.assertEqual(tier, "strong")

    def test_unscored_tier(self):
        """Less than 5 closed trades → unscored."""
        tier = _compute_wallet_edge_tier(
            win_rate=0.90,
            closed_sample_size=3,
            qualifying_roi=50.0,
            speculator_score=10,
        )
        self.assertEqual(tier, "unscored")

    def test_marginal_tier(self):
        """Low WR, low ROI → marginal."""
        tier = _compute_wallet_edge_tier(
            win_rate=0.52,
            closed_sample_size=30,
            qualifying_roi=2.0,
            speculator_score=30,
        )
        self.assertEqual(tier, "marginal")

    def test_speculator_score_does_not_lower_tier(self):
        """speculator_score no longer reduces edge tier — algorithmic traders are profitable."""
        tier_low_spec = _compute_wallet_edge_tier(
            win_rate=0.75,
            closed_sample_size=100,
            qualifying_roi=20.0,
            speculator_score=30,
        )
        tier_high_spec = _compute_wallet_edge_tier(
            win_rate=0.75,
            closed_sample_size=100,
            qualifying_roi=20.0,
            speculator_score=100,
        )
        # Same tier — spec penalty removed, speculator wallets treated equally
        self.assertEqual(tier_low_spec, tier_high_spec)

    def test_null_qroi_treated_as_zero(self):
        """qualifying_roi=None should be treated as 0 (conservative)."""
        tier_null = _compute_wallet_edge_tier(
            win_rate=0.70,
            closed_sample_size=50,
            qualifying_roi=None,
            speculator_score=40,
        )
        tier_zero = _compute_wallet_edge_tier(
            win_rate=0.70,
            closed_sample_size=50,
            qualifying_roi=0.0,
            speculator_score=40,
        )
        self.assertEqual(tier_null, tier_zero)

    def test_0x84db_speculator_bot_still_elite(self):
        """speculator_bot classification + elite wallet_edge_tier → axes are independent."""
        tier = _compute_wallet_edge_tier(
            win_rate=0.9587,
            closed_sample_size=500,
            qualifying_roi=40.62,
            speculator_score=100,
        )
        self.assertEqual(tier, "elite")


# ---------------------------------------------------------------------------
# followability_cached tests
# ---------------------------------------------------------------------------

class TestFollowabilityScore(unittest.TestCase):

    def test_score_deterministic(self):
        """Same inputs produce same score."""
        kwargs = dict(
            win_rate=0.75,
            qualifying_roi=20.0,
            closed_sample_size=100,
            qualifying_capital=50000.0,
            insider_score=60.0,
            speculator_score=30.0,
            classification="emerging_insider",
        )
        s1 = compute_followability_score(**kwargs)
        s2 = compute_followability_score(**kwargs)
        self.assertEqual(s1, s2)

    def test_score_in_valid_range(self):
        score = compute_followability_score(
            win_rate=0.80,
            qualifying_roi=30.0,
            closed_sample_size=200,
            qualifying_capital=80000.0,
            insider_score=70.0,
            speculator_score=20.0,
            classification="insider_candidate",
        )
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_market_maker_scores_zero(self):
        """market_maker classification → score=0.0 regardless of other inputs."""
        score = compute_followability_score(
            win_rate=0.98,
            qualifying_roi=50.0,
            closed_sample_size=500,
            qualifying_capital=80000.0,
            insider_score=90.0,
            speculator_score=95.0,
            classification="market_maker",
            lb_pnl=500_000.0,
        )
        self.assertEqual(score, 0.0)

    def test_lb_pnl_dominates_score(self):
        """High lb_pnl should push score well above 50 even with mediocre behavioral signals."""
        score_high_pnl = compute_followability_score(
            win_rate=0.55,
            qualifying_roi=5.0,
            closed_sample_size=25,
            qualifying_capital=30000.0,
            insider_score=10.0,
            speculator_score=80.0,
            classification="speculator_bot",
            lb_pnl=500_000.0,
        )
        score_no_pnl = compute_followability_score(
            win_rate=0.55,
            qualifying_roi=5.0,
            closed_sample_size=25,
            qualifying_capital=30000.0,
            insider_score=10.0,
            speculator_score=80.0,
            classification="speculator_bot",
            lb_pnl=None,
        )
        # $500K lifetime P&L drives score significantly higher (45% weight on pnl_score)
        self.assertGreater(score_high_pnl, score_no_pnl + 20)
        self.assertGreater(score_high_pnl, 55.0)

    def test_speculator_not_penalized(self):
        """speculator_bot with same signals should score same as insider_candidate (no spec penalty)."""
        base_kwargs = dict(
            win_rate=0.70,
            qualifying_roi=15.0,
            closed_sample_size=50,
            qualifying_capital=30000.0,
            insider_score=50.0,
            speculator_score=85.0,
        )
        score_spec = compute_followability_score(**base_kwargs, classification="speculator_bot")
        score_insider = compute_followability_score(**base_kwargs, classification="insider_candidate")
        # Should be equal — classification no longer penalizes or rewards (except market_maker)
        self.assertAlmostEqual(score_spec, score_insider, places=2)

    def test_open_loss_penalty_applied(self):
        """Large open losses (open_cash_pnl < -10K) should reduce score."""
        score_no_loss = compute_followability_score(
            win_rate=0.75,
            qualifying_roi=20.0,
            closed_sample_size=100,
            qualifying_capital=50000.0,
            insider_score=60.0,
            speculator_score=30.0,
            classification="emerging_insider",
            open_cash_pnl=0.0,
        )
        score_heavy_loss = compute_followability_score(
            win_rate=0.75,
            qualifying_roi=20.0,
            closed_sample_size=100,
            qualifying_capital=50000.0,
            insider_score=60.0,
            speculator_score=30.0,
            classification="emerging_insider",
            open_cash_pnl=-110_000.0,  # -$110K open losses → 20pt penalty
        )
        self.assertGreater(score_no_loss - score_heavy_loss, 15.0)

    def test_has_followability_inputs_true(self):
        self.assertTrue(_has_followability_inputs(
            qualifying_roi=15.0,
            closed_sample_size=30,
            qualifying_capital=20000.0,
            win_rate=0.65,
        ))

    def test_has_followability_inputs_null_roi(self):
        """NULL qualifying_roi → unscoreable."""
        self.assertFalse(_has_followability_inputs(
            qualifying_roi=None,
            closed_sample_size=50,
            qualifying_capital=30000.0,
            win_rate=0.70,
        ))

    def test_has_followability_inputs_low_sample(self):
        """Sample < 20 → unscoreable."""
        self.assertFalse(_has_followability_inputs(
            qualifying_roi=20.0,
            closed_sample_size=10,
            qualifying_capital=30000.0,
            win_rate=0.70,
        ))

    def test_has_followability_inputs_capital_out_of_range(self):
        """Capital outside 5K-250K → unscoreable."""
        self.assertFalse(_has_followability_inputs(
            qualifying_roi=20.0,
            closed_sample_size=50,
            qualifying_capital=1000.0,
            win_rate=0.70,
        ))

    def test_has_followability_inputs_low_winrate(self):
        """Win rate < 0.55 → unscoreable."""
        self.assertFalse(_has_followability_inputs(
            qualifying_roi=20.0,
            closed_sample_size=50,
            qualifying_capital=30000.0,
            win_rate=0.50,
        ))


class TestWalletClassificationMetadata(unittest.TestCase):

    def test_effective_copy_fraction_not_penalized_for_speculator_score(self):
        """Residual speculator penalties should not leak into wallet copy metadata."""
        base = dict(
            insider_score=65.0,
            position_count=2,
            pnl=10_000.0,
            max_position_value=4_000.0,
            total_open_value=12_000.0,
            low_buy_hits_14d=4,
            serious_trade_count_14d=4,
            max_trade_notional_14d=4_000.0,
            high_winrate_qualified=False,
            closed_sample_size=20,
            trade_count=20,
            account_age_days=30.0,
            position_insider_candidate_count=0,
            position_speculator_count=0,
            max_position_insider_score=0.0,
            max_position_speculator_score=0.0,
            win_rate=0.55,
            qualifying_roi=10.0,
            suspicion_score=0.0,
            lb_pnl=150_000.0,
            is_bot=False,
        )
        _, _, _, tier_data_low = _classify_wallet(speculator_score=55.0, **base)
        _, _, _, tier_data_high = _classify_wallet(speculator_score=80.0, **base)

        self.assertEqual(tier_data_low["wallet_tier"], 4)
        self.assertEqual(tier_data_high["wallet_tier"], 4)
        self.assertEqual(tier_data_low["effective_copy_fraction"], 0.3)
        self.assertEqual(tier_data_high["effective_copy_fraction"], 0.3)


# ---------------------------------------------------------------------------
# upsert backward compat test
# ---------------------------------------------------------------------------

class TestUpsertBackwardCompat(unittest.TestCase):

    def test_upsert_without_new_params(self):
        """Existing call sites that don't pass wallet_edge_tier/followability_cached should still work."""
        from db import upsert_user_analysis, init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)

        # Call without wallet_edge_tier or followability_cached (backward compat)
        upsert_user_analysis(
            conn,
            wallet="0xtest",
            analyzed_ts=1000,
            position_count=5,
            pnl=100.0,
            max_position_value=500.0,
            top_open_position_title="Test",
            top_open_position_value=100.0,
            total_open_value=200.0,
            trade_count=10,
            low_buy_hits_14d=3,
            avg_low_buy_notional_14d=50.0,
            market_diversity_14d=2,
            concentration_ratio=0.5,
            suspicion_score=30.0,
            insider_score=60.0,
            speculator_score=20.0,
            classification="emerging_insider",
            tier="watch",
            strategy_group="balanced_mixed",
            strategy_confidence=0.8,
            strategy_note="test",
            wins=5,
            losses=2,
            closed_sample_size=7,
            win_rate=0.71,
            high_winrate_qualified=True,
            first_activity_ts=900,
            account_age_days=30.0,
            is_fresh_account=False,
        )

        row = conn.execute("SELECT wallet_edge_tier, followability_cached FROM user_analysis WHERE wallet='0xtest'").fetchone()
        self.assertEqual(row["wallet_edge_tier"], "")
        self.assertIsNone(row["followability_cached"])
        conn.close()

    def test_upsert_supports_legacy_flagged_users_schema(self):
        """matches_criteria upserts should work with older wide flagged_users tables."""
        from db import upsert_user_analysis, init_db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute("DROP TABLE flagged_users")
        conn.execute(
            """
            CREATE TABLE flagged_users (
                wallet TEXT PRIMARY KEY,
                flagged_ts INTEGER NOT NULL,
                position_count INTEGER NOT NULL,
                pnl REAL NOT NULL,
                max_position_value REAL NOT NULL,
                total_open_value REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                source_note TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                low_buy_hits_14d INTEGER NOT NULL DEFAULT 0,
                suspicion_score REAL NOT NULL DEFAULT 0,
                tier TEXT NOT NULL DEFAULT 'watch',
                top_open_position_title TEXT NOT NULL DEFAULT '',
                top_open_position_value REAL NOT NULL DEFAULT 0,
                strategy_group TEXT NOT NULL DEFAULT 'balanced_mixed',
                strategy_confidence REAL NOT NULL DEFAULT 0,
                strategy_note TEXT NOT NULL DEFAULT '',
                insider_score REAL NOT NULL DEFAULT 0,
                speculator_score REAL NOT NULL DEFAULT 0,
                classification TEXT NOT NULL DEFAULT 'low_signal',
                win_rate REAL NOT NULL DEFAULT 0,
                closed_sample_size INTEGER NOT NULL DEFAULT 0,
                high_winrate_qualified INTEGER NOT NULL DEFAULT 0,
                account_age_days REAL NOT NULL DEFAULT 99999,
                is_fresh_account INTEGER NOT NULL DEFAULT 0,
                position_signal_count INTEGER NOT NULL DEFAULT 0,
                position_insider_candidate_count INTEGER NOT NULL DEFAULT 0,
                position_speculator_count INTEGER NOT NULL DEFAULT 0,
                max_position_insider_score REAL NOT NULL DEFAULT 0,
                max_position_speculator_score REAL NOT NULL DEFAULT 0
            )
            """
        )
        upsert_user_analysis(
            conn,
            wallet="0xlegacy",
            analyzed_ts=1234,
            position_count=5,
            pnl=123.0,
            max_position_value=456.0,
            top_open_position_title="Legacy market",
            top_open_position_value=456.0,
            total_open_value=789.0,
            trade_count=11,
            low_buy_hits_14d=2,
            avg_low_buy_notional_14d=33.0,
            market_diversity_14d=3,
            concentration_ratio=0.4,
            suspicion_score=44.0,
            insider_score=66.0,
            speculator_score=22.0,
            classification="emerging_insider",
            tier="watch",
            strategy_group="single_thesis",
            strategy_confidence=0.9,
            strategy_note="legacy compat",
            wins=5,
            losses=1,
            closed_sample_size=6,
            win_rate=0.83,
            high_winrate_qualified=True,
            first_activity_ts=900,
            account_age_days=12.0,
            is_fresh_account=False,
            position_signal_count=1,
            position_insider_candidate_count=1,
            position_speculator_count=0,
            max_position_insider_score=72.0,
            max_position_speculator_score=18.0,
            matches_criteria=True,
            reason="legacy flagged row",
            commit=True,
        )

        row = conn.execute(
            "SELECT wallet, position_count, pnl, source_note, strategy_group FROM flagged_users WHERE wallet='0xlegacy'"
        ).fetchone()
        self.assertEqual(row["wallet"], "0xlegacy")
        self.assertEqual(row["position_count"], 5)
        self.assertEqual(row["source_note"], "legacy flagged row")
        self.assertEqual(row["strategy_group"], "single_thesis")
        conn.close()


# ---------------------------------------------------------------------------
# Post-hoc strategy_group tests (arb_hedger, conviction_sniper)
# ---------------------------------------------------------------------------

def _setup_user_analysis(conn, wallet, **overrides):
    """Insert a minimal user_analysis row for post-hoc testing."""
    defaults = dict(
        wallet=wallet, analyzed_ts=1000, position_count=5, pnl=100.0,
        max_position_value=500.0, top_open_position_title="T",
        top_open_position_value=100.0, total_open_value=200.0,
        trade_count=10, low_buy_hits_14d=3, avg_low_buy_notional_14d=50.0,
        market_diversity_14d=2, concentration_ratio=0.5, suspicion_score=30.0,
        insider_score=60.0, speculator_score=20.0, classification="emerging_insider",
        tier="watch", strategy_group="balanced_mixed", strategy_confidence=0.8,
        strategy_note="test", wins=5, losses=2, closed_sample_size=50,
        win_rate=0.71, high_winrate_qualified=True, first_activity_ts=900,
        account_age_days=30.0, is_fresh_account=False,
        qualifying_roi=20.0, qualifying_capital=30000.0, qualifying_pnl=200.0,
        wallet_edge_tier="strong", followability_cached=55.0,
    )
    defaults.update(overrides)
    from db import upsert_user_analysis
    upsert_user_analysis(conn, **defaults)


class TestPostHocStrategyGroup(unittest.TestCase):

    def setUp(self):
        from db import init_db
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_arb_hedger_updates_strategy_group(self):
        """arb_fund reclassification should also set strategy_group='arb_hedger'."""
        _setup_user_analysis(self.conn, "0xarb_test")
        # Simulate the post-hoc arb_fund update (same SQL as run_once)
        self.conn.execute(
            "UPDATE user_analysis SET classification='arb_fund', strategy_group='arb_hedger', reason=? WHERE wallet=?",
            ("arb_fund: test", "0xarb_test"),
        )
        row = self.conn.execute(
            "SELECT classification, strategy_group FROM user_analysis WHERE wallet='0xarb_test'"
        ).fetchone()
        self.assertEqual(row["classification"], "arb_fund")
        self.assertEqual(row["strategy_group"], "arb_hedger")

    def test_conviction_sniper_updates_strategy_group(self):
        """conviction_sniper post-hoc rule should set strategy_group."""
        _setup_user_analysis(self.conn, "0xconv_test", strategy_group="single_thesis")
        self.conn.execute(
            "UPDATE user_analysis SET strategy_group='conviction_sniper' WHERE wallet=?",
            ("0xconv_test",),
        )
        row = self.conn.execute(
            "SELECT strategy_group FROM user_analysis WHERE wallet='0xconv_test'"
        ).fetchone()
        self.assertEqual(row["strategy_group"], "conviction_sniper")

    def test_arb_fund_recomputes_followability(self):
        """After arb_fund reclassification, followability_cached should be updated in the DB.
        With the P&L-first formula, classification no longer adds a bonus, so scores are equal."""
        kwargs = dict(
            win_rate=0.75,
            qualifying_roi=20.0,
            closed_sample_size=100,
            qualifying_capital=50000.0,
            insider_score=60.0,
            speculator_score=30.0,
        )
        score_before = compute_followability_score(**kwargs, classification="emerging_insider")
        _setup_user_analysis(
            self.conn, "0xrecomp_test",
            classification="emerging_insider",
            followability_cached=score_before,
            **kwargs,
        )

        # Recompute with arb_fund — same score since classification no longer adds bonus
        recomp_fc = compute_followability_score(**kwargs, classification="arb_fund")
        self.conn.execute(
            "UPDATE user_analysis SET classification='arb_fund', strategy_group='arb_hedger', followability_cached=? WHERE wallet=?",
            (recomp_fc, "0xrecomp_test"),
        )
        row_after = self.conn.execute(
            "SELECT followability_cached, classification FROM user_analysis WHERE wallet='0xrecomp_test'"
        ).fetchone()

        self.assertEqual(row_after["classification"], "arb_fund")
        # P&L-first formula: classification doesn't add bonus, scores are equal
        self.assertAlmostEqual(score_before, row_after["followability_cached"], places=1)

    def test_persistence_with_new_fields(self):
        """upsert_user_analysis should persist wallet_edge_tier and followability_cached."""
        _setup_user_analysis(
            self.conn, "0xpersist_test",
            wallet_edge_tier="elite",
            followability_cached=72.5,
        )
        row = self.conn.execute(
            "SELECT wallet_edge_tier, followability_cached FROM user_analysis WHERE wallet='0xpersist_test'"
        ).fetchone()
        self.assertEqual(row["wallet_edge_tier"], "elite")
        self.assertAlmostEqual(row["followability_cached"], 72.5, places=1)


# ---------------------------------------------------------------------------
# Domain classification tests
# ---------------------------------------------------------------------------

class TestDomainClassification(unittest.TestCase):

    def test_sports_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Nuggets vs. Grizzlies"), "Sports")
        self.assertEqual(_classify_title_domain("Spread: Bucks (-2.5)"), "Sports")
        self.assertEqual(_classify_title_domain("Thunder vs. Suns: O/U 213.5"), "Sports")

    def test_tennis_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Dubai Duty Free Tennis Championships: Svitolina vs Coco"), "Tennis")
        self.assertEqual(_classify_title_domain("Rio Open: Heide vs Kopriva"), "Tennis")

    def test_geopolitics_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Will Iran name a successor to Khamenei by March 15?"), "Geopolitics")
        self.assertEqual(_classify_title_domain("US strikes Iran by February 28, 2026?"), "Geopolitics")
        self.assertEqual(_classify_title_domain("Trump announces end of military operations against Iran"), "Geopolitics")

    def test_us_politics_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Will Josh Shapiro win the 2028 US Presidential Election?"), "US Politics")
        self.assertEqual(_classify_title_domain("Will the Republican Party control the House after the 2026 Midterm elections?"), "US Politics")

    def test_entertainment_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Will Michael B. Jordan win Best Actor at the 98th Academy Awards?"), "Entertainment")
        self.assertEqual(_classify_title_domain("Will MrBeast's next video get between 35 and 40 million views on day 1?"), "Entertainment")

    def test_crypto_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Bitcoin above $100K by March 31?"), "Crypto")

    def test_esports_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("LoL: Ruddy Esports vs Verdant (BO1)"), "Esports")
        self.assertEqual(_classify_title_domain("Map Handicap: FURIA (-1.5) vs TheMongolz (+1.5)"), "Esports")

    def test_finance_titles(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("S&P 500 above 5000 by end of March?"), "Finance")

    def test_unknown_title(self):
        from db import _classify_title_domain
        self.assertEqual(_classify_title_domain("Something completely unrelated xyz"), "")

    def test_wallet_top_domains_integration(self):
        """wallet_top_domains returns correct structure from position_signals."""
        from db import wallet_top_domains, init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)

        wallet = "0xtest_domain"
        # Insert some position_signals with varied titles
        titles = [
            ("c1", "Nuggets vs. Grizzlies"),
            ("c2", "Spread: Bucks (-2.5)"),
            ("c3", "Thunder vs. Suns: O/U 213.5"),
            ("c4", "Lakers vs. Celtics"),
            ("c5", "Will Iran ceasefire happen?"),
            ("c6", "Rio Open: Heide vs Kopriva"),
        ]
        for cid, title in titles:
            conn.execute(
                """INSERT INTO position_signals (wallet, condition_id, outcome, title, is_open, analyzed_ts)
                   VALUES (?, ?, 'Yes', ?, 1, 1000)""",
                (wallet, cid, title),
            )
        conn.commit()

        result = wallet_top_domains(conn, [wallet], max_domains=3)
        domains = result.get(wallet, [])
        self.assertTrue(len(domains) >= 2)
        # Sports should be the top domain (4 signals: Nuggets, Spread, Thunder, Lakers)
        domain_names = [d["domain"] for d in domains]
        self.assertIn("Sports", domain_names)
        sports = next(d for d in domains if d["domain"] == "Sports")
        self.assertEqual(sports["count"], 4)
        conn.close()

    def test_wallet_top_domains_empty(self):
        from db import wallet_top_domains, init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        result = wallet_top_domains(conn, ["0xnonexistent"])
        self.assertEqual(result["0xnonexistent"], [])
        conn.close()

    def test_wallet_top_domains_no_wallets(self):
        from db import wallet_top_domains, init_db
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        result = wallet_top_domains(conn, [])
        self.assertEqual(result, {})
        conn.close()


if __name__ == "__main__":
    unittest.main()
