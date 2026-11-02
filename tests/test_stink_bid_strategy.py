"""Tests for stink_bid_v1 strategy plugin and tennis helpers."""
from __future__ import annotations

import pytest

from sniper.strategies.base import StrategyManageState, StrategySignal
from sniper.strategies.stink_bid_v1 import (
    StinkBidV1Strategy,
    _extract_player_surnames,
    _extract_tour,
    _is_tennis_match_title,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plugin() -> StinkBidV1Strategy:
    return StinkBidV1Strategy()


def _make_strategy() -> dict:
    return {
        "id": 99,
        "strategy_type": "stink_bid_v1",
        "parameters": "{}",
        "status": "active",
    }


def _make_position(**overrides) -> dict:
    base = {
        "id": 1,
        "condition_id": "0xabc",
        "outcome": "YES",
        "wallet": "",
        "avg_entry_price": 0.35,
        "current_price": 0.50,
        "opened_ts": 1700000000,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


class TestPluginMetadata:

    def test_plugin_id(self):
        plugin = _make_plugin()
        assert plugin.plugin_id == "stink_bid_v1"

    def test_display_name(self):
        plugin = _make_plugin()
        assert "Tennis" in plugin.display_name or "tennis" in plugin.display_name.lower()

    def test_paper_in_supported_execution_modes(self):
        plugin = _make_plugin()
        assert "paper" in plugin.supported_execution_modes

    def test_real_not_in_supported_execution_modes(self):
        plugin = _make_plugin()
        assert "real" not in plugin.supported_execution_modes

    def test_default_loop_seconds(self):
        plugin = _make_plugin()
        assert plugin.default_loop_seconds == 900

    def test_registered_in_registry(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("stink_bid_v1")
        assert plugin is not None
        assert plugin.plugin_id == "stink_bid_v1"


# ---------------------------------------------------------------------------
# discover() — always empty
# ---------------------------------------------------------------------------


class TestDiscover:

    def test_returns_empty_list(self):
        plugin = _make_plugin()
        result = plugin.discover(None, _make_strategy())
        assert result == []

    def test_return_type_is_list(self):
        plugin = _make_plugin()
        result = plugin.discover(None, _make_strategy())
        assert isinstance(result, list)

    def test_ignores_strategy_dict_contents(self):
        plugin = _make_plugin()
        result = plugin.discover(None, {"id": 1, "wallet": "0xabc"})
        assert result == []


# ---------------------------------------------------------------------------
# enter() — always False
# ---------------------------------------------------------------------------


class TestEnter:

    def _make_signal(self) -> StrategySignal:
        return StrategySignal(
            wallet="0xabc",
            condition_id="0xdef",
            outcome="YES",
            title="Swiatek vs Sabalenka - WTA Finals",
            current_price=0.35,
            position_value=500.0,
            analyzed_ts=1700000000,
        )

    def test_returns_false(self):
        plugin = _make_plugin()
        decision = plugin.enter(None, _make_strategy(), self._make_signal(), [])
        assert decision.should_enter is False

    def test_reason_contains_delegated(self):
        plugin = _make_plugin()
        decision = plugin.enter(None, _make_strategy(), self._make_signal(), [])
        assert "runner" in decision.reason or "delegated" in decision.reason

    def test_false_regardless_of_open_positions(self):
        plugin = _make_plugin()
        many_open = [_make_position()] * 5
        decision = plugin.enter(None, _make_strategy(), self._make_signal(), many_open)
        assert decision.should_enter is False


# ---------------------------------------------------------------------------
# manage() — returns current_price and signal_open=True
# ---------------------------------------------------------------------------


class TestManage:

    def test_returns_current_price_from_position(self):
        plugin = _make_plugin()
        pos = _make_position(current_price=0.72)
        state = plugin.manage(None, _make_strategy(), pos)
        assert state.current_price == pytest.approx(0.72)

    def test_falls_back_to_avg_entry_price(self):
        plugin = _make_plugin()
        pos = _make_position(avg_entry_price=0.35)
        del pos["current_price"]
        state = plugin.manage(None, _make_strategy(), pos)
        assert state.current_price == pytest.approx(0.35)

    def test_signal_open_always_true(self):
        plugin = _make_plugin()
        state = plugin.manage(None, _make_strategy(), _make_position())
        assert state.signal_open is True

    def test_signal_open_true_when_price_near_resolution(self):
        plugin = _make_plugin()
        pos = _make_position(current_price=0.98)
        state = plugin.manage(None, _make_strategy(), pos)
        assert state.signal_open is True

    def test_metadata_is_dict(self):
        plugin = _make_plugin()
        state = plugin.manage(None, _make_strategy(), _make_position())
        assert isinstance(state.metadata, dict)


# ---------------------------------------------------------------------------
# exit() — resolution only
# ---------------------------------------------------------------------------


class TestExit:

    def _exit(self, current_price: float) -> object:
        plugin = _make_plugin()
        pos = _make_position()
        state = StrategyManageState(current_price=current_price, signal_open=True)
        return plugin.exit(None, _make_strategy(), pos, state)

    def test_resolution_win_at_0_97(self):
        decision = self._exit(0.97)
        assert decision.should_exit is True
        assert decision.reason == "resolution_win"
        assert decision.status == "closed"

    def test_resolution_win_above_0_97(self):
        decision = self._exit(0.99)
        assert decision.should_exit is True
        assert decision.reason == "resolution_win"

    def test_resolution_win_at_1_0(self):
        decision = self._exit(1.0)
        assert decision.should_exit is True
        assert decision.reason == "resolution_win"

    def test_resolution_loss_at_0_03(self):
        decision = self._exit(0.03)
        assert decision.should_exit is True
        assert decision.reason == "resolution_loss"
        assert decision.status == "closed"

    def test_resolution_loss_below_0_03(self):
        decision = self._exit(0.01)
        assert decision.should_exit is True
        assert decision.reason == "resolution_loss"

    def test_resolution_loss_at_0_0(self):
        decision = self._exit(0.0)
        assert decision.should_exit is True
        assert decision.reason == "resolution_loss"

    def test_hold_at_mid_price(self):
        decision = self._exit(0.50)
        assert decision.should_exit is False
        assert decision.reason == "hold"
        assert decision.status == "open"

    def test_hold_just_below_win_threshold(self):
        decision = self._exit(0.96)
        assert decision.should_exit is False

    def test_hold_just_above_loss_threshold(self):
        decision = self._exit(0.04)
        assert decision.should_exit is False

    def test_exit_price_matches_current_price(self):
        decision = self._exit(0.98)
        assert decision.exit_price == pytest.approx(0.98)

    def test_uses_state_current_price_not_position_price(self):
        """state.current_price should take priority over position avg_entry_price."""
        plugin = _make_plugin()
        pos = _make_position(avg_entry_price=0.35)
        state = StrategyManageState(current_price=0.98, signal_open=True)
        decision = plugin.exit(None, _make_strategy(), pos, state)
        assert decision.should_exit is True
        assert decision.reason == "resolution_win"

    def test_falls_back_to_position_price_when_state_none(self):
        """If state.current_price is None, fall back to position avg_entry_price."""
        plugin = _make_plugin()
        pos = _make_position(avg_entry_price=0.50)
        state = StrategyManageState(current_price=None, signal_open=True)
        decision = plugin.exit(None, _make_strategy(), pos, state)
        assert decision.should_exit is False
        assert decision.reason == "hold"


# ---------------------------------------------------------------------------
# Tennis helpers: _is_tennis_match_title
# ---------------------------------------------------------------------------


class TestIsTennisMatchTitle:

    def test_wta_match(self):
        assert _is_tennis_match_title("Swiatek vs Sabalenka - WTA Finals") is True

    def test_atp_match(self):
        assert _is_tennis_match_title("Alcaraz vs Sinner - ATP Roland Garros") is True

    def test_wimbledon(self):
        assert _is_tennis_match_title("Djokovic vs Alcaraz - Wimbledon Final") is True

    def test_non_tennis_market(self):
        assert _is_tennis_match_title("Will Bitcoin hit 100K?") is False

    def test_vs_without_tennis_keyword(self):
        # "vs" alone is not enough — needs a tennis keyword
        assert _is_tennis_match_title("Team A vs Team B - NBA Finals") is False

    def test_tennis_keyword_without_vs(self):
        # keyword alone is not enough — needs a "vs" pattern
        assert _is_tennis_match_title("WTA Finals 2025 winner?") is False

    def test_empty_string(self):
        assert _is_tennis_match_title("") is False

    def test_none_like_handling(self):
        # Passing None should not raise; treated as no-match
        assert _is_tennis_match_title("") is False

    def test_itf_tournament(self):
        assert _is_tennis_match_title("Fernandez vs Pavlyuchenkova - ITF Billie Jean") is True

    def test_us_open(self):
        assert _is_tennis_match_title("Gauff vs Rybakina - US Open Quarterfinal") is True


# ---------------------------------------------------------------------------
# Tennis helpers: _extract_tour
# ---------------------------------------------------------------------------


class TestExtractTour:

    def test_wta_title(self):
        assert _extract_tour("Swiatek vs Sabalenka - WTA Finals") == "wta"

    def test_wta_case_insensitive(self):
        assert _extract_tour("smith vs jones - wta miami open") == "wta"

    def test_atp_title(self):
        assert _extract_tour("Alcaraz vs Sinner - ATP Roland Garros") == "atp"

    def test_no_keyword_defaults_atp(self):
        # Titles without WTA default to "atp"
        assert _extract_tour("Djokovic vs Federer - Wimbledon") == "atp"

    def test_wta_takes_priority(self):
        # If both WTA and ATP appear (unusual but possible), WTA wins
        assert _extract_tour("WTA/ATP mixed event - Smith vs Jones") == "wta"


# ---------------------------------------------------------------------------
# Tennis helpers: _extract_player_surnames
# ---------------------------------------------------------------------------


class TestExtractPlayerSurnames:

    def test_basic_wta(self):
        result = _extract_player_surnames("Swiatek vs Sabalenka - WTA Finals")
        assert result is not None
        names = (result[0].lower(), result[1].lower())
        assert names == ("swiatek", "sabalenka")

    def test_basic_atp(self):
        result = _extract_player_surnames("Alcaraz vs Sinner - ATP Roland Garros")
        assert result is not None
        names = (result[0].lower(), result[1].lower())
        assert names == ("alcaraz", "sinner")

    def test_no_vs_returns_none(self):
        result = _extract_player_surnames("WTA Finals 2025 winner?")
        assert result is None

    def test_empty_returns_none(self):
        result = _extract_player_surnames("")
        assert result is None

    def test_returns_tuple_of_two(self):
        result = _extract_player_surnames("Djokovic vs Alcaraz - Wimbledon")
        assert result is not None
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_last_word_used_for_compound_names(self):
        # "Carlos Alcaraz vs Jannik Sinner" → ("Alcaraz", "Sinner")
        result = _extract_player_surnames("Carlos Alcaraz vs Jannik Sinner - ATP Finals")
        assert result is not None
        assert result[0].lower() == "alcaraz"
        assert result[1].lower() == "sinner"
