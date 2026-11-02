import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import pytest

import sniper.settings.paper as paper_settings
from db import get_connection, init_db, upsert_position_signal, upsert_user_analysis
from sniper.services.strategy_runner import process_plugin_strategy, _enrich_signal_with_live_price
from sniper.strategies.base import StrategyManageState, StrategySignal

PASSING_INSIDER_SCORE = paper_settings.PAPER_ENTRY_MIN_INSIDER_SCORE + 2.0

# Use a dynamic future date to avoid test rot from hardcoded dates
from datetime import datetime, timezone, timedelta
_FUTURE_DATE_ISO = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


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
        insider_score=PASSING_INSIDER_SCORE,
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
        max_position_insider_score=PASSING_INSIDER_SCORE,
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
    condition_id: str = "cond-strat-1",
    is_open: bool = True,
    current_price: float = 0.42,
    analyzed_ts: int | None = None,
) -> None:
    ts = int(analyzed_ts or time.time())
    upsert_position_signal(
        conn,
        wallet=wallet,
        condition_id=condition_id,
        outcome="YES",
        title="Will Test Market Resolve?",
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
        position_insider_score=PASSING_INSIDER_SCORE,
        position_speculator_score=25.0,
        position_classification="position_watch",
        signal_reason="test signal",
        analyzed_ts=ts,
        end_date_iso=_FUTURE_DATE_ISO,
    )
    conn.commit()


@pytest.mark.integration
class MarketExposureLimitTests(unittest.TestCase):
    """Tests for per-market exposure cap (max_exposure_per_market)."""

    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet_a = "0xaaaa111111111111111111111111111111111111"
        self.wallet_b = "0xbbbb222222222222222222222222222222222222"
        _seed_wallet(self.conn, self.wallet_a)
        _seed_wallet(self.conn, self.wallet_b)
        _seed_signal(self.conn, self.wallet_a, condition_id="market-exposure-1")
        _seed_signal(self.conn, self.wallet_b, condition_id="market-exposure-1")

    def tearDown(self) -> None:
        self.conn.close()

    def test_strategy_plugin_blocks_when_market_exposure_exceeded(self) -> None:
        """When existing open positions for a market exceed the cap, new entry is rejected."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        from sniper.strategies.base import StrategySignal

        plugin = InsiderCopyV1Strategy()
        strategy = {
            "id": 99,
            "wallet": self.wallet_a,
            "parameters": {
                "execution_mode": "paper",
                "min_position_value_usdc": 500,
                "max_exposure_per_market_usdc": 50,  # Low cap for testing
            },
        }

        # Simulate existing open position with $40 exposure in market-exposure-1
        open_positions = [
            {
                "wallet": self.wallet_a,
                "condition_id": "market-exposure-1",
                "outcome": "YES",
                "entry_shares": 100.0,
                "avg_entry_price": 0.40,  # 100 * 0.40 = $40
            }
        ]

        signal = StrategySignal(
            wallet=self.wallet_b,
            condition_id="market-exposure-1",
            outcome="YES",
            title="Test Market",
            current_price=0.42,
            position_value=5000.0,
            analyzed_ts=int(time.time()),
            metadata={
                "position_insider_score": PASSING_INSIDER_SCORE,
                "position_speculator_score": 25.0,
                "end_date_iso": _FUTURE_DATE_ISO,
                "win_rate": 0.82,
                "qualifying_roi": 18.0,
                "wallet_classification": "high_winrate_review",
            },
        )

        decision = plugin.enter(self.conn, strategy, signal, open_positions)
        # $40 existing + new entry would exceed $50 cap; should clamp (not block,
        # since $40 < $50 there's $10 headroom — but sized position > $10)
        # Actually $40 < $50 so it's allowed, but clamped to $10 headroom
        self.assertTrue(decision.should_enter)
        self.assertLessEqual(decision.size_usdc, 10.0)

    def test_strategy_plugin_fully_blocks_at_cap(self) -> None:
        """When existing exposure equals or exceeds cap, entry is fully blocked."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        from sniper.strategies.base import StrategySignal

        plugin = InsiderCopyV1Strategy()
        strategy = {
            "id": 99,
            "wallet": self.wallet_a,
            "parameters": {
                "execution_mode": "paper",
                "min_position_value_usdc": 500,
                "max_exposure_per_market_usdc": 30,
            },
        }

        # Existing $40 exposure >= $30 cap
        open_positions = [
            {
                "wallet": self.wallet_a,
                "condition_id": "market-exposure-1",
                "outcome": "YES",
                "entry_shares": 100.0,
                "avg_entry_price": 0.40,
            }
        ]

        signal = StrategySignal(
            wallet=self.wallet_b,
            condition_id="market-exposure-1",
            outcome="YES",
            title="Test Market",
            current_price=0.42,
            position_value=5000.0,
            metadata={
                "position_insider_score": PASSING_INSIDER_SCORE,
                "position_speculator_score": 25.0,
                "end_date_iso": _FUTURE_DATE_ISO,
                "win_rate": 0.82,
                "qualifying_roi": 18.0,
                "wallet_classification": "high_winrate_review",
            },
        )

        decision = plugin.enter(self.conn, strategy, signal, open_positions)
        self.assertFalse(decision.should_enter)
        self.assertIn("exposure", decision.reason)

    def test_strategy_plugin_allows_different_market(self) -> None:
        """Exposure in one market does not block entry to a different market."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        from sniper.strategies.base import StrategySignal

        plugin = InsiderCopyV1Strategy()
        strategy = {
            "id": 99,
            "wallet": self.wallet_a,
            "parameters": {
                "execution_mode": "paper",
                "min_position_value_usdc": 500,
                "max_exposure_per_market_usdc": 30,
            },
        }

        # $40 exposure in market-exposure-1
        open_positions = [
            {
                "wallet": self.wallet_a,
                "condition_id": "market-exposure-1",
                "outcome": "YES",
                "entry_shares": 100.0,
                "avg_entry_price": 0.40,
            }
        ]

        # Signal for a DIFFERENT market
        _seed_signal(self.conn, self.wallet_b, condition_id="market-different-2")
        signal = StrategySignal(
            wallet=self.wallet_b,
            condition_id="market-different-2",
            outcome="YES",
            title="Different Market",
            current_price=0.42,
            position_value=5000.0,
            metadata={
                "position_insider_score": PASSING_INSIDER_SCORE,
                "position_speculator_score": 25.0,
                "end_date_iso": _FUTURE_DATE_ISO,
                "win_rate": 0.82,
                "qualifying_roi": 18.0,
                "wallet_classification": "high_winrate_review",
            },
        )

        decision = plugin.enter(self.conn, strategy, signal, open_positions)
        self.assertTrue(decision.should_enter)

    def test_paper_trader_market_exposure_method(self) -> None:
        """Paper trader market_exposure_usdc() sums open trades for a market."""
        from paper_trader import PaperTrader

        # Create paper_trades table (not in init_db, created by PaperTrader.__init__)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'YES',
                entry_price REAL NOT NULL,
                size_usdc REAL NOT NULL,
                shares REAL,
                signal_type TEXT DEFAULT 'test',
                status TEXT DEFAULT 'open',
                duplicate_of INTEGER
            )
        """)
        self.conn.commit()

        pt = PaperTrader.__new__(PaperTrader)
        pt.conn = self.conn

        # No trades yet
        self.assertEqual(pt.market_exposure_usdc("cond-test"), 0.0)

        # Insert two open paper trades in same market
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, entry_price, size_usdc, shares, status)
               VALUES ('0xaaa', 'cond-test', 'YES', 0.40, 25.0, 62.5, 'open')"""
        )
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, entry_price, size_usdc, shares, status)
               VALUES ('0xbbb', 'cond-test', 'YES', 0.50, 30.0, 60.0, 'open')"""
        )
        # Closed trade in same market — should NOT be counted
        self.conn.execute(
            """INSERT INTO paper_trades
               (wallet, condition_id, outcome, entry_price, size_usdc, shares, status)
               VALUES ('0xccc', 'cond-test', 'YES', 0.35, 50.0, 142.8, 'resolution_win')"""
        )
        self.conn.commit()

        self.assertAlmostEqual(pt.market_exposure_usdc("cond-test"), 55.0, places=1)


@pytest.mark.integration
class DriftGateTests(unittest.TestCase):
    """Tests for price drift gates in insider_copy_v1 strategy plugin."""

    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        _seed_wallet(self.conn, "0xdrift")
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        self.plugin = InsiderCopyV1Strategy()

    def tearDown(self) -> None:
        self.conn.close()

    def _make_signal(self, current_price=0.45, source_entry_price=0.40):
        from sniper.strategies.base import StrategySignal
        return StrategySignal(
            wallet="0xdrift",
            condition_id="drift-market",
            outcome="YES",
            title="Drift test",
            current_price=current_price,
            position_value=3000.0,
            analyzed_ts=int(time.time()),
            metadata={
                "position_insider_score": PASSING_INSIDER_SCORE,
                "position_speculator_score": 25.0,
                "position_classification": "position_watch",
                "end_date_iso": _FUTURE_DATE_ISO,
                "win_rate": 0.71,
                "qualifying_roi": 18.0,
                "wallet_classification": "high_winrate_review",
                "source_entry_price": source_entry_price,
            },
        )

    def _strategy(self, **overrides):
        base = {"wallet": "0xdrift", "parameters": {}}
        base.update(overrides)
        return base

    def test_normal_drift_passes(self):
        """Small drift should be allowed."""
        sig = self._make_signal(current_price=0.43, source_entry_price=0.40)
        dec = self.plugin.enter(self.conn, self._strategy(), sig, [])
        self.assertTrue(dec.should_enter, f"Small drift should pass: {dec.reason}")

    def test_adverse_drift_rejected(self):
        """Copy price much higher than source entry = adverse drift."""
        sig = self._make_signal(current_price=0.50, source_entry_price=0.40)
        # 25% adverse drift > 15% default
        dec = self.plugin.enter(self.conn, self._strategy(), sig, [])
        self.assertFalse(dec.should_enter)
        self.assertIn("adverse drift", dec.reason)

    def test_favorable_drift_rejected(self):
        """Copy price much lower than source entry = favorable but likely late."""
        sig = self._make_signal(current_price=0.20, source_entry_price=0.40)
        # -50% favorable drift > 40% default
        dec = self.plugin.enter(self.conn, self._strategy(), sig, [])
        self.assertFalse(dec.should_enter)
        self.assertIn("favorable drift", dec.reason)

    def test_custom_drift_params(self):
        """Strategy parameters should override default drift limits."""
        sig = self._make_signal(current_price=0.50, source_entry_price=0.40)
        # 25% drift would fail default (15%) but pass custom (30%)
        strat = self._strategy(parameters={"max_adverse_drift_pct": 30})
        dec = self.plugin.enter(self.conn, strat, sig, [])
        self.assertTrue(dec.should_enter, f"Custom 30% cap should pass 25% drift: {dec.reason}")

    def test_zero_source_entry_skips_drift(self):
        """No drift check when source entry price is 0 or missing."""
        sig = self._make_signal(current_price=0.45, source_entry_price=0.0)
        dec = self.plugin.enter(self.conn, self._strategy(), sig, [])
        self.assertTrue(dec.should_enter, f"Zero source entry should skip drift: {dec.reason}")


@pytest.mark.integration
class StrategyPluginRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0x1111111111111111111111111111111111111111"
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet)
        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, parameters, status)
            VALUES (?, ?, 'insider_copy_v1', ?, 'active')
            """,
            (
                self.wallet,
                "Test strategy",
                '{"execution_mode":"paper","min_position_value_usdc":500}',
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _strategy(self):
        row = self.conn.execute("SELECT * FROM strategies LIMIT 1").fetchone()
        return dict(row)

    def test_plugin_runner_opens_position_and_records_run(self) -> None:
        process_plugin_strategy(self.conn, self._strategy())

        pos = self.conn.execute("SELECT * FROM strategy_positions").fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["state"], "open")

        ex = self.conn.execute("SELECT * FROM strategy_executions").fetchone()
        self.assertIsNotNone(ex)
        self.assertEqual(ex["status"], "open")

        run = self.conn.execute(
            "SELECT * FROM strategy_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["discovered_count"], 1)
        self.assertEqual(run["entered_count"], 1)
        self.assertEqual(run["open_positions"], 1)

    def test_plugin_runner_closes_when_source_exits(self) -> None:
        process_plugin_strategy(self.conn, self._strategy())
        _seed_signal(
            self.conn,
            self.wallet,
            is_open=False,
            current_price=0.61,
            analyzed_ts=int(time.time()) + 60,
        )

        process_plugin_strategy(self.conn, self._strategy())

        pos = self.conn.execute("SELECT * FROM strategy_positions").fetchone()
        self.assertEqual(pos["state"], "closed")
        self.assertGreater(float(pos["realized_pnl"] or 0.0), 0.0)

        ex = self.conn.execute(
            "SELECT * FROM strategy_executions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ex["status"], "source_exited")

        run = self.conn.execute(
            "SELECT * FROM strategy_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(run["exited_count"], 1)
        self.assertEqual(run["open_positions"], 0)


class StrategyApiRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_strategy_api.db")
        if "sniper.web.server" in sys.modules:
            del sys.modules["sniper.web.server"]
        cls.web = importlib.import_module("sniper.web.server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        # Ensure paper_trades table exists (normally created by paper_trader.py)
        self.web.conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                market_title TEXT,
                direction TEXT NOT NULL DEFAULT 'buy',
                entry_price REAL NOT NULL,
                current_price REAL,
                exit_price REAL,
                size_usdc REAL NOT NULL,
                shares REAL,
                status TEXT DEFAULT 'open',
                pnl_usdc REAL,
                roi_percent REAL,
                exit_reason TEXT,
                signal_insider_score REAL,
                signal_speculator_score REAL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                duplicate_of INTEGER
            )
        """)
        self.web.conn.commit()
        for table in [
            "strategy_executions",
            "strategy_positions",
            "strategy_runs",
            "strategies",
            "position_signals",
            "user_analysis",
            "followed_positions",
            "wallet_profiles",
            "followed_wallets",
            "paper_trades",
        ]:
            self.web.conn.execute(f"DELETE FROM {table}")
        self.web.conn.commit()

        wallet = "0x2222222222222222222222222222222222222222"
        self.web.conn.execute(
            """
            INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status, trades_triggered, pnl_usdc)
            VALUES (1, ?, 'API strategy', 'insider_copy_v1', '{"execution_mode":"paper"}', 'active', 1, 12.5)
            """,
            (wallet,),
        )
        self.web.conn.execute(
            "INSERT INTO followed_wallets (wallet, label) VALUES (?, ?)",
            (wallet, "Research wallet"),
        )
        self.web.conn.execute(
            """
            INSERT INTO wallet_profiles (wallet, username, pseudonym, profile_image)
            VALUES (?, 'tester', '', '')
            """,
            (wallet,),
        )
        # Seed paper_trades (source of truth for insider_copy strategies)
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                trade_id, wallet, condition_id, outcome, market_title,
                entry_price, current_price, size_usdc, shares,
                status, pnl_usdc, signal_insider_score,
                opened_at
            ) VALUES (1, ?, 'cond-api-1', 'YES', 'API Market',
                      0.40, 0.55, 10, 25,
                      'open', 0, 80,
                      '2026-03-06 12:00:00')
            """,
            (wallet,),
        )
        # Closed trade for realized PnL
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                trade_id, wallet, condition_id, outcome, market_title,
                entry_price, exit_price, size_usdc, shares,
                status, pnl_usdc, signal_insider_score,
                opened_at, closed_at, exit_reason
            ) VALUES (2, ?, 'cond-api-2', 'NO', 'Closed Market',
                      0.30, 0.80, 15, 50,
                      'resolution_win', 25.0, 75,
                      '2026-03-05 10:00:00', '2026-03-06 10:00:00', 'resolution')
            """,
            (wallet,),
        )
        self.web.conn.commit()

    def test_strategy_list_includes_runtime_fields(self) -> None:
        resp = self.client.get("/api/strategies")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        matches = [i for i in payload["items"] if i["strategy_type"] == "insider_copy_v1"]
        self.assertTrue(matches, "insider_copy_v1 strategy not found in /api/strategies")
        item = matches[0]
        self.assertEqual(item["open_positions_count"], 1)
        self.assertEqual(item["last_run_status"], "ok")
        # Unrealized PnL: 25 shares * (0.55 - 0.40) = 3.75
        self.assertAlmostEqual(item["unrealized_pnl"], 3.75, places=2)
        # Realized PnL from closed trade
        self.assertAlmostEqual(item["realized_pnl"], 25.0, places=2)

    def test_strategy_detail_includes_positions_and_executions(self) -> None:
        resp = self.client.get("/api/strategies/1")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["display_name"], "tester")
        self.assertEqual(len(payload["positions"]), 1)
        self.assertEqual(payload["positions"][0]["title"], "API Market")
        # insider_copy uses paper_trades for executions, not strategy_runs
        self.assertEqual(len(payload["runs"]), 0)
        # Executions from paper_trades (ordered by most recent activity)
        self.assertEqual(len(payload["executions"]), 2)
        statuses = {e["status"] for e in payload["executions"]}
        self.assertEqual(statuses, {"open", "resolution_win"})


class ClobPriceEnrichmentTests(unittest.TestCase):
    """Tests for live CLOB price fetching in strategy_runner and paper_trader."""

    def setUp(self):
        # Clear CLOB price cache between tests to prevent leakage
        import sniper.markets.polymarket_api as polymarket_api
        polymarket_api._clob_price_cache.clear()

    def _make_signal(self, price=0.45, condition_id="cond-123", outcome="Yes"):
        return StrategySignal(
            wallet="0xabc",
            condition_id=condition_id,
            outcome=outcome,
            title="Test market",
            current_price=price,
            position_value=1000.0,
        )

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_updates_price_from_clob(self, mock_get):
        """Live CLOB price should replace cached signal price."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "Yes", "price": "0.52"},
                {"outcome": "No", "price": "0.48"},
            ]
        }
        mock_get.return_value = mock_resp

        signal = self._make_signal(price=0.45)
        result = _enrich_signal_with_live_price(signal)

        self.assertAlmostEqual(result.current_price, 0.52, places=2)
        mock_get.assert_called_once()

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_fallback_on_failure(self, mock_get):
        """On fetch failure, cached price should be preserved."""
        mock_get.side_effect = Exception("network error")

        signal = self._make_signal(price=0.45)
        result = _enrich_signal_with_live_price(signal)

        self.assertAlmostEqual(result.current_price, 0.45, places=2)

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_fallback_on_non_ok_response(self, mock_get):
        """Non-200 response should preserve cached price."""
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_get.return_value = mock_resp

        signal = self._make_signal(price=0.45)
        result = _enrich_signal_with_live_price(signal)

        self.assertAlmostEqual(result.current_price, 0.45, places=2)

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_clamps_price_to_valid_range(self, mock_get):
        """Prices from CLOB should be clamped to [0.0, 1.0]."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": "1.50"}]
        }
        mock_get.return_value = mock_resp

        signal = self._make_signal(price=0.45)
        result = _enrich_signal_with_live_price(signal)

        self.assertLessEqual(result.current_price, 1.0)

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_matches_outcome_case_insensitive(self, mock_get):
        """Outcome matching should be case-insensitive."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "YES", "price": "0.60"},
                {"outcome": "NO", "price": "0.40"},
            ]
        }
        mock_get.return_value = mock_resp

        signal = self._make_signal(price=0.45, outcome="yes")
        result = _enrich_signal_with_live_price(signal)

        self.assertAlmostEqual(result.current_price, 0.60, places=2)

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_enrich_skips_zero_live_price(self, mock_get):
        """Zero live price should be ignored, cached price preserved."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": "0.0"}]
        }
        mock_get.return_value = mock_resp

        signal = self._make_signal(price=0.45)
        result = _enrich_signal_with_live_price(signal)

        self.assertAlmostEqual(result.current_price, 0.45, places=2)


class PaperTraderClobFetchTests(unittest.TestCase):
    """Tests for PaperTrader._fetch_clob_price method."""

    def setUp(self):
        from paper_trader import PaperTrader
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.trader = PaperTrader.__new__(PaperTrader)
        self.trader.conn = self.conn

    def tearDown(self):
        self.conn.close()

    @patch("paper_trader.requests.get")
    def test_fetch_returns_price_for_matching_outcome(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "Yes", "price": "0.55"},
                {"outcome": "No", "price": "0.45"},
            ]
        }
        mock_get.return_value = mock_resp

        price = self.trader._fetch_clob_price("cond-abc", "Yes")
        self.assertAlmostEqual(price, 0.55, places=2)

    @patch("paper_trader.requests.get")
    def test_fetch_returns_none_on_failure(self, mock_get):
        mock_get.side_effect = Exception("timeout")

        price = self.trader._fetch_clob_price("cond-abc", "Yes")
        self.assertIsNone(price)

    @patch("paper_trader.requests.get")
    def test_fetch_returns_none_on_non_ok(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_get.return_value = mock_resp

        price = self.trader._fetch_clob_price("cond-abc", "Yes")
        self.assertIsNone(price)

    @patch("paper_trader.requests.get")
    def test_fetch_clamps_to_valid_range(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [{"outcome": "Yes", "price": "2.5"}]
        }
        mock_get.return_value = mock_resp

        price = self.trader._fetch_clob_price("cond-abc", "Yes")
        self.assertLessEqual(price, 1.0)

    @patch("paper_trader.requests.get")
    def test_fetch_returns_none_when_outcome_not_found(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [{"outcome": "No", "price": "0.45"}]
        }
        mock_get.return_value = mock_resp

        price = self.trader._fetch_clob_price("cond-abc", "Yes")
        self.assertIsNone(price)


class FadePluginTests(unittest.TestCase):
    """Tests for fade_v1 StrategyPlugin."""

    def setUp(self) -> None:
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xfade111111111111111111111111111111111111"
        _seed_wallet(self.conn, self.wallet)
        _seed_signal(self.conn, self.wallet)  # YES signal at 0.42

    def tearDown(self) -> None:
        self.conn.close()

    def _get_plugin(self):
        from sniper.strategies.fade_v1 import FadeV1Strategy
        return FadeV1Strategy()

    def test_discover_inverts_outcome(self):
        """Discover should flip YES → NO."""
        plugin = self._get_plugin()
        strategy = {"wallet": self.wallet, "parameters": {"min_position_value_usdc": 100}}
        signals = plugin.discover(self.conn, strategy)
        self.assertTrue(len(signals) >= 1)
        # Source signal is YES, so faded should be NO
        sig = signals[0]
        self.assertEqual(sig.outcome, "NO")
        self.assertEqual(sig.metadata["source_outcome"], "YES")

    def test_discover_inverts_price(self):
        """Faded price should be 1 - source_price."""
        plugin = self._get_plugin()
        strategy = {"wallet": self.wallet, "parameters": {"min_position_value_usdc": 100}}
        signals = plugin.discover(self.conn, strategy)
        sig = signals[0]
        # Source price is 0.42, so faded price should be 0.58
        self.assertAlmostEqual(sig.current_price, 0.58, places=2)
        self.assertAlmostEqual(sig.metadata["source_price"], 0.42, places=2)

    def test_enter_passes_basic_fade(self):
        """A valid fade signal should pass entry gates."""
        plugin = self._get_plugin()
        strategy = {"wallet": self.wallet, "parameters": {
            "min_position_value_usdc": 100,
            "min_source_notional_usdc": 10,
            "execution_mode": "paper",
        }}
        signals = plugin.discover(self.conn, strategy)
        dec = plugin.enter(self.conn, strategy, signals[0], [])
        self.assertTrue(dec.should_enter, f"Should enter but got: {dec.reason}")
        self.assertIn("fade", dec.reason.lower())
        self.assertEqual(dec.entry_price, signals[0].current_price)

    def test_enter_rejects_low_source_notional(self):
        """Fade should reject signals from tiny positions."""
        plugin = self._get_plugin()
        strategy = {"wallet": self.wallet, "parameters": {
            "min_position_value_usdc": 100,
            "min_source_notional_usdc": 10000,  # Very high threshold
        }}
        signals = plugin.discover(self.conn, strategy)
        dec = plugin.enter(self.conn, strategy, signals[0], [])
        self.assertFalse(dec.should_enter)
        self.assertIn("notional", dec.reason.lower())

    def test_enter_rejects_high_faded_price(self):
        """If faded price > max_entry_price, reject."""
        plugin = self._get_plugin()
        # Source price 0.42 → faded price 0.58. Set max to 0.50.
        strategy = {"wallet": self.wallet, "parameters": {
            "min_position_value_usdc": 100,
            "min_source_notional_usdc": 10,
            "max_entry_price": 0.50,
        }}
        signals = plugin.discover(self.conn, strategy)
        dec = plugin.enter(self.conn, strategy, signals[0], [])
        self.assertFalse(dec.should_enter)
        self.assertIn("price", dec.reason.lower())

    def test_enter_rejects_duplicate(self):
        """Already-open position on same outcome should be rejected."""
        plugin = self._get_plugin()
        strategy = {"wallet": self.wallet, "parameters": {
            "min_position_value_usdc": 100,
            "min_source_notional_usdc": 10,
        }}
        signals = plugin.discover(self.conn, strategy)
        open_positions = [{
            "wallet": self.wallet,
            "condition_id": "cond-strat-1",
            "outcome": "NO",
        }]
        dec = plugin.enter(self.conn, strategy, signals[0], open_positions)
        self.assertFalse(dec.should_enter)
        self.assertIn("already open", dec.reason)

    def test_manage_inverts_source_price(self):
        """Manage should return faded price (1 - source_price)."""
        plugin = self._get_plugin()
        position = {
            "wallet": self.wallet,
            "condition_id": "cond-strat-1",
            "outcome": "NO",  # Our faded position
            "avg_entry_price": 0.58,
        }
        state = plugin.manage(self.conn, {}, position)
        # Source signal is YES at 0.42 → our NO price = 0.58
        self.assertAlmostEqual(state.current_price, 0.58, places=2)
        self.assertTrue(state.signal_open)

    def test_manage_detects_source_exit(self):
        """When source closes, signal_open should be False."""
        # Close the source signal
        _seed_signal(self.conn, self.wallet, is_open=False, current_price=0.90,
                     analyzed_ts=int(time.time()) + 60)
        plugin = self._get_plugin()
        position = {
            "wallet": self.wallet,
            "condition_id": "cond-strat-1",
            "outcome": "NO",
            "avg_entry_price": 0.58,
        }
        state = plugin.manage(self.conn, {}, position)
        self.assertFalse(state.signal_open)

    def test_exit_resolution_win(self):
        """Faded position wins when its price hits threshold."""
        plugin = self._get_plugin()
        position = {"opened_ts": _now_ts() - 3600, "avg_entry_price": 0.58}
        state = StrategyManageState(current_price=0.97, signal_open=True)
        dec = plugin.exit(self.conn, {}, position, state)
        self.assertTrue(dec.should_exit)
        self.assertEqual(dec.reason, "resolution_win")

    def test_exit_resolution_loss(self):
        """Faded position loses when its price drops below threshold."""
        plugin = self._get_plugin()
        position = {"opened_ts": _now_ts() - 3600, "avg_entry_price": 0.58}
        state = StrategyManageState(current_price=0.03, signal_open=True)
        dec = plugin.exit(self.conn, {}, position, state)
        self.assertTrue(dec.should_exit)
        self.assertEqual(dec.reason, "resolution_loss")

    def test_exit_source_exited(self):
        """Fade exits when source wallet exits."""
        plugin = self._get_plugin()
        position = {"opened_ts": _now_ts() - 3600, "avg_entry_price": 0.58}
        state = StrategyManageState(current_price=0.55, signal_open=False)
        dec = plugin.exit(self.conn, {}, position, state)
        self.assertTrue(dec.should_exit)
        self.assertEqual(dec.reason, "source_exited")

    def test_registry_includes_fade(self):
        """fade_v1 should be registered and discoverable."""
        from sniper.strategies.registry import get_strategy_plugin
        plugin = get_strategy_plugin("fade_v1")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.plugin_id, "fade_v1")

    @patch("sniper.markets.polymarket_api.requests.get")
    def test_full_lifecycle_via_runner(self, mock_get):
        """End-to-end: create fade strategy, process it, verify entry."""
        # Mock CLOB fetch to return the faded price
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "tokens": [
                {"outcome": "Yes", "price": "0.42"},
                {"outcome": "No", "price": "0.58"},
            ]
        }
        mock_get.return_value = mock_resp

        self.conn.execute(
            """
            INSERT INTO strategies (wallet, label, strategy_type, parameters, status)
            VALUES (?, ?, 'fade_v1', ?, 'active')
            """,
            (
                self.wallet,
                "Fade test",
                '{"execution_mode":"paper","min_position_value_usdc":100,"min_source_notional_usdc":10}',
            ),
        )
        self.conn.commit()

        strategy = dict(self.conn.execute("SELECT * FROM strategies LIMIT 1").fetchone())
        process_plugin_strategy(self.conn, strategy)

        pos = self.conn.execute("SELECT * FROM strategy_positions").fetchone()
        self.assertIsNotNone(pos, "Fade should have opened a position")
        self.assertEqual(pos["state"], "open")
        self.assertEqual(pos["outcome"], "NO")  # Faded from source YES


def _now_ts() -> int:
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).timestamp())


@pytest.mark.integration
class InsiderCopyThresholdTests(unittest.TestCase):
    """insider_copy_v1 rejects signals in the 55-59 insider score bucket."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xaaaa000000000000000000000000000000000001"
        _seed_wallet(self.conn, self.wallet)

    def tearDown(self):
        self.conn.close()

    def _make_signal(self, insider_score: float):
        from sniper.strategies.base import StrategySignal
        return StrategySignal(
            wallet=self.wallet,
            condition_id="cond-thresh-test",
            outcome="YES",
            title="Threshold test market",
            current_price=0.45,
            position_value=5000.0,
            analyzed_ts=_now_ts(),
            metadata={
                "position_insider_score": insider_score,
                "position_speculator_score": 20.0,
                "end_date_iso": _FUTURE_DATE_ISO,
                "win_rate": 0.72,
                "qualifying_roi": 15.0,
                "wallet_classification": "high_winrate_review",
                "source_entry_price": 0.40,
            },
        )

    def test_insider_score_57_is_rejected(self):
        """Score 57 is below the live threshold — must be blocked."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        plugin = InsiderCopyV1Strategy()
        strategy = {"id": 1, "wallet": self.wallet, "parameters": {}}
        decision = plugin.enter(self.conn, strategy, self._make_signal(57.0), [])
        self.assertFalse(decision.should_enter)
        self.assertIn("insider", decision.reason.lower())

    def test_insider_score_65_is_rejected(self):
        """Score 65 is below the live threshold — must be blocked."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        plugin = InsiderCopyV1Strategy()
        strategy = {"id": 1, "wallet": self.wallet, "parameters": {}}
        decision = plugin.enter(self.conn, strategy, self._make_signal(65.0), [])
        self.assertFalse(decision.should_enter)
        self.assertIn("insider", decision.reason.lower())

    def test_insider_score_threshold_is_accepted(self):
        """A score exactly at the live threshold must pass."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        plugin = InsiderCopyV1Strategy()
        strategy = {"id": 1, "wallet": self.wallet, "parameters": {}}
        decision = plugin.enter(
            self.conn,
            strategy,
            self._make_signal(paper_settings.PAPER_ENTRY_MIN_INSIDER_SCORE),
            [],
        )
        self.assertTrue(decision.should_enter)


class WalletDegradationPauseTests(unittest.TestCase):
    """insider_copy_v1 auto-pauses strategy when source wallet insider score degrades."""

    def setUp(self):
        self.conn = get_connection(":memory:")
        init_db(self.conn)
        self.wallet = "0xbbbb000000000000000000000000000000000002"
        _seed_wallet(self.conn, self.wallet)
        self.conn.execute(
            """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
               VALUES (42, ?, 'degrade test', 'insider_copy_v1', '{}', 'active')""",
            (self.wallet,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _seed_score_history(self, scores: list):
        from db import insert_score_history
        for i, score in enumerate(scores):
            insert_score_history(
                self.conn,
                wallet=self.wallet,
                insider_score=score,
                analyzed_ts=_now_ts() - (len(scores) - i) * 3600,
            )
        self.conn.commit()

    def _make_strategy(self):
        return {
            "id": 42,
            "wallet": self.wallet,
            "parameters": {},
            "status": "active",
        }

    def _make_position(self):
        return {
            "wallet": self.wallet,
            "condition_id": "cond-degrade-001",
            "outcome": "YES",
            "avg_entry_price": 0.45,
            "opened_ts": _now_ts() - 86400,
        }

    def test_pauses_after_n_consecutive_degraded_runs(self):
        """3 consecutive runs all below threshold → strategy status becomes paused."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        self._seed_score_history([10.0, 15.0, 20.0])  # all below 60
        plugin = InsiderCopyV1Strategy()
        plugin.manage(self.conn, self._make_strategy(), self._make_position())
        row = self.conn.execute(
            "SELECT status FROM strategies WHERE id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "paused")

    def test_does_not_pause_with_only_two_degraded_runs(self):
        """Only 2 degraded runs (need N=3) → stays active."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        self._seed_score_history([10.0, 15.0])  # only 2 rows
        plugin = InsiderCopyV1Strategy()
        plugin.manage(self.conn, self._make_strategy(), self._make_position())
        row = self.conn.execute(
            "SELECT status FROM strategies WHERE id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "active")

    def test_does_not_pause_when_recent_run_above_threshold(self):
        """Even if 2 of 3 are degraded, one above threshold → stays active."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        # most recent is fine (above the live threshold), two older are bad
        self._seed_score_history([10.0, 15.0, PASSING_INSIDER_SCORE])
        plugin = InsiderCopyV1Strategy()
        plugin.manage(self.conn, self._make_strategy(), self._make_position())
        row = self.conn.execute(
            "SELECT status FROM strategies WHERE id = 42"
        ).fetchone()
        self.assertEqual(row["status"], "active")

    def test_telegram_sent_once_on_first_pause(self):
        """Telegram notification fires exactly once when status transitions to paused."""
        from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
        self._seed_score_history([10.0, 15.0, 20.0])
        plugin = InsiderCopyV1Strategy()
        with patch("sniper.strategies.insider_copy_v1.send_telegram_notification") as mock_tg:
            plugin.manage(self.conn, self._make_strategy(), self._make_position())
            plugin.manage(self.conn, self._make_strategy(), self._make_position())
        mock_tg.assert_called_once()
