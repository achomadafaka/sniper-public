"""Tests for Chunk 4: Strategy Plugin Contract Extension + Replay Adapter.

Covers:
- New StrategyPlugin ABC attributes have correct defaults
- validate_parameters / replay_adapter default behaviour
- InsiderCopyV1Strategy and FadeV1Strategy declare correct attributes
- ReplayCandidate dataclass instantiation
- StrategyReplayAdapter is abstract (cannot instantiate directly)
- InsiderCopyReplayAdapter behavior and registry resolution
- ReplayLifecycleState enum values
"""
from __future__ import annotations

import pytest

from db import get_connection, init_db
from sniper.strategies.base import StrategyPlugin
from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
from sniper.strategies.fade_v1 import FadeV1Strategy
from sniper.strategies.weather_v1 import WeatherV1Strategy
from sniper.replay.adapters import (
    InsiderCopyReplayAdapter,
    ReplayCandidate,
    ReplayLifecycleState,
    StrategyReplayAdapter,
    get_replay_adapter_for_strategy,
)


# ---------------------------------------------------------------------------
# Helper: minimal concrete subclass to test ABC defaults
# ---------------------------------------------------------------------------

class _MinimalPlugin(StrategyPlugin):
    """Bare-minimum concrete subclass — only abstract methods implemented."""

    plugin_id = "test_minimal"

    def discover(self, conn, strategy):
        return []

    def enter(self, conn, strategy, signal, open_positions):
        raise NotImplementedError

    def manage(self, conn, strategy, position):
        raise NotImplementedError

    def exit(self, conn, strategy, position, state):
        raise NotImplementedError


# ===================================================================
# StrategyPlugin ABC defaults
# ===================================================================

class TestStrategyPluginDefaults:
    """New attributes should have sensible defaults so existing plugins
    continue to work without changes."""

    def setup_method(self):
        self.plugin = _MinimalPlugin()

    def test_strategy_family_default(self):
        assert self.plugin.strategy_family == ""

    def test_supported_execution_modes_default(self):
        assert self.plugin.supported_execution_modes == ("paper",)

    def test_required_feeds_default(self):
        assert self.plugin.required_feeds == ()

    def test_required_tables_default(self):
        assert self.plugin.required_tables == ()

    def test_default_loop_seconds_default(self):
        assert self.plugin.default_loop_seconds == 60

    def test_validate_parameters_returns_empty_list(self):
        errors = self.plugin.validate_parameters({"anything": "goes"})
        assert errors == []
        assert isinstance(errors, list)

    def test_validate_parameters_empty_dict(self):
        assert self.plugin.validate_parameters({}) == []

    def test_replay_adapter_returns_none(self):
        assert self.plugin.replay_adapter() is None


# ===================================================================
# InsiderCopyV1Strategy attributes
# ===================================================================

class TestInsiderCopyV1Attributes:

    def setup_method(self):
        self.plugin = InsiderCopyV1Strategy()

    def test_plugin_id(self):
        assert self.plugin.plugin_id == "insider_copy_v1"

    def test_strategy_family(self):
        assert self.plugin.strategy_family == "insider_copy"

    def test_supported_execution_modes(self):
        assert self.plugin.supported_execution_modes == ("paper", "real")

    def test_required_feeds(self):
        assert self.plugin.required_feeds == ("polymarket",)

    def test_default_loop_seconds(self):
        assert self.plugin.default_loop_seconds == 30

    def test_validate_parameters_default(self):
        assert self.plugin.validate_parameters({}) == []

    def test_replay_adapter_returns_insider_copy_adapter(self):
        assert isinstance(self.plugin.replay_adapter(), InsiderCopyReplayAdapter)


# ===================================================================
# FadeV1Strategy attributes
# ===================================================================

class TestFadeV1Attributes:

    def setup_method(self):
        self.plugin = FadeV1Strategy()

    def test_plugin_id(self):
        assert self.plugin.plugin_id == "fade_v1"

    def test_strategy_family(self):
        assert self.plugin.strategy_family == "fade"

    def test_supported_execution_modes(self):
        assert self.plugin.supported_execution_modes == ("paper",)

    def test_required_feeds(self):
        assert self.plugin.required_feeds == ("polymarket",)

    def test_default_loop_seconds(self):
        assert self.plugin.default_loop_seconds == 30

    def test_validate_parameters_default(self):
        assert self.plugin.validate_parameters({}) == []

    def test_replay_adapter_default(self):
        assert self.plugin.replay_adapter() is None


# ===================================================================
# ReplayCandidate dataclass
# ===================================================================

class TestReplayCandidate:

    def test_basic_instantiation(self):
        c = ReplayCandidate(
            condition_id="0xabc",
            outcome="YES",
            wallet="0x123",
            entry_price=0.45,
            entry_ts=1700000000,
        )
        assert c.condition_id == "0xabc"
        assert c.outcome == "YES"
        assert c.wallet == "0x123"
        assert c.entry_price == 0.45
        assert c.entry_ts == 1700000000
        assert c.metadata == {}

    def test_with_metadata(self):
        meta = {"insider_score": 85, "title": "Test Market"}
        c = ReplayCandidate(
            condition_id="0xdef",
            outcome="NO",
            wallet="0x456",
            entry_price=0.30,
            entry_ts=1700000100,
            metadata=meta,
        )
        assert c.metadata["insider_score"] == 85
        assert c.metadata["title"] == "Test Market"

    def test_metadata_default_is_independent(self):
        """Each instance should get its own metadata dict."""
        c1 = ReplayCandidate("a", "YES", "w1", 0.5, 100)
        c2 = ReplayCandidate("b", "NO", "w2", 0.6, 200)
        c1.metadata["x"] = 1
        assert "x" not in c2.metadata


# ===================================================================
# ReplayLifecycleState enum
# ===================================================================

class TestReplayLifecycleState:

    def test_values(self):
        assert ReplayLifecycleState.ACTIVE == "active"
        assert ReplayLifecycleState.WON == "won"
        assert ReplayLifecycleState.LOST == "lost"
        assert ReplayLifecycleState.EXITED == "exited"

    def test_is_str_subclass(self):
        assert isinstance(ReplayLifecycleState.ACTIVE, str)

    def test_member_count(self):
        assert len(ReplayLifecycleState) == 4


# ===================================================================
# StrategyReplayAdapter (abstract — cannot instantiate)
# ===================================================================

class TestStrategyReplayAdapterABC:

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            StrategyReplayAdapter()

    def test_strategy_type_default(self):
        assert StrategyReplayAdapter.strategy_type == ""


# ===================================================================
# InsiderCopyReplayAdapter
# ===================================================================

class TestInsiderCopyReplayAdapter:

    def setup_method(self):
        self.adapter = InsiderCopyReplayAdapter()
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.candidate = ReplayCandidate(
            condition_id="0xabc",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
        )

    def teardown_method(self):
        self.conn.close()

    def test_strategy_type(self):
        assert self.adapter.strategy_type == "insider_copy_v1"

    def test_get_candidates_returns_curated_signal_entries(self):
        self.conn.execute(
            """INSERT INTO signal_history
               (wallet, condition_id, outcome, title, is_open, entry_price,
                entry_notional, current_price, position_insider_score,
                position_speculator_score, position_classification,
                activity_state, hold_hours, entry_anchor_ts, analyzed_ts)
               VALUES ('0x123', '0xabc', 'YES', 'Test Market', 0, 0.50, 100, 0.50,
                       70, 20, 'insider_candidate', 'holding', 10, 1700000000, 1700000000)"""
        )
        self.conn.execute(
            """INSERT INTO market_outcomes
               (condition_id, outcome, resolved_price, resolved_at)
               VALUES ('0xabc', 'yes', 1.0, 1700001000)"""
        )
        self.conn.commit()

        candidates = self.adapter.get_candidates(self.conn, {}, 1699999999, 1700000001)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.condition_id == "0xabc"
        assert candidate.wallet == "0x123"
        assert candidate.metadata["resolved_at"] == 1700001000

    def test_get_lifecycle_state_returns_active_before_resolution(self):
        candidate = ReplayCandidate(
            condition_id="0xabc",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
            metadata={"resolved_price": 1.0, "resolved_at": 1700001000},
        )
        assert (
            self.adapter.get_lifecycle_state(self.conn, candidate, 1700000500)
            == ReplayLifecycleState.ACTIVE
        )

    def test_get_lifecycle_state_returns_terminal_state_at_resolution(self):
        won = ReplayCandidate(
            condition_id="0xabc",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
            metadata={"resolved_price": 1.0, "resolved_at": 1700001000},
        )
        lost = ReplayCandidate(
            condition_id="0xdef",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
            metadata={"resolved_price": 0.0, "resolved_at": 1700001000},
        )
        exited = ReplayCandidate(
            condition_id="0xghi",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
            metadata={"resolved_price": 0.42, "resolved_at": 1700001000},
        )
        assert (
            self.adapter.get_lifecycle_state(self.conn, won, 1700001000)
            == ReplayLifecycleState.WON
        )
        assert (
            self.adapter.get_lifecycle_state(self.conn, lost, 1700001000)
            == ReplayLifecycleState.LOST
        )
        assert (
            self.adapter.get_lifecycle_state(self.conn, exited, 1700001000)
            == ReplayLifecycleState.EXITED
        )

    def test_get_exit_price_returns_resolved_price(self):
        candidate = ReplayCandidate(
            condition_id="0xabc",
            outcome="YES",
            wallet="0x123",
            entry_price=0.50,
            entry_ts=1700000000,
            metadata={"resolved_price": 0.42, "resolved_at": 1700001000},
        )
        assert self.adapter.get_exit_price(self.conn, candidate, 1700001000) == 0.42

    def test_is_subclass_of_adapter(self):
        assert isinstance(self.adapter, StrategyReplayAdapter)


class TestReplayAdapterRegistry:

    def test_registry_returns_insider_copy_adapter(self):
        adapter = get_replay_adapter_for_strategy("insider_copy_v1")
        assert isinstance(adapter, InsiderCopyReplayAdapter)

    def test_registry_returns_none_for_weather_until_supported(self):
        plugin = WeatherV1Strategy()
        assert plugin.replay_adapter() is None
        assert get_replay_adapter_for_strategy("weather_v1") is None
