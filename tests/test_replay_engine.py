"""Tests for replay engine and its curated data layer."""
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from db import (
    get_connection,
    get_replay_config,
    get_replay_run,
    init_db,
    list_replay_configs,
    list_replay_runs,
    replay_curated_signals,
    save_replay_config,
    save_replay_run,
)
from sniper.replay.adapters import InsiderCopyReplayAdapter
from sniper.analysis.replay_engine import (
    ReplayConfig,
    ReplayResult,
    _check_entry_gates,
    _compute_trade_pnl,
    _run_replay_loop,
    run_replay,
)


class TestReplayCuratedSignals(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, analyzed_ts,
                       insider=70, spec=20, entry_price=0.45, is_open=1):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test Market', ?, ?, 100.0, ?, ?, ?, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, is_open, entry_price, entry_price,
             insider, spec, anchor_ts, analyzed_ts),
        )

    # ── dedup & order ───────────────────────────────────────────────
    def test_basic_dedup_and_chronological_order(self):
        """First snapshot per (wallet, cond, outcome, anchor) wins, ordered by anchor_ts."""
        # Two snapshots of the same signal at different analyzed_ts
        self._insert_signal("0xA", "c1", "Yes", self.now - 200, self.now - 200, insider=70)
        self._insert_signal("0xA", "c1", "Yes", self.now - 200, self.now - 100, insider=85)

        # A second, later signal
        self._insert_signal("0xA", "c2", "No", self.now - 50, self.now - 50, insider=60)

        rows = replay_curated_signals(self.conn)

        # Dedup: only 2 unique signals
        self.assertEqual(len(rows), 2)
        # First snapshot wins (insider=70, not 85)
        self.assertEqual(rows[0]["position_insider_score"], 70)
        # Chronological order by anchor_ts
        self.assertLess(rows[0]["entry_anchor_ts"], rows[1]["entry_anchor_ts"])

    # ── exclusion filters ───────────────────────────────────────────
    def test_excludes_zero_anchor_and_zero_price(self):
        """entry_anchor_ts=0 or entry_price<=0 are excluded."""
        # Zero anchor — should be excluded
        self._insert_signal("0xA", "c1", "Yes", 0, self.now, insider=80)
        # Zero entry_price — should be excluded
        self._insert_signal("0xA", "c2", "Yes", self.now - 10, self.now, entry_price=0.0)
        # Valid signal — should be included
        self._insert_signal("0xA", "c3", "Yes", self.now - 5, self.now, entry_price=0.35)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["condition_id"], "c3")

    # ── date range filter ───────────────────────────────────────────
    def test_date_range_filter(self):
        """start_ts/end_ts bounds on entry_anchor_ts."""
        self._insert_signal("0xA", "c1", "Yes", 1000, 1000)
        self._insert_signal("0xA", "c2", "Yes", 2000, 2000)
        self._insert_signal("0xA", "c3", "Yes", 3000, 3000)

        # Only the middle signal
        rows = replay_curated_signals(self.conn, start_ts=1500, end_ts=2500)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["condition_id"], "c2")

        # start_ts only
        rows = replay_curated_signals(self.conn, start_ts=1500)
        self.assertEqual(len(rows), 2)

        # end_ts only
        rows = replay_curated_signals(self.conn, end_ts=2500)
        self.assertEqual(len(rows), 2)

    # ── column completeness ─────────────────────────────────────────
    def test_returns_all_required_columns(self):
        """Check for all required columns in output."""
        self._insert_signal("0xA", "c1", "Yes", self.now - 100, self.now)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)

        required = {
            "wallet", "condition_id", "outcome", "title",
            "entry_price", "entry_notional",
            "position_insider_score", "position_speculator_score",
            "position_classification", "entry_anchor_ts", "is_open",
            "resolved_price", "resolved_at",
        }
        self.assertTrue(required.issubset(set(rows[0].keys())),
                        f"Missing columns: {required - set(rows[0].keys())}")

    def test_dedup_normalizes_outcome_case(self):
        """Mixed-case legacy outcomes collapse to one replay entry."""
        self._insert_signal("0xA", "c1", "YES", 1000, 1000, insider=70)
        self._insert_signal("0xA", "c1", "yes", 1000, 1100, insider=85)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_insider_score"], 70)


class TestReplayCuratedResolution(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, analyzed_ts,
                       insider=70, spec=20, entry_price=0.45, is_open=1):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test Market', ?, ?, 100.0, ?, ?, ?, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, is_open, entry_price, entry_price,
             insider, spec, anchor_ts, analyzed_ts),
        )

    def _insert_outcome(self, cond, outcome, resolved_price, resolved_at=0):
        self.conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES (?, ?, ?, ?)""",
            (cond, outcome.lower(), resolved_price, resolved_at),
        )

    # ── resolved signal ─────────────────────────────────────────────
    def test_resolved_signal_has_resolved_price(self):
        """JOIN returns resolved_price from market_outcomes."""
        self._insert_signal("0xA", "c1", "Yes", self.now - 100, self.now)
        self._insert_outcome("c1", "Yes", 1.0, resolved_at=1234)

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolved_price"], 1.0)
        self.assertEqual(rows[0]["resolved_at"], 1234)

    # ── unresolved signal ────────────────────────────────────────────
    def test_unresolved_signal_has_null_resolved_price(self):
        """LEFT JOIN returns None when no outcome exists."""
        self._insert_signal("0xA", "c1", "Yes", self.now - 100, self.now)
        # No market_outcomes row inserted

        rows = replay_curated_signals(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["resolved_price"])


# ===================================================================
# Task 3: ReplayConfig and ReplayResult dataclasses
# ===================================================================


class TestReplayConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = ReplayConfig()
        self.assertEqual(cfg.strategy_type, "insider_copy_v1")
        self.assertEqual(cfg.initial_bankroll, 1000.0)
        self.assertEqual(cfg.base_size_usdc, 25.0)
        self.assertEqual(cfg.max_concurrent_positions, 20)
        self.assertEqual(cfg.max_exposure_per_market_usdc, 200.0)
        self.assertEqual(cfg.max_total_exposure_usdc, 1000.0)
        self.assertEqual(cfg.min_insider_score, 55.0)
        self.assertEqual(cfg.max_speculator_score, 70.0)
        self.assertEqual(cfg.max_entry_price, 0.70)
        self.assertIsNone(cfg.circuit_breaker_max_consecutive_losses)
        self.assertIsNone(cfg.circuit_breaker_max_drawdown_pct)

    def test_config_to_dict_roundtrip(self):
        cfg = ReplayConfig(strategy_type="insider_copy_v1", initial_bankroll=500.0, min_insider_score=70)
        d = cfg.to_dict()
        self.assertEqual(d["strategy_type"], "insider_copy_v1")
        self.assertEqual(d["initial_bankroll"], 500.0)
        self.assertEqual(d["min_insider_score"], 70)
        cfg2 = ReplayConfig.from_dict(d)
        self.assertEqual(cfg2.strategy_type, "insider_copy_v1")
        self.assertEqual(cfg2.initial_bankroll, 500.0)
        self.assertEqual(cfg2.min_insider_score, 70)


class TestReplayResult(unittest.TestCase):
    def test_empty_result(self):
        result = ReplayResult(
            config=ReplayConfig(), trades=[], skipped=[], equity_curve=[], summary={},
        )
        self.assertEqual(result.trades, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.equity_curve, [])


# ===================================================================
# Task 4: Entry gate logic
# ===================================================================


def _make_signal(**overrides):
    """Helper to build a signal dict with sensible defaults."""
    sig = {
        "wallet": "0xA",
        "condition_id": "c1",
        "outcome": "Yes",
        "title": "Test Market",
        "entry_price": 0.40,
        "entry_notional": 100.0,
        "position_insider_score": 70,
        "position_speculator_score": 20,
        "position_classification": "insider_candidate",
        "entry_anchor_ts": 1000,
        "is_open": 1,
        "resolved_price": 1.0,
        "resolved_at": 1100,
    }
    sig.update(overrides)
    return sig


class TestEntryGates(unittest.TestCase):
    def setUp(self):
        self.cfg = ReplayConfig()
        self.gate_kwargs = {
            "open_positions": {},
            "concurrent_count": 0,
            "total_exposure": 0.0,
            "current_bankroll": 1000.0,
        }

    def test_passes_all_gates(self):
        sig = _make_signal()
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertIsNone(reason)

    def test_rejects_low_insider(self):
        sig = _make_signal(position_insider_score=40)
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertEqual(reason, "insider_score_below_min")

    def test_rejects_high_speculator(self):
        sig = _make_signal(position_speculator_score=75)
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertEqual(reason, "speculator_score_above_max")

    def test_rejects_high_price(self):
        sig = _make_signal(entry_price=0.85)
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertEqual(reason, "entry_price_above_max")

    def test_rejects_skip_classification(self):
        sig = _make_signal(position_classification="arb_fund")
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertEqual(reason, "classification_skipped")

    def test_rejects_max_concurrent(self):
        sig = _make_signal()
        kwargs = dict(self.gate_kwargs, concurrent_count=20)
        reason = _check_entry_gates(sig, self.cfg, **kwargs)
        self.assertEqual(reason, "max_concurrent_positions")

    def test_rejects_total_exposure(self):
        sig = _make_signal()
        kwargs = dict(self.gate_kwargs, total_exposure=990.0)
        reason = _check_entry_gates(sig, self.cfg, **kwargs)
        self.assertEqual(reason, "total_exposure_exceeded")

    def test_rejects_market_exposure(self):
        sig = _make_signal()
        kwargs = dict(self.gate_kwargs, open_positions={"c1": 190.0})
        reason = _check_entry_gates(sig, self.cfg, **kwargs)
        self.assertEqual(reason, "market_exposure_exceeded")

    def test_rejects_unresolved(self):
        sig = _make_signal(resolved_price=None)
        reason = _check_entry_gates(sig, self.cfg, **self.gate_kwargs)
        self.assertEqual(reason, "unresolved_market")

    def test_rejects_bankroll_depleted(self):
        sig = _make_signal()
        kwargs = dict(self.gate_kwargs, current_bankroll=10.0)
        reason = _check_entry_gates(sig, self.cfg, **kwargs)
        self.assertEqual(reason, "bankroll_depleted")


# ===================================================================
# Task 5: Core replay loop
# ===================================================================


class TestRunReplay(unittest.TestCase):
    def test_simple_winning_trade(self):
        """entry=0.40, resolved=1.0 -> ROI=1.5, PnL=$37.5"""
        signals = [_make_signal(entry_price=0.40, resolved_price=1.0)]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertAlmostEqual(t["roi"], 1.5)
        self.assertAlmostEqual(t["pnl_usdc"], 37.5)
        self.assertAlmostEqual(result.summary["final_bankroll"], 1037.5)

    def test_simple_losing_trade(self):
        """entry=0.40, resolved=0.0 -> ROI=-1.0, PnL=-$25"""
        signals = [_make_signal(entry_price=0.40, resolved_price=0.0)]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertAlmostEqual(t["roi"], -1.0)
        self.assertAlmostEqual(t["pnl_usdc"], -25.0)
        self.assertAlmostEqual(result.summary["final_bankroll"], 975.0)

    def test_gate_rejection_tracked(self):
        """insider=40 < min=55 -> skipped."""
        signals = [_make_signal(position_insider_score=40)]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "insider_score_below_min")

    def test_equity_curve_tracks_bankroll(self):
        """2 trades: win then loss -> curve tracks correctly."""
        signals = [
            _make_signal(
                condition_id="c1",
                entry_price=0.40,
                resolved_price=1.0,
                entry_anchor_ts=100,
                resolved_at=150,
            ),
            _make_signal(
                condition_id="c2",
                entry_price=0.40,
                resolved_price=0.0,
                entry_anchor_ts=200,
                resolved_at=250,
            ),
        ]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.equity_curve), 3)  # initial + 2 trades
        self.assertAlmostEqual(result.equity_curve[0]["bankroll"], 1000.0)
        self.assertAlmostEqual(result.equity_curve[1]["bankroll"], 1037.5)
        self.assertAlmostEqual(result.equity_curve[2]["bankroll"], 1012.5)

    def test_circuit_breaker_consecutive_losses(self):
        """5 signals all lose, breaker at 3 -> 3 trades, 2 skipped."""
        signals = [
            _make_signal(
                condition_id=f"c{i}",
                entry_price=0.40,
                resolved_price=0.0,
                entry_anchor_ts=i * 100,
                resolved_at=(i * 100) + 50,
            )
            for i in range(5)
        ]
        cfg = ReplayConfig(circuit_breaker_max_consecutive_losses=3)
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 3)
        self.assertEqual(len(result.skipped), 2)
        for s in result.skipped:
            self.assertEqual(s["skip_reason"], "circuit_breaker_consecutive_losses")

    def test_circuit_breaker_drawdown(self):
        """5 signals all lose at $25, breaker at 8%, bankroll=$1000.
        3 losses = $75 = 7.5% dd -> still under 8%.
        4 losses = $100 = 10% dd from peak -> trade 5 skipped (dd >= 8% after 4th loss).
        """
        signals = [
            _make_signal(
                condition_id=f"c{i}",
                entry_price=0.40,
                resolved_price=0.0,
                entry_anchor_ts=i * 100,
                resolved_at=(i * 100) + 50,
            )
            for i in range(5)
        ]
        cfg = ReplayConfig(circuit_breaker_max_drawdown_pct=0.08)
        result = _run_replay_loop(signals, cfg)
        # After 3 losses: bankroll=925, dd=75/1000=7.5% < 8% -> trade 4 proceeds
        # After 4 losses: bankroll=900, dd=100/1000=10% >= 8% -> trade 5 skipped
        self.assertEqual(len(result.trades), 4)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "circuit_breaker_max_drawdown")

    def test_no_signals_returns_empty_result(self):
        """0 signals -> summary.total_trades=0, final_bankroll=1000."""
        cfg = ReplayConfig()
        result = _run_replay_loop([], cfg)
        self.assertEqual(result.summary["total_trades"], 0)
        self.assertAlmostEqual(result.summary["final_bankroll"], 1000.0)
        self.assertEqual(len(result.equity_curve), 1)  # just initial

    def test_max_concurrent_positions_skips_overlapping_signal(self):
        """Overlapping positions respect the configured concurrency cap."""
        signals = [
            _make_signal(
                condition_id=f"c{i}",
                entry_price=0.40,
                resolved_price=1.0,
                entry_anchor_ts=100 + i,
                resolved_at=1000,
            )
            for i in range(3)
        ]
        cfg = ReplayConfig(max_concurrent_positions=2)
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "max_concurrent_positions")

    def test_total_exposure_cap_skips_overlapping_signal(self):
        """Total exposure cap is enforced while earlier positions remain open."""
        signals = [
            _make_signal(
                condition_id=f"c{i}",
                entry_price=0.40,
                resolved_price=1.0,
                entry_anchor_ts=100 + i,
                resolved_at=1000,
            )
            for i in range(3)
        ]
        cfg = ReplayConfig(max_total_exposure_usdc=50.0)
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "total_exposure_exceeded")

    def test_market_exposure_cap_skips_same_market_signal(self):
        """Per-market exposure cap is enforced on overlapping entries."""
        signals = [
            _make_signal(
                condition_id="same-market",
                outcome="Yes" if i == 0 else "No",
                entry_price=0.40,
                resolved_price=1.0,
                entry_anchor_ts=100 + i,
                resolved_at=1000,
            )
            for i in range(2)
        ]
        cfg = ReplayConfig(max_exposure_per_market_usdc=40.0)
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "market_exposure_exceeded")

    def test_resolved_before_entry_is_skipped(self):
        """Signals whose markets resolved before entry are invalid replay inputs."""
        signals = [
            _make_signal(
                condition_id="c1",
                entry_anchor_ts=1000,
                resolved_at=900,
                resolved_price=1.0,
            )
        ]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "resolved_before_entry")

    def test_missing_resolution_timestamp_is_skipped(self):
        """Signals with resolved_at=0 get a distinct skip reason."""
        signals = [
            _make_signal(
                condition_id="c1",
                entry_anchor_ts=1000,
                resolved_at=0,
                resolved_price=1.0,
            )
        ]
        cfg = ReplayConfig()
        result = _run_replay_loop(signals, cfg)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["skip_reason"], "missing_resolution_timestamp")


# ===================================================================
# Task 6: ReplayResult.to_dict() serialization
# ===================================================================


class TestReplayResultSerialization(unittest.TestCase):
    def test_to_dict_is_json_serializable(self):
        result = ReplayResult(
            config=ReplayConfig(),
            trades=[{"wallet": "0xA", "pnl_usdc": 10.0}],
            skipped=[],
            equity_curve=[{"ts": 0, "bankroll": 1000.0}],
            summary={"total_trades": 1},
        )
        d = result.to_dict()
        serialized = json.dumps(d)
        self.assertIn('"total_trades"', serialized)
        self.assertIn('"initial_bankroll"', serialized)


# ===================================================================
# Task 7: Replay tables in init_db
# ===================================================================


class TestReplayTables(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_replay_configs_table_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_configs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_replay_runs_table_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='replay_runs'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_replay_configs_columns(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(replay_configs)")}
        self.assertTrue({"id", "name", "config_json", "notes", "created_at"}.issubset(cols))

    def test_replay_runs_columns(self):
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(replay_runs)")}
        self.assertTrue(
            {"id", "config_id", "config_json", "summary_json", "result_json",
             "duration_ms", "created_at"}.issubset(cols)
        )


# ===================================================================
# Task 8: DB helper persistence tests
# ===================================================================


class TestReplayConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_and_get_config(self):
        cfg = {"initial_bankroll": 500, "min_insider_score": 60}
        config_id = save_replay_config(self.conn, name="test-cfg",
                                       config_json=cfg, notes="test run")
        self.assertIsInstance(config_id, int)
        row = get_replay_config(self.conn, config_id)
        self.assertEqual(row["name"], "test-cfg")
        self.assertEqual(json.loads(row["config_json"])["initial_bankroll"], 500)

    def test_list_configs(self):
        save_replay_config(self.conn, name="cfg-a", config_json={})
        save_replay_config(self.conn, name="cfg-b", config_json={})
        configs = list_replay_configs(self.conn)
        self.assertEqual(len(configs), 2)

    def test_duplicate_name_raises(self):
        save_replay_config(self.conn, name="dup", config_json={})
        with self.assertRaises(Exception):
            save_replay_config(self.conn, name="dup", config_json={})


class TestReplayRunPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_and_get_run(self):
        cfg_id = save_replay_config(self.conn, name="run-cfg", config_json={"a": 1})
        run_id = save_replay_run(
            self.conn, config_id=cfg_id, config_json={"a": 1},
            summary_json={"total_trades": 5}, result_json={"trades": []},
            signal_count=100, trade_count=5, duration_ms=1234,
        )
        self.assertIsInstance(run_id, int)
        row = get_replay_run(self.conn, run_id)
        self.assertEqual(json.loads(row["summary_json"])["total_trades"], 5)
        self.assertEqual(row["duration_ms"], 1234)

    def test_list_runs_for_config(self):
        cfg_id = save_replay_config(self.conn, name="list-cfg", config_json={})
        save_replay_run(self.conn, config_id=cfg_id, config_json={},
                        summary_json={}, result_json={}, signal_count=0,
                        trade_count=0, duration_ms=100)
        save_replay_run(self.conn, config_id=cfg_id, config_json={},
                        summary_json={}, result_json={}, signal_count=0,
                        trade_count=0, duration_ms=200)
        runs = list_replay_runs(self.conn, config_id=cfg_id)
        self.assertEqual(len(runs), 2)

    def test_save_run_without_config(self):
        run_id = save_replay_run(
            self.conn, config_id=None, config_json={"inline": True},
            summary_json={"total_trades": 0}, result_json={},
            signal_count=0, trade_count=0, duration_ms=50,
        )
        row = get_replay_run(self.conn, run_id)
        self.assertIsNone(row["config_id"])

    def test_replay_config_and_run_capture_instance_tag(self):
        with patch.dict(os.environ, {"INSTANCE_TAG": "replay-test-vps"}):
            cfg_id = save_replay_config(self.conn, name="tagged-cfg", config_json={"x": 1})
            run_id = save_replay_run(
                self.conn,
                config_id=cfg_id,
                config_json={"x": 1},
                summary_json={"total_trades": 1},
                result_json={"trades": []},
                signal_count=1,
                trade_count=1,
                duration_ms=10,
            )
        cfg = get_replay_config(self.conn, cfg_id)
        run = get_replay_run(self.conn, run_id)
        self.assertEqual(cfg["instance_tag"], "replay-test-vps")
        self.assertEqual(run["instance_tag"], "replay-test-vps")
        listed_cfg = list_replay_configs(self.conn)[0]
        listed_run = list_replay_runs(self.conn, config_id=cfg_id)[0]
        self.assertEqual(listed_cfg["instance_tag"], "replay-test-vps")
        self.assertEqual(listed_run["instance_tag"], "replay-test-vps")


# ===================================================================
# Task 12: CLI argument parsing
# ===================================================================


from sniper.analysis.replay_engine import _parse_cli_args


class TestReplayCLI(unittest.TestCase):
    def test_default_args(self):
        args = _parse_cli_args([])
        self.assertEqual(args.bankroll, 1000.0)
        self.assertEqual(args.size, 25.0)
        self.assertEqual(args.min_insider, 55.0)
        self.assertEqual(args.max_spec, 70.0)
        self.assertEqual(args.max_price, 0.70)
        self.assertEqual(args.max_concurrent, 20)
        self.assertEqual(args.max_market_exposure, 200.0)
        self.assertEqual(args.max_total_exposure, 1000.0)
        self.assertIsNone(args.cb_losses)
        self.assertIsNone(args.cb_drawdown)
        self.assertIsNone(args.config_file)
        self.assertFalse(args.json_output)
        self.assertIsNone(args.save_as)
        self.assertIsNone(args.db)

    def test_custom_args(self):
        args = _parse_cli_args([
            "--bankroll", "500", "--min-insider", "70",
            "--max-price", "0.60", "--max-concurrent", "10",
            "--size", "50", "--max-spec", "80",
            "--max-market-exposure", "300", "--max-total-exposure", "1500",
        ])
        self.assertEqual(args.bankroll, 500.0)
        self.assertEqual(args.min_insider, 70.0)
        self.assertEqual(args.max_price, 0.60)
        self.assertEqual(args.max_concurrent, 10)
        self.assertEqual(args.size, 50.0)
        self.assertEqual(args.max_spec, 80.0)
        self.assertEqual(args.max_market_exposure, 300.0)
        self.assertEqual(args.max_total_exposure, 1500.0)

    def test_config_file_arg(self):
        args = _parse_cli_args(["--config", "my_config.json"])
        self.assertEqual(args.config_file, "my_config.json")

    def test_output_json_flag(self):
        args = _parse_cli_args(["--json"])
        self.assertTrue(args.json_output)

    def test_save_as_arg(self):
        args = _parse_cli_args(["--save-as", "my-run"])
        self.assertEqual(args.save_as, "my-run")

    def test_circuit_breaker_args(self):
        args = _parse_cli_args(["--cb-losses", "5", "--cb-drawdown", "0.15"])
        self.assertEqual(args.cb_losses, 5)
        self.assertEqual(args.cb_drawdown, 0.15)

    def test_date_range_args(self):
        args = _parse_cli_args(["--start-ts", "1000", "--end-ts", "2000"])
        self.assertEqual(args.start_ts, 1000)
        self.assertEqual(args.end_ts, 2000)

    def test_db_arg(self):
        args = _parse_cli_args(["--db", "/tmp/test.db"])
        self.assertEqual(args.db, "/tmp/test.db")

    def test_strategy_type_arg(self):
        args = _parse_cli_args(["--strategy-type", "weather_v1"])
        self.assertEqual(args.strategy_type, "weather_v1")


# ===================================================================
# Task 13: MCP replay tool
# ===================================================================


class TestMCPReplayTool(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _insert_signal(self, wallet, cond, outcome, anchor_ts, insider=70, entry_price=0.45):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES (?, ?, ?, 'Test', 0, ?, 100, ?, ?, 20, 'insider_candidate',
                       'holding', 10, ?, ?)""",
            (wallet, cond, outcome, entry_price, entry_price, insider, anchor_ts, anchor_ts),
        )

    def _insert_outcome(self, cond, outcome, resolved_price, resolved_at=2000):
        self.conn.execute(
            """INSERT OR IGNORE INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES (?, ?, ?, ?)""",
            (cond, outcome, resolved_price, resolved_at),
        )

    def test_replay_tool_returns_envelope(self):
        from sniper.mcp_tools.replay_tools import replay_impl
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000)
        self._insert_outcome("c1", "yes", 1.0)
        result = replay_impl(initial_bankroll=1000, min_insider_score=55, conn=self.conn)
        self.assertIn("generated_at", result)
        self.assertIn("data_source", result)
        self.assertEqual(result["data_source"], "signal_history_curated")
        self.assertIn("bias_warning", result)
        self.assertIsNotNone(result["bias_warning"])
        self.assertIn("data", result)
        data = result["data"]
        self.assertIn("summary", data)
        self.assertIn("total_trades", data["summary"])
        self.assertIn("config", data)
        self.assertIn("trade_count", data)
        self.assertIn("equity_curve_points", data)
        self.assertIn("skip_reason_counts", data)

    def test_replay_tool_respects_gates(self):
        from sniper.mcp_tools.replay_tools import replay_impl
        # Insert signal with insider=40 (below default min of 55)
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, insider=40)
        self._insert_outcome("c1", "yes", 1.0)
        result = replay_impl(min_insider_score=55, conn=self.conn)
        self.assertEqual(result["data"]["summary"]["total_trades"], 0)
        self.assertIn("insider_score_below_min", result["data"]["skip_reason_counts"])

    def test_replay_tool_executes_trade(self):
        from sniper.mcp_tools.replay_tools import replay_impl
        self._insert_signal("0xA", "c1", "Yes", anchor_ts=1000, insider=70, entry_price=0.40)
        self._insert_outcome("c1", "yes", 1.0)
        result = replay_impl(initial_bankroll=1000, min_insider_score=55, conn=self.conn)
        self.assertEqual(result["data"]["summary"]["total_trades"], 1)
        self.assertEqual(result["data"]["summary"]["wins"], 1)
        self.assertGreater(result["data"]["summary"]["total_pnl_usdc"], 0)

    def test_replay_tool_skip_reason_counts(self):
        from sniper.mcp_tools.replay_tools import _count_skip_reasons
        skipped = [
            {"skip_reason": "insider_score_below_min"},
            {"skip_reason": "insider_score_below_min"},
            {"skip_reason": "entry_price_above_max"},
        ]
        counts = _count_skip_reasons(skipped)
        self.assertEqual(counts["insider_score_below_min"], 2)
        self.assertEqual(counts["entry_price_above_max"], 1)

    def test_replay_tool_rejects_unsupported_strategy_type(self):
        from sniper.mcp_tools.replay_tools import replay_impl
        result = replay_impl(strategy_type="weather_v1", conn=self.conn)
        self.assertEqual(result["data"]["error"], "Replay not supported for strategy_type 'weather_v1'")


class TestReplayAdapterRouting(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES ('0xA', 'c1', 'YES', 'Test', 0, 0.40, 100, 0.40,
                       70, 20, 'insider_candidate', 'holding', 10, 1000, 1000)""",
        )
        self.conn.execute(
            """INSERT INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES ('c1', 'yes', 1.0, 2000)""",
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_insider_copy_adapter_fetches_curated_candidates(self):
        adapter = InsiderCopyReplayAdapter()
        candidates = adapter.get_candidates(self.conn, {}, 0, 3000)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].condition_id, "c1")
        self.assertEqual(candidates[0].metadata["resolved_at"], 2000)

    def test_run_replay_rejects_unknown_strategy_type(self):
        with self.assertRaises(ValueError):
            run_replay(self.conn, ReplayConfig(strategy_type="does_not_exist"))

    def test_run_replay_rejects_weather_until_adapter_exists(self):
        with self.assertRaises(ValueError):
            run_replay(self.conn, ReplayConfig(strategy_type="weather_v1"))


if __name__ == "__main__":
    unittest.main()
