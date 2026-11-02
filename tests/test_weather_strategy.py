"""Tests for the weather feed adapter and weather_v1 strategy plugin."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from db import (
    ensure_weather_snapshots_table,
    init_db,
    insert_weather_snapshot,
    recent_weather_snapshots,
)
from sniper.feeds.weather import WeatherFeedAdapter
from sniper.strategies.weather_v1 import (
    WeatherV1Strategy,
    _title_matches_weather,
    _extract_temperature,
)


def _in_memory_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with row_factory set."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# Always a future end-date so the "market past end date" guard never fires
# incidentally — tests that want a past date pass one explicitly.
# 14d keeps us safely under the strategy's 30d `max_days_to_resolution` cap.
_FUTURE_END_DATE = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sample_owm_payload(temp_c: float = 22.5) -> dict:
    """Return a minimal OpenWeatherMap-like forecast payload."""
    return {
        "list": [
            {
                "dt": 1710000000,
                "main": {
                    "temp": temp_c,
                    "humidity": 65,
                },
                "weather": [{"main": "Clear", "description": "clear sky"}],
            }
        ],
        "city": {"name": "London"},
    }


def _seed_weather_market(conn, condition_id: str = "cond_weather_1", title: str = "Will temperature exceed 30C in London?"):
    """Insert a weather-related market into market_metadata."""
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, end_date_iso, category,
            category_source, volume_24h, is_open, last_synced_ts)
           VALUES (?, ?, '', '', ?, 'weather', '', 1000.0, 1, ?)""",
        (condition_id, title, _FUTURE_END_DATE, int(time.time())),
    )
    conn.commit()


# =============================================================================
# Feed Adapter Tests
# =============================================================================


class TestWeatherFeedAdapterAttributes(unittest.TestCase):
    """Verify the feed adapter has correct class-level attributes."""

    def test_feed_id(self):
        adapter = WeatherFeedAdapter()
        self.assertEqual(adapter.feed_id, "weather")

    def test_display_name(self):
        adapter = WeatherFeedAdapter()
        self.assertEqual(adapter.display_name, "OpenWeatherMap")

    def test_default_poll_seconds(self):
        adapter = WeatherFeedAdapter()
        self.assertEqual(adapter.default_poll_seconds, 300)

    def test_mode(self):
        adapter = WeatherFeedAdapter()
        self.assertEqual(adapter.mode, "poll")


class TestWeatherFeedValidateConfig(unittest.TestCase):
    """validate_config() should depend on OPENWEATHER_API_KEY env var."""

    def test_returns_false_when_env_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure the key is NOT set
            os.environ.pop("OPENWEATHER_API_KEY", None)
            adapter = WeatherFeedAdapter()
            self.assertFalse(adapter.validate_config())

    def test_returns_true_when_env_set(self):
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test_key_123"}):
            adapter = WeatherFeedAdapter()
            self.assertTrue(adapter.validate_config())

    def test_returns_false_for_empty_string(self):
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "  "}):
            adapter = WeatherFeedAdapter()
            self.assertFalse(adapter.validate_config())


class TestWeatherFeedWarmup(unittest.TestCase):
    """warmup() should create the weather_snapshots table."""

    def test_warmup_creates_table(self):
        conn = _in_memory_conn()
        adapter = WeatherFeedAdapter()
        adapter.warmup(conn)

        # Verify table exists by querying it
        rows = conn.execute("SELECT * FROM weather_snapshots").fetchall()
        self.assertEqual(len(rows), 0)

    def test_warmup_is_idempotent(self):
        conn = _in_memory_conn()
        adapter = WeatherFeedAdapter()
        adapter.warmup(conn)
        adapter.warmup(conn)  # Should not raise

        rows = conn.execute("SELECT * FROM weather_snapshots").fetchall()
        self.assertEqual(len(rows), 0)


class TestWeatherFeedTick(unittest.TestCase):
    """tick() should fetch and store weather snapshots."""

    def test_tick_skips_when_no_api_key(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)
        adapter = WeatherFeedAdapter()

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENWEATHER_API_KEY", None)
            adapter.tick(conn)

        rows = conn.execute("SELECT * FROM weather_snapshots").fetchall()
        self.assertEqual(len(rows), 0)

    @patch.object(WeatherFeedAdapter, "_fetch_forecast")
    def test_tick_stores_snapshot(self, mock_fetch):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)
        adapter = WeatherFeedAdapter()

        payload = _sample_owm_payload(25.0)
        mock_fetch.return_value = payload

        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test_key"}):
            adapter.tick(conn)

        rows = conn.execute("SELECT * FROM weather_snapshots").fetchall()
        self.assertGreater(len(rows), 0)

        # Verify payload was stored correctly
        first = dict(rows[0])
        stored_payload = json.loads(first["payload_json"])
        self.assertEqual(stored_payload["list"][0]["main"]["temp"], 25.0)

    @patch.object(WeatherFeedAdapter, "_fetch_forecast")
    def test_tick_handles_fetch_failure(self, mock_fetch):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)
        adapter = WeatherFeedAdapter()

        mock_fetch.return_value = None  # Simulate API failure

        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test_key"}):
            adapter.tick(conn)  # Should not raise

        rows = conn.execute("SELECT * FROM weather_snapshots").fetchall()
        self.assertEqual(len(rows), 0)

    @patch.object(WeatherFeedAdapter, "_fetch_forecast")
    def test_tick_uses_instance_tag_env_var(self, mock_fetch):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)
        adapter = WeatherFeedAdapter()

        mock_fetch.return_value = _sample_owm_payload(20.0)

        with patch.dict(
            os.environ,
            {"OPENWEATHER_API_KEY": "test_key", "INSTANCE_TAG": "weather-paper-us"},
            clear=False,
        ):
            adapter.tick(conn)

        row = conn.execute(
            "SELECT instance_tag FROM weather_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["instance_tag"], "weather-paper-us")


# =============================================================================
# DB Helper Tests
# =============================================================================


class TestWeatherDBHelpers(unittest.TestCase):
    """Test the weather_snapshots DB helpers in db.py."""

    def test_ensure_table_creates_schema(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        # Insert and read back
        row_id = insert_weather_snapshot(
            conn,
            instance_tag="test",
            location_key="london",
            forecast_ts=1710000000,
            payload_json=json.dumps(_sample_owm_payload()),
        )
        self.assertIsNotNone(row_id)
        self.assertGreater(row_id, 0)

    def test_recent_snapshots_all(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        insert_weather_snapshot(conn, location_key="london", forecast_ts=1710000000, payload_json="{}")
        insert_weather_snapshot(conn, location_key="tokyo", forecast_ts=1710003600, payload_json="{}")

        results = recent_weather_snapshots(conn, hours=24)
        self.assertEqual(len(results), 2)

    def test_recent_snapshots_by_location(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        insert_weather_snapshot(conn, location_key="london", forecast_ts=1710000000, payload_json="{}")
        insert_weather_snapshot(conn, location_key="tokyo", forecast_ts=1710003600, payload_json="{}")

        results = recent_weather_snapshots(conn, location_key="london", hours=24)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["location_key"], "london")


# =============================================================================
# Weather Keyword Matching Tests
# =============================================================================


class TestWeatherKeywordMatching(unittest.TestCase):
    """Test _title_matches_weather helper."""

    def test_matches_temperature(self):
        self.assertTrue(_title_matches_weather("Will the temperature exceed 30C?"))

    def test_matches_weather(self):
        self.assertTrue(_title_matches_weather("Weather conditions in NYC"))

    def test_matches_rain(self):
        self.assertTrue(_title_matches_weather("Will it rain in London tomorrow?"))

    def test_matches_snow(self):
        self.assertTrue(_title_matches_weather("Snow forecast for Denver"))

    def test_matches_celsius(self):
        self.assertTrue(_title_matches_weather("Above 25 celsius in Paris?"))

    def test_matches_fahrenheit(self):
        self.assertTrue(_title_matches_weather("Over 100 fahrenheit in Phoenix?"))

    def test_no_match_politics(self):
        self.assertFalse(_title_matches_weather("Will Biden win the election?"))

    def test_no_match_sports(self):
        self.assertFalse(_title_matches_weather("Lakers vs Celtics game outcome"))

    def test_case_insensitive(self):
        self.assertTrue(_title_matches_weather("TEMPERATURE RECORD BROKEN"))


# =============================================================================
# Temperature Extraction Tests
# =============================================================================


class TestExtractTemperature(unittest.TestCase):
    """Test _extract_temperature helper."""

    def test_extracts_normal_temp(self):
        payload = _sample_owm_payload(18.5)
        self.assertAlmostEqual(_extract_temperature(payload), 18.5)

    def test_returns_none_for_empty(self):
        self.assertIsNone(_extract_temperature({}))

    def test_returns_none_for_missing_main(self):
        self.assertIsNone(_extract_temperature({"list": [{}]}))

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(_extract_temperature({"list": []}))


# =============================================================================
# Strategy Plugin Tests
# =============================================================================


class TestWeatherV1StrategyAttributes(unittest.TestCase):
    """Verify the strategy plugin has correct class-level attributes."""

    def test_plugin_id(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.plugin_id, "weather_v1")

    def test_display_name(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.display_name, "Weather Bot V1")

    def test_strategy_family(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.strategy_family, "weather")

    def test_required_feeds(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.required_feeds, ("polymarket", "weather"))

    def test_supported_execution_modes(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.supported_execution_modes, ("paper",))

    def test_default_loop_seconds(self):
        plugin = WeatherV1Strategy()
        self.assertEqual(plugin.default_loop_seconds, 60)

    def test_replay_adapter_returns_none(self):
        plugin = WeatherV1Strategy()
        self.assertIsNone(plugin.replay_adapter())


class TestWeatherV1Discover(unittest.TestCase):
    """Test discover() for weather_v1 strategy."""

    def test_returns_signals_when_weather_data_and_matching_markets(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        # Seed a weather market
        _seed_weather_market(conn, "cond_w1", "Will temperature exceed 30C in London?")

        # Seed a weather snapshot
        insert_weather_snapshot(
            conn,
            location_key="london",
            forecast_ts=int(time.time()),
            payload_json=json.dumps(_sample_owm_payload(32.0)),
        )

        plugin = WeatherV1Strategy()
        strategy = {"parameters": {}}
        signals = plugin.discover(conn, strategy)

        self.assertGreater(len(signals), 0)
        self.assertEqual(signals[0].condition_id, "cond_w1")
        self.assertIn("temperature", signals[0].title.lower())

    def test_returns_empty_when_no_matching_markets(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        # Seed a NON-weather market
        _seed_weather_market(conn, "cond_politics", "Will Biden win?")

        # Seed a weather snapshot
        insert_weather_snapshot(
            conn,
            location_key="london",
            forecast_ts=int(time.time()),
            payload_json=json.dumps(_sample_owm_payload(22.0)),
        )

        plugin = WeatherV1Strategy()
        strategy = {"parameters": {}}
        signals = plugin.discover(conn, strategy)

        self.assertEqual(len(signals), 0)

    def test_returns_empty_when_no_weather_snapshots(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        _seed_weather_market(conn, "cond_w1", "Will temperature exceed 30C?")

        plugin = WeatherV1Strategy()
        strategy = {"parameters": {}}
        signals = plugin.discover(conn, strategy)

        self.assertEqual(len(signals), 0)

    def test_signal_metadata_includes_forecast(self):
        conn = _in_memory_conn()
        ensure_weather_snapshots_table(conn)

        _seed_weather_market(conn, "cond_w2", "Rain forecast for Tokyo this week")
        insert_weather_snapshot(
            conn,
            location_key="tokyo",
            forecast_ts=int(time.time()),
            payload_json=json.dumps(_sample_owm_payload(15.0)),
        )

        plugin = WeatherV1Strategy()
        signals = plugin.discover(conn, {"parameters": {}})

        self.assertGreater(len(signals), 0)
        meta = signals[0].metadata
        self.assertIn("forecast_temperature_c", meta)
        self.assertIn("location_key", meta)


class TestWeatherV1Enter(unittest.TestCase):
    """Test enter() for weather_v1 strategy."""

    def _make_signal(self, price: float = 0.45, temp: float = 32.0, end_date: str = _FUTURE_END_DATE):
        from sniper.strategies.base import StrategySignal
        return StrategySignal(
            wallet="weather_bot",
            condition_id="cond_w1",
            outcome="YES",
            title="Will temperature exceed 30C?",
            current_price=price,
            position_value=0.0,
            analyzed_ts=int(time.time()),
            metadata={
                "forecast_temperature_c": temp,
                "location_key": "london",
                "end_date_iso": end_date,
            },
        )

    def test_enters_when_conditions_met(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45, temp=32.0)
        strategy = {"parameters": {"bet_size_usdc": 10.0}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertTrue(decision.should_enter)
        self.assertGreater(decision.size_usdc, 0)

    def test_rejects_above_max_entry_price(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.85)
        strategy = {"parameters": {"max_entry_price": 0.70}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("entry price", decision.reason)

    def test_rejects_duplicate_position(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45)
        strategy = {"parameters": {}}
        existing = [{"condition_id": "cond_w1", "outcome": "YES"}]

        decision = plugin.enter(conn, strategy, signal, existing)
        self.assertFalse(decision.should_enter)
        self.assertIn("already open", decision.reason)

    def test_rejects_max_concurrent(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45)
        strategy = {"parameters": {"max_concurrent_positions": 2}}
        existing = [
            {"condition_id": "cond_a", "outcome": "YES"},
            {"condition_id": "cond_b", "outcome": "NO"},
        ]

        decision = plugin.enter(conn, strategy, signal, existing)
        self.assertFalse(decision.should_enter)
        self.assertIn("max concurrent", decision.reason)

    def test_rejects_invalid_price(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.0)
        strategy = {"parameters": {}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("invalid current price", decision.reason)

    def test_rejects_market_exposure_exceeded(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45)
        strategy = {"parameters": {"max_exposure_per_market_usdc": 50.0}}
        existing = [
            {"condition_id": "cond_w1", "outcome": "NO", "entry_shares": 100, "avg_entry_price": 0.55},
        ]

        decision = plugin.enter(conn, strategy, signal, existing)
        self.assertFalse(decision.should_enter)
        self.assertIn("market exposure", decision.reason)

    def test_rejects_no_forecast_temperature(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        from sniper.strategies.base import StrategySignal
        signal = StrategySignal(
            wallet="weather_bot",
            condition_id="cond_w1",
            outcome="YES",
            title="Weather test",
            current_price=0.45,
            position_value=0.0,
            metadata={"forecast_temperature_c": None, "end_date_iso": _FUTURE_END_DATE},
        )
        strategy = {"parameters": {}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("no forecast temperature", decision.reason)

    def test_rejects_past_end_date(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45, end_date="2020-01-01T00:00:00Z")
        strategy = {"parameters": {}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertFalse(decision.should_enter)
        self.assertIn("past end date", decision.reason)

    def test_entry_metadata_includes_forecast(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        signal = self._make_signal(price=0.45, temp=28.5)
        strategy = {"parameters": {}}

        decision = plugin.enter(conn, strategy, signal, [])
        self.assertTrue(decision.should_enter)
        self.assertAlmostEqual(decision.metadata["forecast_temperature_c"], 28.5)


class TestWeatherV1Manage(unittest.TestCase):
    """Test manage() for weather_v1 strategy."""

    def test_returns_signal_open_true_for_active_market(self):
        conn = _in_memory_conn()
        # Seed an open market
        _seed_weather_market(conn, "cond_w1", "Temperature market")

        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45}
        strategy = {"parameters": {}}

        state = plugin.manage(conn, strategy, position)
        self.assertTrue(state.signal_open)

    def test_returns_signal_open_false_for_closed_market(self):
        conn = _in_memory_conn()
        conn.execute(
            """INSERT OR REPLACE INTO market_metadata
               (condition_id, title, slug, event_slug, end_date_iso, category,
                category_source, volume_24h, is_open, last_synced_ts)
               VALUES ('cond_w1', 'Temp market', '', '', '', '', '', 0.0, 0, ?)""",
            (int(time.time()),),
        )
        conn.commit()

        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45}
        strategy = {"parameters": {}}

        state = plugin.manage(conn, strategy, position)
        self.assertFalse(state.signal_open)


class TestWeatherV1Exit(unittest.TestCase):
    """Test exit() for weather_v1 strategy."""

    def test_exit_on_resolution_win(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.98, signal_open=True)
        strategy = {"parameters": {}}

        decision = plugin.exit(conn, strategy, position, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "resolution_win")

    def test_exit_on_resolution_loss(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.02, signal_open=True)
        strategy = {"parameters": {}}

        decision = plugin.exit(conn, strategy, position, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "resolution_loss")

    def test_exit_on_market_closed(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.50, signal_open=False)
        strategy = {"parameters": {}}

        decision = plugin.exit(conn, strategy, position, state)
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_closed")

    def test_exit_on_stale(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        # Position opened 10 days ago
        position = {
            "condition_id": "cond_w1",
            "outcome": "YES",
            "avg_entry_price": 0.45,
            "opened_ts": int(time.time()) - 86400 * 10,
        }
        state = StrategyManageState(current_price=0.50, signal_open=True)
        strategy = {"parameters": {"stale_days": 7}}

        decision = plugin.exit(conn, strategy, position, state)
        self.assertTrue(decision.should_exit)
        self.assertIn("stale_", decision.reason)

    def test_hold_when_active(self):
        conn = _in_memory_conn()
        plugin = WeatherV1Strategy()
        position = {"condition_id": "cond_w1", "outcome": "YES", "avg_entry_price": 0.45, "opened_ts": int(time.time())}
        state = StrategyManageState(current_price=0.50, signal_open=True)
        strategy = {"parameters": {}}

        decision = plugin.exit(conn, strategy, position, state)
        self.assertFalse(decision.should_exit)
        self.assertEqual(decision.reason, "hold")


# =============================================================================
# Registry Tests
# =============================================================================


class TestRegistryInclusion(unittest.TestCase):
    """Verify weather_v1 is registered in the strategy registry."""

    def test_weather_v1_in_strategy_registry(self):
        from sniper.strategies.registry import get_strategy_plugin, list_strategy_plugins
        plugin = get_strategy_plugin("weather_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "weather_v1")

    def test_weather_in_feed_registry(self):
        from sniper.feeds.registry import get_feed_adapter, list_feed_adapters
        adapter = get_feed_adapter("weather")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.feed_id, "weather")

    def test_list_strategy_plugins_includes_weather(self):
        from sniper.strategies.registry import list_strategy_plugins
        plugins = list_strategy_plugins()
        plugin_ids = [p.plugin_id for p in plugins]
        self.assertIn("weather_v1", plugin_ids)

    def test_list_feed_adapters_includes_weather(self):
        from sniper.feeds.registry import list_feed_adapters
        adapters = list_feed_adapters()
        feed_ids = [a.feed_id for a in adapters]
        self.assertIn("weather", feed_ids)


# =============================================================================
# Import for StrategyManageState in tests
# =============================================================================
from sniper.strategies.base import StrategyManageState


if __name__ == "__main__":
    unittest.main()
