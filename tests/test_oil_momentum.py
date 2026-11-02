"""Tests for the oil_momentum_v1 strategy plugin.

TDD: tests written before implementation.
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from db import (
    ensure_commodity_snapshots_table,
    init_db,
    insert_commodity_snapshot,
)


# Always a future end-date so the "market past end date" guard never fires
# incidentally — tests that want a past date pass one explicitly.
_FUTURE_END_DATE = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_commodity_snapshots_table(conn)
    return conn


def _seed_market(
    conn,
    condition_id: str,
    title: str,
    is_open: int = 1,
    volume_24h: float = 1000.0,
    end_date: str = _FUTURE_END_DATE,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, end_date_iso, category,
            category_source, volume_24h, is_open, last_synced_ts)
           VALUES (?, ?, '', '', ?, '', '', ?, ?, ?)""",
        (condition_id, title, end_date, volume_24h, is_open, int(time.time())),
    )
    conn.commit()


def _seed_commodity_history(
    conn,
    symbol: str = "BZ=F",
    prices: list | None = None,
    spread_hours: int = 100,
) -> None:
    """Seed commodity price history spread over time so coverage checks pass.

    Uses SQLite datetime arithmetic to spread entries across ``spread_hours``.
    The oldest entry is ``spread_hours`` ago; the newest is now.
    """
    if prices is None:
        prices = [70.0, 71.0, 72.0, 73.0, 74.0]

    n = len(prices)
    if n == 1:
        # Single entry at the midpoint
        hours_ago = spread_hours // 2
        conn.execute(
            """INSERT OR IGNORE INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, market_ts, payload_json, captured_at)
               VALUES (?, 'commodities', 'yfinance', ?, ?, ?, '{}',
                       datetime('now', ? || ' hours'))""",
            ("test", symbol, prices[0],
             (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             f"-{hours_ago}"),
        )
    else:
        step = spread_hours / (n - 1)
        for i, price in enumerate(prices):
            hours_ago = spread_hours - i * step
            # Use distinct market_ts to avoid unique constraint violations
            market_ts = (
                datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """INSERT OR IGNORE INTO commodity_snapshots
                   (instance_tag, feed_id, provider, symbol, price_usd, market_ts, payload_json, captured_at)
                   VALUES (?, 'commodities', 'yfinance', ?, ?, ?, '{}',
                           datetime('now', ? || ' hours'))""",
                ("test", symbol, price, market_ts, f"-{hours_ago:.1f}"),
            )
    conn.commit()


def _default_strategy(params: dict | None = None) -> dict:
    return {
        "id": 1,
        "wallet": "oil_momentum_bot",
        "strategy_type": "oil_momentum_v1",
        "parameters": params or {},
    }


# ---------------------------------------------------------------------------
# TestParseDirection
# ---------------------------------------------------------------------------

class TestParseDirection(unittest.TestCase):
    """_parse_direction() should classify market titles as oil_up / oil_down / None."""

    def setUp(self):
        from sniper.strategies.oil_momentum_v1 import _parse_direction
        self._parse = _parse_direction

    def test_oil_up_keywords(self):
        self.assertEqual(self._parse("Will oil prices exceed $80 per barrel?"), "oil_up")
        self.assertEqual(self._parse("Will crude oil rise above $90?"), "oil_up")
        self.assertEqual(self._parse("Oil prices higher than $75 by June?"), "oil_up")
        self.assertEqual(self._parse("Will Brent trade over $85?"), "oil_up")

    def test_oil_down_keywords(self):
        self.assertEqual(self._parse("Will crude oil fall below $70?"), "oil_down")
        self.assertEqual(self._parse("Oil prices below $60 this quarter?"), "oil_down")
        self.assertEqual(self._parse("Will crude drop to $65?"), "oil_down")
        self.assertEqual(self._parse("Will oil decline below $60?"), "oil_down")

    def test_ambiguous_returns_none(self):
        self.assertIsNone(self._parse("Will oil prices change?"))
        self.assertIsNone(self._parse("Will Bitcoin reach $200k?"))

    def test_case_insensitive(self):
        self.assertEqual(self._parse("WILL OIL EXCEED $80?"), "oil_up")
        self.assertEqual(self._parse("OIL WILL FALL BELOW $60?"), "oil_down")


# ---------------------------------------------------------------------------
# TestComputeMomentum
# ---------------------------------------------------------------------------

class TestComputeMomentum(unittest.TestCase):
    """_compute_momentum() should return % change or None."""

    def setUp(self):
        from sniper.strategies.oil_momentum_v1 import _compute_momentum
        self._compute = _compute_momentum

    def _make_series(self, prices: list, spread_hours: int = 100) -> list:
        """Build a synthetic price series with proper timestamps."""
        n = len(prices)
        if n == 0:
            return []
        if n == 1:
            ts = (datetime.now(timezone.utc) - timedelta(hours=spread_hours // 2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            return [{"price_usd": prices[0], "market_ts": ts}]
        step = spread_hours / (n - 1)
        result = []
        for i, price in enumerate(prices):
            hours_ago = spread_hours - i * step
            ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            result.append({"price_usd": price, "market_ts": ts})
        return result

    def test_positive_momentum(self):
        series = self._make_series([70.0, 71.0, 72.0, 73.0, 74.0], spread_hours=100)
        result = self._compute(series, window_hours=168)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_negative_momentum(self):
        series = self._make_series([80.0, 78.0, 76.0, 74.0, 72.0], spread_hours=100)
        result = self._compute(series, window_hours=168)
        self.assertIsNotNone(result)
        self.assertLess(result, 0)

    def test_empty_series_returns_none(self):
        result = self._compute([], window_hours=168)
        self.assertIsNone(result)

    def test_single_point_returns_none(self):
        series = self._make_series([70.0], spread_hours=50)
        result = self._compute(series, window_hours=168)
        self.assertIsNone(result)

    def test_insufficient_time_coverage_returns_none(self):
        """Series spanning only 10h of a 168h window → < 50% coverage → None."""
        # Both points within 10 hours of each other
        now = datetime.now(timezone.utc)
        series = [
            {"price_usd": 70.0, "market_ts": (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"price_usd": 72.0, "market_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        ]
        result = self._compute(series, window_hours=168)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestResolveMomentumSignal
# ---------------------------------------------------------------------------

class TestResolveMomentumSignal(unittest.TestCase):

    def setUp(self):
        from sniper.strategies.oil_momentum_v1 import _resolve_momentum_signal
        self._resolve = _resolve_momentum_signal

    def test_conflicting_symbols_return_none(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 72.0, 74.0, 76.0, 78.0])
        _seed_commodity_history(conn, "CL=F", prices=[80.0, 78.0, 76.0, 74.0, 72.0])
        result = self._resolve(
            conn,
            symbols=["BZ=F", "CL=F"],
            window_hours=168,
            min_momentum_pct=1.0,
        )
        self.assertIsNone(result)

    def test_confirming_symbols_return_consensus(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 72.0, 74.0, 76.0, 78.0])
        _seed_commodity_history(conn, "CL=F", prices=[69.0, 70.0, 71.0, 73.0, 75.0])
        result = self._resolve(
            conn,
            symbols=["BZ=F", "CL=F"],
            window_hours=168,
            min_momentum_pct=1.0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "oil_up")
        self.assertEqual(set(result["symbols_confirmed"]), {"BZ=F", "CL=F"})


# ---------------------------------------------------------------------------
# TestDiscover
# ---------------------------------------------------------------------------

class TestDiscover(unittest.TestCase):

    def _make_plugin(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        return OilMomentumV1Strategy()

    def test_matching_market_with_positive_momentum(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil exceed $80 per barrel?")
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 71.0, 72.0, 73.0, 74.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy())

        self.assertGreater(len(signals), 0)
        sig = signals[0]
        self.assertEqual(sig.wallet, "oil_momentum_bot")
        self.assertEqual(sig.condition_id, "cond_oil_1")
        self.assertEqual(sig.current_price, 0.0)  # runner enriches later

    def test_no_momentum_skips_market(self):
        """Flat / zero change → momentum None or 0 → skip."""
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil exceed $80?")
        # All same price = no momentum
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 70.0, 70.0, 70.0, 70.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)

    def test_ambiguous_direction_market_skipped(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil prices change significantly?")
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 71.0, 72.0, 73.0, 74.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)

    def test_oil_down_market_with_negative_momentum(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_down", "Will crude oil fall below $70?")
        _seed_commodity_history(conn, "BZ=F", prices=[80.0, 78.0, 76.0, 74.0, 72.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy())

        self.assertGreater(len(signals), 0)
        self.assertEqual(signals[0].condition_id, "cond_oil_down")

    def test_opposing_momentum_skips_market(self):
        """oil_up market but momentum is negative → skip."""
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_up", "Will oil exceed $80?")
        # Negative momentum (falling price)
        _seed_commodity_history(conn, "BZ=F", prices=[80.0, 78.0, 76.0, 74.0, 72.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy())
        self.assertEqual(len(signals), 0)

    def test_conflicting_symbols_skip_discovery(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_up", "Will oil exceed $80?")
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 72.0, 74.0, 76.0, 78.0])
        _seed_commodity_history(conn, "CL=F", prices=[80.0, 78.0, 76.0, 74.0, 72.0])

        plugin = self._make_plugin()
        signals = plugin.discover(conn, _default_strategy({"symbols": ["BZ=F", "CL=F"]}))
        self.assertEqual(len(signals), 0)


# ---------------------------------------------------------------------------
# TestEnter
# ---------------------------------------------------------------------------

class TestEnter(unittest.TestCase):

    def _make_plugin(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        return OilMomentumV1Strategy()

    def _make_signal(
        self,
        condition_id: str = "cond_oil_1",
        outcome: str = "YES",
        price: float = 0.45,
        momentum: float = 5.0,
        momentum_ts: int | None = None,
        end_date: str = _FUTURE_END_DATE,
    ):
        from sniper.strategies.base import StrategySignal
        if momentum_ts is None:
            momentum_ts = int(time.time()) - 3600  # 1 hour ago
        return StrategySignal(
            wallet="oil_momentum_bot",
            condition_id=condition_id,
            outcome=outcome,
            title="Will oil exceed $80?",
            current_price=price,
            position_value=0.0,
            analyzed_ts=int(time.time()),
            metadata={
                "direction": "oil_up",
                "momentum_pct": momentum,
                "momentum_ts": momentum_ts,
                "end_date_iso": end_date,
                "symbol": "BZ=F",
            },
        )

    def test_approve_entry(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 71.0, 72.0, 73.0, 74.0])
        plugin = self._make_plugin()
        signal = self._make_signal(price=0.45)
        strategy = _default_strategy({"bet_size_usdc": 10.0})

        decision = plugin.enter(conn, strategy, signal, [])

        self.assertTrue(decision.should_enter)
        self.assertAlmostEqual(decision.size_usdc, 10.0)
        self.assertAlmostEqual(decision.entry_price, 0.45)

    def test_reject_duplicate(self):
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        signal = self._make_signal(condition_id="cond_oil_1", outcome="YES")
        existing = [{"condition_id": "cond_oil_1", "outcome": "YES"}]

        decision = plugin.enter(conn, strategy=_default_strategy(), signal=signal, open_positions=existing)

        self.assertFalse(decision.should_enter)
        self.assertIn("already open", decision.reason)

    def test_reject_max_concurrent(self):
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        signal = self._make_signal()
        # Fill up max_concurrent (default=5)
        existing = [
            {"condition_id": f"cond_{i}", "outcome": "YES"} for i in range(5)
        ]
        strategy = _default_strategy({"max_concurrent": 5})

        decision = plugin.enter(conn, strategy, signal, existing)

        self.assertFalse(decision.should_enter)
        self.assertIn("max concurrent", decision.reason)

    def test_reject_high_price(self):
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        signal = self._make_signal(price=0.85)
        strategy = _default_strategy({"max_entry_price": 0.70})

        decision = plugin.enter(conn, strategy, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("entry price", decision.reason)

    def test_reject_stale_momentum(self):
        """No commodity data at entry time → momentum re-query fails → reject."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        # No commodity data seeded — re-query will find nothing
        signal = self._make_signal()
        strategy = _default_strategy()

        decision = plugin.enter(conn, strategy, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("momentum", decision.reason)

    def test_metadata_includes_insider_convergence(self):
        conn = _in_memory_conn()
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 71.0, 72.0, 73.0, 74.0])
        plugin = self._make_plugin()
        signal = self._make_signal(price=0.45)
        strategy = _default_strategy()

        decision = plugin.enter(conn, strategy, signal, [])

        self.assertTrue(decision.should_enter)
        self.assertIn("insider_convergence", decision.metadata)

    def test_reject_when_symbol_consensus_reverses(self):
        conn = _in_memory_conn()
        # Original signal says oil_up and both symbols were considered.
        _seed_commodity_history(conn, "BZ=F", prices=[70.0, 72.0, 74.0, 76.0, 78.0])
        _seed_commodity_history(conn, "CL=F", prices=[80.0, 78.0, 76.0, 74.0, 72.0])
        plugin = self._make_plugin()
        signal = self._make_signal(price=0.45)
        signal.metadata["symbols_considered"] = ["BZ=F", "CL=F"]
        strategy = _default_strategy({"symbols": ["BZ=F", "CL=F"]})

        decision = plugin.enter(conn, strategy, signal, [])

        self.assertFalse(decision.should_enter)
        self.assertIn("momentum", decision.reason)


# ---------------------------------------------------------------------------
# TestManage
# ---------------------------------------------------------------------------

class TestManage(unittest.TestCase):

    def _make_plugin(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        return OilMomentumV1Strategy()

    def _make_position(
        self,
        condition_id: str = "cond_oil_1",
        outcome: str = "YES",
        avg_entry_price: float = 0.45,
        opened_ts: int | None = None,
    ) -> dict:
        return {
            "condition_id": condition_id,
            "outcome": outcome,
            "avg_entry_price": avg_entry_price,
            "opened_ts": opened_ts or int(time.time()),
        }

    def test_market_open_returns_live_price(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil exceed $80?", is_open=1)
        plugin = self._make_plugin()
        position = self._make_position()
        strategy = _default_strategy()

        with patch("sniper.strategies.oil_momentum_v1.fetch_live_clob_price", return_value=0.55):
            state = plugin.manage(conn, strategy, position)

        self.assertAlmostEqual(state.current_price, 0.55)
        self.assertTrue(state.signal_open)

    def test_market_closed_returns_signal_open_false(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil exceed $80?", is_open=0)
        plugin = self._make_plugin()
        position = self._make_position()
        strategy = _default_strategy()

        with patch("sniper.strategies.oil_momentum_v1.fetch_live_clob_price", return_value=0.55):
            state = plugin.manage(conn, strategy, position)

        self.assertFalse(state.signal_open)

    def test_fallback_to_entry_price_when_live_price_unavailable(self):
        conn = _in_memory_conn()
        _seed_market(conn, "cond_oil_1", "Will oil exceed $80?", is_open=1)
        plugin = self._make_plugin()
        position = self._make_position(avg_entry_price=0.45)
        strategy = _default_strategy()

        with patch("sniper.strategies.oil_momentum_v1.fetch_live_clob_price", return_value=None):
            state = plugin.manage(conn, strategy, position)

        self.assertAlmostEqual(state.current_price, 0.45)


# ---------------------------------------------------------------------------
# TestExit
# ---------------------------------------------------------------------------

class TestExit(unittest.TestCase):

    def _make_plugin(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        return OilMomentumV1Strategy()

    def _make_position(
        self,
        avg_entry_price: float = 0.45,
        opened_ts: int | None = None,
    ) -> dict:
        return {
            "condition_id": "cond_oil_1",
            "outcome": "YES",
            "avg_entry_price": avg_entry_price,
            "opened_ts": opened_ts or int(time.time()),
        }

    def _make_state(self, price: float = 0.50, signal_open: bool = True):
        from sniper.strategies.base import StrategyManageState
        return StrategyManageState(current_price=price, signal_open=signal_open)

    def test_exit_market_closed(self):
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        position = self._make_position()
        state = self._make_state(price=0.50, signal_open=False)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "market_closed")

    def test_exit_early_close_win(self):
        """Price >= 0.97 → early_close_win."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        position = self._make_position()
        state = self._make_state(price=0.98, signal_open=True)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "early_close_win")

    def test_exit_early_close_loss(self):
        """Price <= 0.03 → early_close_loss."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        position = self._make_position()
        state = self._make_state(price=0.02, signal_open=True)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "early_close_loss")

    def test_exit_take_profit(self):
        """ROI >= 30% → take_profit."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        # Entry 0.40, current 0.52 → ROI = (0.52 - 0.40) / 0.40 = 30%
        position = self._make_position(avg_entry_price=0.40)
        state = self._make_state(price=0.52, signal_open=True)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.reason, "take_profit")

    def test_exit_stale(self):
        """Position open >= 14 days → stale_Xd."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        old_ts = int(time.time()) - 15 * 86400
        position = self._make_position(opened_ts=old_ts)
        state = self._make_state(price=0.50, signal_open=True)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertTrue(decision.should_exit)
        self.assertTrue(decision.reason.startswith("stale_"))

    def test_hold(self):
        """All conditions unmet → hold."""
        conn = _in_memory_conn()
        plugin = self._make_plugin()
        position = self._make_position(avg_entry_price=0.45)
        state = self._make_state(price=0.50, signal_open=True)

        decision = plugin.exit(conn, _default_strategy(), position, state)

        self.assertFalse(decision.should_exit)
        self.assertEqual(decision.reason, "hold")


# ---------------------------------------------------------------------------
# TestOilMomentumRegistry
# ---------------------------------------------------------------------------

class TestOilMomentumRegistry(unittest.TestCase):

    def test_lookup_by_plugin_id(self):
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("oil_momentum_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "oil_momentum_v1")

    def test_plugin_attributes(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        plugin = OilMomentumV1Strategy()
        self.assertEqual(plugin.plugin_id, "oil_momentum_v1")
        self.assertEqual(plugin.display_name, "Oil Momentum V1")
        self.assertEqual(plugin.strategy_family, "commodity_momentum")
        self.assertEqual(plugin.required_feeds, ("polymarket", "commodities"))
        self.assertEqual(plugin.supported_execution_modes, ("paper",))
        self.assertEqual(plugin.default_loop_seconds, 300)

    def test_replay_adapter_returns_none(self):
        from sniper.strategies.oil_momentum_v1 import OilMomentumV1Strategy
        plugin = OilMomentumV1Strategy()
        self.assertIsNone(plugin.replay_adapter())


if __name__ == "__main__":
    unittest.main()
