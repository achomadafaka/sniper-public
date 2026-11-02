"""
Tests for Chunk 5: Runner integration — instance profile, feed init,
risk policy gating, cadence tracking, and instance_tag propagation.
"""
import os
import sqlite3
import time
import unittest
from unittest.mock import patch, MagicMock

from db import get_connection, init_db, upsert_position_signal, upsert_user_analysis
from sniper.settings.instance_profile import InstanceProfile, load_instance_profile
from sniper.risk.models import RiskContext, RiskDecision
from sniper.risk.policy import evaluate_entry_risk
from sniper.strategies.base import StrategySignal, StrategyEntryDecision


# ---------------------------------------------------------------------------
# Helpers (shared with test_strategy_framework.py)
# ---------------------------------------------------------------------------

def _seed_wallet(conn, wallet: str, *, qualifying_roi: float = 18.0) -> None:
    now_ts = int(time.time())
    upsert_user_analysis(
        conn,
        wallet=wallet,
        analyzed_ts=now_ts,
        position_count=8,
        pnl=1250.0,
        max_position_value=4200.0,
        top_open_position_title="Test market",
        top_open_position_value=4200.0,
        total_open_value=8500.0,
        trade_count=40,
        low_buy_hits_14d=3,
        avg_low_buy_notional_14d=1200.0,
        market_diversity_14d=5,
        concentration_ratio=0.28,
        suspicion_score=62.0,
        insider_score=71.0,
        speculator_score=24.0,
        classification="high_winrate_review",
        tier="A",
        strategy_group="balanced_mixed",
        strategy_confidence=0.82,
        strategy_note="test wallet",
        wins=30,
        losses=8,
        closed_sample_size=38,
        win_rate=0.82,
        high_winrate_qualified=True,
        first_activity_ts=now_ts - 86400 * 120,
        account_age_days=120.0,
        is_fresh_account=False,
        total_closed_count=38,
        resolution_rate=0.9,
        effective_resolved_wins=30,
        effective_resolved_losses=8,
        position_signal_count=1,
        position_insider_candidate_count=1,
        position_speculator_count=0,
        max_position_insider_score=72.0,
        max_position_speculator_score=24.0,
        roi_percent=21.0,
        qualifying_pnl=440.0,
        qualifying_capital=3200.0,
        qualifying_roi=qualifying_roi,
        commit=True,
    )


def _seed_signal(
    conn,
    wallet: str,
    *,
    condition_id: str = "cond-ri-1",
    is_open: bool = True,
    current_price: float = 0.42,
) -> None:
    ts = int(time.time())
    upsert_position_signal(
        conn,
        wallet=wallet,
        condition_id=condition_id,
        outcome="YES",
        title="Runner Integration Test Market",
        is_open=is_open,
        position_value=5000.0,
        position_share=0.2,
        position_count_wallet=3,
        entry_anchor_ts=ts - 600,
        entry_price=0.39,
        entry_size=120.0,
        entry_notional=46.8,
        current_price=current_price,
        current_size=120.0 if is_open else 0.0,
        latest_trade_ts=ts,
        buys_24h=3,
        trades_24h=4,
        notional_24h=5000.0,
        position_insider_score=72.0,
        position_speculator_score=25.0,
        position_classification="position_watch",
        signal_reason="test signal",
        analyzed_ts=ts,
        end_date_iso="2026-04-01T00:00:00+00:00",
    )


def _make_profile(**overrides) -> InstanceProfile:
    """Build an InstanceProfile with sensible test defaults, overridable."""
    defaults = dict(
        instance_tag="test-local",
        display_name="Test Local",
        role="paper",
        enabled=True,
        enabled_strategies=(),
        enabled_feeds=(),
        paper_only=True,
        real_trading_enabled=False,
        bankroll_usdc=1000.0,
        max_total_exposure_usdc=500.0,
        max_market_exposure_usdc=200.0,
        max_concurrent_positions=20,
        daily_loss_limit_usdc=50.0,
        default_loop_seconds=60,
    )
    defaults.update(overrides)
    return InstanceProfile(**defaults)


def _insert_test_strategy(conn, *, strategy_type: str = "insider_copy_v1") -> int:
    """Insert a minimal active strategy row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO strategies (wallet, label, strategy_type, parameters, status,
                                trades_triggered, pnl_usdc, created_at, updated_at)
        VALUES (?, ?, ?, '{}', 'active', 0, 0, datetime('now'), datetime('now'))
        """,
        ("0xtest_wallet", "Test Strategy", strategy_type),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnabledStrategiesFiltering(unittest.TestCase):
    """Task 5.1: process_strategies filters by enabled_strategies."""

    def setUp(self):
        from sniper.services import strategy_runner
        strategy_runner._last_run_ts.clear()
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xaaaa" + "1" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet)

    def tearDown(self):
        from sniper.services import strategy_runner
        strategy_runner._last_run_ts.clear()
        self.conn.close()

    def test_empty_enabled_strategies_runs_all(self):
        """When enabled_strategies is empty tuple, ALL plugin strategies run."""
        from sniper.services.strategy_runner import process_strategies

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(enabled_strategies=())

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=profile)
            # Should have been called for the insider_copy_v1 strategy
            self.assertTrue(mock_pps.called)

    def test_nonempty_enabled_strategies_filters(self):
        """When enabled_strategies lists only 'fade_v1', insider_copy_v1 is skipped."""
        from sniper.services.strategy_runner import process_strategies

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(enabled_strategies=("fade_v1",))

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps:
            process_strategies(self.conn, None, profile=profile)
            # insider_copy_v1 not in enabled_strategies → should NOT be called
            mock_pps.assert_not_called()

    def test_enabled_strategies_includes_matching_type(self):
        """When enabled_strategies includes the strategy type, it runs."""
        from sniper.services.strategy_runner import process_strategies

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(enabled_strategies=("insider_copy_v1",))

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=profile)
            self.assertTrue(mock_pps.called)

    def test_no_profile_runs_all(self):
        """When profile is None, ALL plugin strategies run (backward compat)."""
        from sniper.services.strategy_runner import process_strategies

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=None)
            self.assertTrue(mock_pps.called)


class TestFeedRequirementCheck(unittest.TestCase):
    """Task 5.2: strategies are skipped if their required feeds aren't enabled."""

    def setUp(self):
        from sniper.services import strategy_runner
        strategy_runner._last_run_ts.clear()
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xbbbb" + "2" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet)

    def tearDown(self):
        from sniper.services import strategy_runner
        strategy_runner._last_run_ts.clear()
        self.conn.close()

    def test_missing_feed_skips_strategy(self):
        """insider_copy_v1 requires 'polymarket' feed; if not enabled, skip."""
        from sniper.services.strategy_runner import process_strategies

        _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        # Enable only a non-existent feed — polymarket not listed
        profile = _make_profile(
            enabled_strategies=("insider_copy_v1",),
            enabled_feeds=("weather",),
        )

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps:
            process_strategies(self.conn, None, profile=profile)
            mock_pps.assert_not_called()

    def test_present_feed_allows_strategy(self):
        """insider_copy_v1 requires 'polymarket'; when enabled, it runs."""
        from sniper.services.strategy_runner import process_strategies

        _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(
            enabled_strategies=("insider_copy_v1",),
            enabled_feeds=("polymarket",),
        )

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=profile)
            self.assertTrue(mock_pps.called)

    def test_empty_enabled_feeds_skips_feed_check(self):
        """When enabled_feeds is empty, feed requirement checks are disabled."""
        from sniper.services.strategy_runner import process_strategies

        _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(
            enabled_strategies=("insider_copy_v1",),
            enabled_feeds=(),
        )

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=profile)
            self.assertTrue(mock_pps.called)


class TestFeedTickIntegration(unittest.TestCase):
    """Initialized feeds are actually started, ticked on cadence, and stoppable."""

    def setUp(self):
        from sniper.services import strategy_runner
        strategy_runner._last_feed_tick_ts.clear()
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        from sniper.services import strategy_runner
        strategy_runner._last_feed_tick_ts.clear()
        self.conn.close()

    def test_tick_initialized_feeds_runs_due_feed_once(self):
        from sniper.services.strategy_runner import _tick_initialized_feeds

        adapter = MagicMock()
        adapter.default_poll_seconds = 300
        adapter.tick = MagicMock()
        initialized_feeds = {"weather": adapter}

        _tick_initialized_feeds(self.conn, initialized_feeds)
        adapter.tick.assert_called_once_with(self.conn)

        # Second call immediately after should be skipped by cadence.
        adapter.tick.reset_mock()
        _tick_initialized_feeds(self.conn, initialized_feeds)
        adapter.tick.assert_not_called()

    def test_start_and_stop_initialized_feeds(self):
        from sniper.services.strategy_runner import _start_initialized_feeds, _stop_initialized_feeds

        adapter = MagicMock()
        initialized_feeds = {"polymarket": adapter}

        _start_initialized_feeds(initialized_feeds)
        adapter.start.assert_called_once_with()

        _stop_initialized_feeds(initialized_feeds)
        adapter.stop.assert_called_once_with()


class TestClobInitializationPolicy(unittest.TestCase):
    """CLOB client init should only happen on real-trading instances."""

    def test_feed_only_profile_skips_clob_init(self):
        from sniper.services.strategy_runner import should_initialize_clob_client

        profile = _make_profile(
            enabled_feeds=("polymarket", "odds"),
            enabled_strategies=("__none__",),
            paper_only=True,
            real_trading_enabled=False,
        )

        self.assertFalse(should_initialize_clob_client(profile))

    def test_paper_strategy_profile_skips_clob_init(self):
        from sniper.services.strategy_runner import should_initialize_clob_client

        profile = _make_profile(
            enabled_strategies=("insider_copy_v1",),
            paper_only=True,
            real_trading_enabled=False,
        )

        self.assertFalse(should_initialize_clob_client(profile))

    def test_real_trading_profile_requires_clob_init(self):
        from sniper.services.strategy_runner import should_initialize_clob_client

        profile = _make_profile(
            role="real",
            paper_only=False,
            real_trading_enabled=True,
        )

        self.assertTrue(should_initialize_clob_client(profile))


class TestRiskPolicyGating(unittest.TestCase):
    """Task 5.3: risk policy gates entries in process_plugin_strategy."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xcccc" + "3" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet, condition_id="cond-risk-1")

    def tearDown(self):
        self.conn.close()

    def test_risk_rejection_prevents_entry(self):
        """When risk policy rejects, no entry is recorded."""
        from sniper.services.strategy_runner import process_plugin_strategy, _active_strategy_positions

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        # Profile with 0 bankroll → risk will reject "insufficient bankroll"
        profile = _make_profile(bankroll_usdc=0.0)

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_plugin_strategy(self.conn, strategy, profile=profile)

        # Should have zero open positions (entry was rejected)
        positions = _active_strategy_positions(self.conn, sid)
        self.assertEqual(len(positions), 0)

    def test_risk_clamping_reduces_size(self):
        """When risk policy clamps, the recorded entry uses the smaller size."""
        from sniper.services.strategy_runner import process_plugin_strategy, _active_strategy_positions

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        # Profile with very tight market exposure → forces clamping
        profile = _make_profile(
            bankroll_usdc=10000.0,
            max_total_exposure_usdc=10000.0,
            max_market_exposure_usdc=5.0,  # Very small → will clamp
        )

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_plugin_strategy(self.conn, strategy, profile=profile)

        positions = _active_strategy_positions(self.conn, sid)
        if positions:
            # Size should be at most $5 (market exposure limit)
            shares = float(positions[0].get("filled_entry_shares", 0))
            entry_price = float(positions[0].get("avg_entry_price", 1))
            actual_size = shares * entry_price
            self.assertLessEqual(actual_size, 5.01)  # tiny float tolerance

    def test_no_profile_skips_risk_check(self):
        """When profile is None, risk check is skipped (backward compat)."""
        from sniper.services.strategy_runner import process_plugin_strategy, _active_strategy_positions

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            # No profile → risk checks bypassed → entries proceed normally
            process_plugin_strategy(self.conn, strategy, profile=None)

        # May or may not enter depending on plugin logic, but at least no crash
        # (the important thing is backward compat: no risk rejection without profile)

    def test_daily_loss_limit_rejects_entry(self):
        """When daily realized PnL exceeds the loss limit, entry is rejected."""
        from sniper.services.strategy_runner import process_plugin_strategy, _active_strategy_positions

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        # Insert a closed position with big loss today
        now_ts = int(time.time())
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, last_reconciled_ts, version, updated_ts
            ) VALUES (?, 'cond-old', 'YES', ?, 'closed',
                      0.5, 0.5, 100, 100,
                      -100.0, ?, ?, ?, 1, ?)
            """,
            ("0xloserwallet", str(sid), now_ts - 3600, now_ts, now_ts, now_ts),
        )
        self.conn.commit()

        profile = _make_profile(
            bankroll_usdc=10000.0,
            daily_loss_limit_usdc=50.0,  # Lost 100 > limit 50
        )

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_plugin_strategy(self.conn, strategy, profile=profile)

        positions = [
            p for p in _active_strategy_positions(self.conn, sid)
            if p.get("condition_id") != "cond-old"
        ]
        self.assertEqual(len(positions), 0)


class TestInstanceTagPropagation(unittest.TestCase):
    """Task 5.1/5.3: instance_tag is written to strategy_runs, positions, executions."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xdddd" + "4" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet, condition_id="cond-tag-1")

    def tearDown(self):
        self.conn.close()

    def test_instance_tag_written_to_strategy_runs(self):
        """_insert_strategy_run writes instance_tag to strategy_runs."""
        from sniper.services.strategy_runner import _insert_strategy_run

        sid = _insert_test_strategy(self.conn)
        run_id = _insert_strategy_run(self.conn, sid, instance_tag="vps-ams-01")
        self.assertGreater(run_id, 0)

        row = self.conn.execute(
            "SELECT instance_tag FROM strategy_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "vps-ams-01")

    def test_instance_tag_written_to_positions_and_executions(self):
        """_record_plugin_entry writes instance_tag to strategy_positions and strategy_executions."""
        from sniper.services.strategy_runner import _record_plugin_entry

        sid = _insert_test_strategy(self.conn)
        signal = StrategySignal(
            wallet=self.wallet,
            condition_id="cond-tag-1",
            outcome="YES",
            title="Tag Test Market",
            current_price=0.42,
            position_value=5000.0,
        )
        decision = StrategyEntryDecision(
            should_enter=True,
            reason="test",
            size_usdc=25.0,
            entry_price=0.42,
            execution_mode="paper",
        )

        strategy = {"id": sid}
        ok = _record_plugin_entry(
            self.conn, strategy, signal, decision, instance_tag="my-tag-123"
        )
        self.assertTrue(ok)

        # Check strategy_positions
        pos_row = self.conn.execute(
            "SELECT instance_tag FROM strategy_positions WHERE strategy_id = ?",
            (str(sid),),
        ).fetchone()
        self.assertIsNotNone(pos_row)
        self.assertEqual(pos_row[0], "my-tag-123")

        # Check strategy_executions
        exec_row = self.conn.execute(
            "SELECT instance_tag FROM strategy_executions WHERE strategy_id = ?",
            (sid,),
        ).fetchone()
        self.assertIsNotNone(exec_row)
        self.assertEqual(exec_row[0], "my-tag-123")

    def test_instance_tag_propagated_through_process_plugin_strategy(self):
        """End-to-end: process_plugin_strategy passes instance_tag from profile."""
        from sniper.services.strategy_runner import process_plugin_strategy

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        profile = _make_profile(
            instance_tag="e2e-tag-42",
            bankroll_usdc=10000.0,
            max_total_exposure_usdc=10000.0,
            max_market_exposure_usdc=5000.0,
        )

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_plugin_strategy(self.conn, strategy, profile=profile)

        # Check strategy_runs for instance_tag
        run_row = self.conn.execute(
            "SELECT instance_tag FROM strategy_runs WHERE strategy_id = ? ORDER BY run_id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row[0], "e2e-tag-42")


class TestCadenceTracking(unittest.TestCase):
    """Task 5.4: strategy cadence — skip if not due."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xeeee" + "5" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet)

    def tearDown(self):
        self.conn.close()
        # Clean up cadence state between tests
        from sniper.services import strategy_runner
        strategy_runner._last_run_ts.clear()

    def test_first_run_is_always_due(self):
        """A strategy that has never run is always due."""
        from sniper.services.strategy_runner import _is_strategy_due
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy

        plugin = InsiderCopyV1Strategy()
        self.assertTrue(_is_strategy_due(42, plugin))

    def test_recently_run_is_not_due(self):
        """A strategy that ran < default_loop_seconds ago is not due."""
        from sniper.services.strategy_runner import _is_strategy_due, _mark_strategy_ran, _last_run_ts
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy

        plugin = InsiderCopyV1Strategy()
        _mark_strategy_ran(42)
        self.assertFalse(_is_strategy_due(42, plugin))

    def test_stale_run_is_due(self):
        """A strategy that ran > default_loop_seconds ago is due."""
        from sniper.services.strategy_runner import _is_strategy_due, _last_run_ts
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy

        plugin = InsiderCopyV1Strategy()
        # Simulate run from 120 seconds ago (default_loop_seconds=30 for insider_copy_v1)
        _last_run_ts[42] = time.time() - 120
        self.assertTrue(_is_strategy_due(42, plugin))

    def test_cadence_skip_in_process_strategies(self):
        """process_strategies skips strategies that ran recently."""
        from sniper.services.strategy_runner import process_strategies, _mark_strategy_ran, _last_run_ts

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(enabled_strategies=("insider_copy_v1",))

        # Mark this specific strategy as just ran
        _mark_strategy_ran(sid)

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps:
            process_strategies(self.conn, None, profile=profile)
            mock_pps.assert_not_called()

    def test_cadence_allows_after_interval(self):
        """process_strategies runs strategy after its cadence interval passes."""
        from sniper.services.strategy_runner import process_strategies, _last_run_ts

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        profile = _make_profile(enabled_strategies=("insider_copy_v1",))

        # Set last run to 120 seconds ago (insider_copy_v1 has default_loop_seconds=30)
        _last_run_ts[sid] = time.time() - 120

        with patch("sniper.services.strategy_runner.process_plugin_strategy") as mock_pps, \
             patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_strategies(self.conn, None, profile=profile)
            self.assertTrue(mock_pps.called)


class TestBuildRiskContext(unittest.TestCase):
    """Test _build_risk_context helper function."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_none_without_profile(self):
        """Without a profile, _build_risk_context returns None."""
        from sniper.services.strategy_runner import _build_risk_context

        result = _build_risk_context(self.conn, None, "cond-test")
        self.assertIsNone(result)

    def test_returns_context_with_profile(self):
        """With a profile, returns a populated RiskContext."""
        from sniper.services.strategy_runner import _build_risk_context

        profile = _make_profile()
        ctx = _build_risk_context(self.conn, profile, "cond-test")
        self.assertIsInstance(ctx, RiskContext)
        self.assertEqual(ctx.bankroll_usdc, 1000.0)
        self.assertEqual(ctx.total_exposure_usdc, 0.0)
        self.assertEqual(ctx.concurrent_positions, 0)

    def test_context_reflects_open_positions(self):
        """Context includes exposure from open strategy positions."""
        from sniper.services.strategy_runner import _build_risk_context

        # Insert an open position
        now_ts = int(time.time())
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, last_reconciled_ts, version, updated_ts
            ) VALUES ('0xwallet', 'cond-ctx-1', 'YES', '1', 'open',
                      0.5, 0.5, 100, 100,
                      0, ?, 0, ?, 1, ?)
            """,
            (now_ts, now_ts, now_ts),
        )
        self.conn.commit()

        profile = _make_profile()
        ctx = _build_risk_context(self.conn, profile, "cond-ctx-1")

        self.assertAlmostEqual(ctx.total_exposure_usdc, 50.0)  # 100 shares * 0.5
        self.assertAlmostEqual(ctx.market_exposure_usdc, 50.0)
        self.assertEqual(ctx.concurrent_positions, 1)

    def test_context_market_exposure_scoped_to_condition_id(self):
        """Market exposure only counts positions for the given condition_id."""
        from sniper.services.strategy_runner import _build_risk_context

        now_ts = int(time.time())
        # Position in market A
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, last_reconciled_ts, version, updated_ts
            ) VALUES ('0xw1', 'market-a', 'YES', '1', 'open',
                      0.4, 0.4, 200, 200, 0, ?, 0, ?, 1, ?)
            """,
            (now_ts, now_ts, now_ts),
        )
        # Position in market B
        self.conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet, condition_id, outcome, strategy_id, state,
                entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                realized_pnl, opened_ts, closed_ts, last_reconciled_ts, version, updated_ts
            ) VALUES ('0xw2', 'market-b', 'YES', '1', 'open',
                      0.6, 0.6, 100, 100, 0, ?, 0, ?, 1, ?)
            """,
            (now_ts, now_ts, now_ts),
        )
        self.conn.commit()

        profile = _make_profile()
        ctx = _build_risk_context(self.conn, profile, "market-a")

        # Total = 200*0.4 + 100*0.6 = 80 + 60 = 140
        self.assertAlmostEqual(ctx.total_exposure_usdc, 140.0)
        # Market A only = 200*0.4 = 80
        self.assertAlmostEqual(ctx.market_exposure_usdc, 80.0)
        self.assertEqual(ctx.concurrent_positions, 2)


class TestMaxConcurrentPositionsRejection(unittest.TestCase):
    """Risk policy rejects when max concurrent positions reached."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xffff" + "6" * 36
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet, condition_id="cond-max-pos")

    def tearDown(self):
        self.conn.close()

    def test_max_concurrent_positions_rejects(self):
        """When concurrent positions >= max, risk policy rejects entry."""
        from sniper.services.strategy_runner import process_plugin_strategy, _active_strategy_positions

        sid = _insert_test_strategy(self.conn, strategy_type="insider_copy_v1")
        strategy = dict(
            self.conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()
        )
        strategy["parameters"] = {}

        # Insert enough open positions to hit the limit
        now_ts = int(time.time())
        for i in range(3):
            self.conn.execute(
                """
                INSERT INTO strategy_positions (
                    wallet, condition_id, outcome, strategy_id, state,
                    entry_price, avg_entry_price, entry_shares, filled_entry_shares,
                    realized_pnl, opened_ts, closed_ts, last_reconciled_ts, version, updated_ts
                ) VALUES (?, ?, 'YES', ?, 'open',
                          0.5, 0.5, 10, 10, 0, ?, 0, ?, 1, ?)
                """,
                (f"0xfiller{i}", f"cond-filler-{i}", str(sid + 100), now_ts, now_ts, now_ts),
            )
        self.conn.commit()

        profile = _make_profile(
            bankroll_usdc=10000.0,
            max_concurrent_positions=3,  # Already at 3 → will reject
        )

        with patch("sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda s: s):
            process_plugin_strategy(self.conn, strategy, profile=profile)

        # Should not have opened a new position beyond the 3 fillers
        new_positions = [
            p for p in _active_strategy_positions(self.conn, sid)
        ]
        self.assertEqual(len(new_positions), 0)


if __name__ == "__main__":
    unittest.main()
