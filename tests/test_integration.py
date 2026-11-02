import importlib
import datetime as dt
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from db import (
    get_connection,
    init_db,
    is_trade_deleted,
    mark_trade_deleted,
    recent_low_buy_trades,
    record_low_buy_trade,
    upsert_market_outcome,
    upsert_market_metadata,
    upsert_position_signal,
    upsert_user_analysis,
    upsert_wallet_profile,
)


class Stage1TriggerPersistenceTests(unittest.TestCase):
    def test_low_price_and_anomaly_trigger_types_are_persisted(self) -> None:
        conn = get_connection(":memory:")
        init_db(conn)

        now_ts = int(time.time())
        trade_low = {
            "transactionHash": "0xtxlow",
            "proxyWallet": "0x1111111111111111111111111111111111111111",
            "conditionId": "cond-low",
            "asset": "asset-low",
            "side": "BUY",
            "price": 0.19,
            "size": 12000,
            "timestamp": now_ts,
            "title": "Low price trade",
            "slug": "low-price-trade",
            "eventSlug": "low-price-trade",
            "outcome": "Yes",
        }
        trade_anomaly = {
            "transactionHash": "0xtxanom",
            "proxyWallet": "0x2222222222222222222222222222222222222222",
            "conditionId": "cond-anom",
            "asset": "asset-anom",
            "side": "BUY",
            "price": 0.68,
            "size": 9000,
            "timestamp": now_ts,
            "title": "Impact anomaly trade",
            "slug": "impact-anomaly-trade",
            "eventSlug": "impact-anomaly-trade",
            "outcome": "Yes",
        }

        self.assertTrue(
            record_low_buy_trade(
                conn,
                trade_low,
                ws_price=0.19,
                ws_timestamp_ms=now_ts * 1000,
                market_volume_24h=100000,
                source="realtime",
                trigger_type="low_price_entry",
                trigger_reason="low price trigger",
            )
        )
        self.assertTrue(
            record_low_buy_trade(
                conn,
                trade_anomaly,
                ws_price=0.68,
                ws_timestamp_ms=now_ts * 1000,
                market_volume_24h=100000,
                source="realtime",
                trigger_type="impact_anomaly",
                trigger_reason="impact anomaly trigger",
            )
        )

        rows = list(recent_low_buy_trades(conn, limit=10))
        trigger_by_tx = {str(row["transaction_hash"]): str(row["trigger_type"]) for row in rows}
        self.assertEqual(trigger_by_tx["0xtxlow"], "low_price_entry")
        self.assertEqual(trigger_by_tx["0xtxanom"], "impact_anomaly")
        conn.close()

    def test_record_low_buy_trade_skips_tombstoned_transaction_hash(self) -> None:
        conn = get_connection(":memory:")
        init_db(conn)

        now_ts = int(time.time())
        tx_hash = "0xtombstoned"
        mark_trade_deleted(conn, tx_hash, deleted_source="test")
        inserted = record_low_buy_trade(
            conn,
            {
                "transactionHash": tx_hash,
                "proxyWallet": "0x1111111111111111111111111111111111111111",
                "conditionId": "cond-1",
                "asset": "asset-1",
                "side": "BUY",
                "price": 0.2,
                "size": 10000,
                "timestamp": now_ts,
                "title": "Should skip",
                "slug": "should-skip",
                "eventSlug": "should-skip",
                "outcome": "Yes",
            },
            ws_price=0.2,
            ws_timestamp_ms=now_ts * 1000,
            market_volume_24h=100000,
        )
        self.assertFalse(inserted)
        self.assertEqual(len(recent_low_buy_trades(conn, limit=10)), 0)
        conn.close()


class WebApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(cls._tmp.name, "test_sniper.db")
        os.environ["SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST"] = "0"
        os.environ["SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST"] = "0"
        os.environ["MAX_UI_TRADES"] = "2000"
        # Strip EVERY credential that turns on an auth guard, so these tests are hermetic no matter
        # what ran before them. Two separate guards exist and both must be off:
        #   * SNIPER_API_KEY          -> the agent-endpoint key check (sniper/settings/web.py)
        #   * SNIPER_DASH_USER/PASS   -> a before_request BASIC-AUTH guard that 401s EVERY route
        #                                (sniper/web/server.py:398 `_DASH_USER`/`_DASH_PASS`)
        #
        # All three are read at MODULE SCOPE, so their values are frozen at import. Several tests
        # (test_env_files, test_health_check_import, ...) call load_env_files(), which leaks the real
        # .env — including SNIPER_DASH_USER/PASS — into os.environ. Any test that then imported the
        # web stack baked basic-auth ON, and this test got `401 Authentication required` (text/html),
        # so .get_json() returned None and it died as "'NoneType' object is not subscriptable".
        # Alone it passed, because nothing had loaded .env yet. Order-dependent, and xdist hid it.
        cls._orig_env = {k: os.environ.pop(k, None)
                         for k in ("SNIPER_API_KEY", "SNIPER_DASH_USER", "SNIPER_DASH_PASS")}
        cls._orig_api_key = cls._orig_env["SNIPER_API_KEY"]   # kept: tearDownClass restores it

        # Purge ONLY the two modules that freeze those values at module scope, so the re-import
        # re-reads the cleared env. Deliberately NOT the sniper.routes.* modules: their blueprints
        # take `ctx` as an argument at registration, so re-importing the server already rebuilds
        # them with fresh values — and purging them mid-session invalidated blueprints that OTHER
        # web tests in the same xdist worker still held (it broke test_api_crypto5m).
        #
        # NOTE: this used to pop "web_settings" — a module that NO LONGER EXISTS (the restructure
        # moved it to sniper/settings/web.py), i.e. the purge was already a silent no-op.
        for mod in ("web_settings", "sniper.settings.web", "sniper.web.server"):
            sys.modules.pop(mod, None)
        cls.web = importlib.import_module("sniper.web.server")
        cls.client = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()
        # Restore every auth credential we stripped (not just the API key — the basic-auth pair
        # must go back too, or we leak a disabled dashboard guard into whatever runs next).
        for key, val in (getattr(cls, "_orig_env", None) or {}).items():
            if val is not None:
                os.environ[key] = val

    def setUp(self) -> None:
        for table in [
            "low_buy_trades",
            "candidate_users",
            "user_analysis",
            "flagged_users",
            "wallet_profiles",
            "position_signals",
            "market_metadata",
            "market_outcomes",
            "followed_trades",
            "followed_positions",
            "dismissed_suspicious_trades",
            "ignored_wallets",
            "watchlist_trades",
            "watchlist_wallets",
            "deleted_trades",
            "alerts_outbox",
            "agent_mutes",
            "redemption_log",
        ]:
            self.web.conn.execute(f"DELETE FROM {table}")
        try:
            self.web.conn.execute("DELETE FROM real_trades")
        except Exception:
            pass
        try:
            self.web.conn.execute("DELETE FROM paper_trades")
        except Exception:
            pass
        self.web.conn.commit()
        with self.web._suspicious_status_cache_lock:
            self.web._suspicious_status_cache.clear()
        with self.web._suspicious_pattern_cache_lock:
            self.web._suspicious_pattern_cache.clear()

        # NO UNIT TEST OPENS A SOCKET (2026-09-22). /api/real-trades runs a best-effort outcome
        # sync per condition, and that calls session.get("https://clob.polymarket.com/markets/...")
        # with a 25 s timeout. These three tests passed for as long as the 2026-07-29 sinkhole
        # answered FAST (*.polymarket.com -> 127.0.0.1, connection refused). The sinkhole has since
        # moved to an unreachable IPv6 address, so the same call HANGS: 25 s x N conditions blows
        # the 60 s pytest timeout, and under xdist the worker dies ("node down") without naming a
        # test — which is why the deploy gate reported only "memecoin suite failed".
        #
        # A 404 is the sync's own first early return, so this exercises the real path to its real
        # exit and upserts nothing. Patched on the CLASS, not per test, because the defect was that
        # only one test in this file ever patched it; that test keeps its own explicit patch, which
        # is what asserts the sync path itself.
        class _NoNetworkResponse:
            status_code = 404
            content = b""

            def raise_for_status(self):
                return None

            def json(self):
                return {}

        class _NoNetworkSession:
            def get(self, *_args, **_kwargs):
                return _NoNetworkResponse()

            def close(self):
                return None

        patcher = patch("sniper.routes.api_followed.build_session",
                        return_value=_NoNetworkSession())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ensure_paper_trades_table(self) -> None:
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                market_title TEXT,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                exit_price REAL,
                size_usdc REAL NOT NULL,
                shares REAL,
                position_value_at_entry REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                signal_type TEXT DEFAULT 'unknown',
                exit_classification TEXT,
                stale_flagged_at TIMESTAMP,
                status TEXT DEFAULT 'open',
                pnl_usdc REAL,
                roi_percent REAL,
                exit_reason TEXT,
                signal_insider_score REAL,
                signal_speculator_score REAL,
                wallet_win_rate REAL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                alert_sent_entry INTEGER DEFAULT 0,
                alert_sent_exit INTEGER DEFAULT 0,
                duplicate_of INTEGER,
                position_signal_id INTEGER
            )
            """
        )
        # The production init path adds the slippage columns after the table exists;
        # the bare CREATE above lacks them and test_get_paper_trades_returns_slippage_fields
        # inserts signal_entry_price (red baseline blocked the ship pipeline 2026-08-22).
        from db import ensure_paper_trades_slippage_columns
        ensure_paper_trades_slippage_columns(self.web.conn)
        self.web.conn.commit()

    def _seed(self, *, include_second_wallet: bool = True) -> tuple[str, str, str, str]:
        now_ts = int(time.time())
        wallet1 = "0x1111111111111111111111111111111111111111"
        wallet2 = "0x2222222222222222222222222222222222222222"
        tx1 = "0xaaaabbbbcccc111122223333444455556666777788889999aaaabbbbccccdddd"
        tx2 = "0xbbbbccccdddd111122223333444455556666777788889999aaaabbbbccccdddd"
        condition1 = "cond-main"
        condition2 = "cond-high-winrate"

        trade1 = {
            "transactionHash": tx1,
            "proxyWallet": wallet1,
            "conditionId": condition1,
            "asset": "asset-main",
            "side": "BUY",
            "price": 0.15,
            "size": 20000,
            "timestamp": now_ts,
            "title": "Will launch happen by Friday?",
            "slug": "will-launch-happen-by-friday",
            "eventSlug": "will-launch-happen-by-friday",
            "outcome": "Yes",
        }
        record_low_buy_trade(
            self.web.conn,
            trade1,
            ws_price=0.15,
            ws_timestamp_ms=now_ts * 1000,
            market_volume_24h=100000,
            source="realtime",
            trigger_type="low_price_entry",
            trigger_reason="low-price test",
        )

        if include_second_wallet:
            trade2 = {
                "transactionHash": tx2,
                "proxyWallet": wallet2,
                "conditionId": condition2,
                "asset": "asset-high-winrate",
                "side": "BUY",
                "price": 0.55,
                "size": 10000,
                "timestamp": now_ts,
                "title": "Will event X resolve by date?",
                "slug": "will-event-x-resolve-by-date",
                "eventSlug": "will-event-x-resolve-by-date",
                "outcome": "Yes",
            }
            record_low_buy_trade(
                self.web.conn,
                trade2,
                ws_price=0.55,
                ws_timestamp_ms=now_ts * 1000,
                market_volume_24h=120000,
                source="realtime",
                trigger_type="impact_anomaly",
                trigger_reason="anomaly test",
            )

        upsert_wallet_profile(
            self.web.conn,
            wallet=wallet1,
            username="alpha_whale",
            pseudonym="",
            profile_image="https://example.com/a.png",
            profile_image_optimized="",
            bio="",
            first_activity_ts=now_ts - 86400,
            last_profile_sync_ts=now_ts,
        )
        if include_second_wallet:
            upsert_wallet_profile(
                self.web.conn,
                wallet=wallet2,
                username="beta_tracker",
                pseudonym="",
                profile_image="https://example.com/b.png",
                profile_image_optimized="",
                bio="",
                first_activity_ts=now_ts - 86400 * 30,
                last_profile_sync_ts=now_ts,
            )

        upsert_user_analysis(
            self.web.conn,
            wallet=wallet1,
            analyzed_ts=now_ts,
            position_count=3,
            pnl=5000,
            max_position_value=7000,
            top_open_position_title="Launch market",
            top_open_position_value=7000,
            total_open_value=12000,
            trade_count=60,
            low_buy_hits_14d=5,
            avg_low_buy_notional_14d=4000,
            market_diversity_14d=2,
            concentration_ratio=0.58,
            suspicion_score=88,
            insider_score=88,
            speculator_score=20,
            classification="insider_candidate",
            tier="high",
            strategy_group="single_thesis",
            strategy_confidence=80,
            strategy_note="",
            wins=16,
            losses=4,
            closed_sample_size=20,
            win_rate=0.8,
            high_winrate_qualified=True,
            first_activity_ts=now_ts - 86400,
            account_age_days=1,
            is_fresh_account=True,
            position_signal_count=1,
            position_insider_candidate_count=1,
            position_speculator_count=0,
            max_position_insider_score=78,
            max_position_speculator_score=24,
            matches_criteria=True,
            reason="seed insider candidate",
        )
        upsert_position_signal(
            self.web.conn,
            wallet=wallet1,
            condition_id=condition1,
            outcome="yes",
            title="Will launch happen by Friday?",
            event_slug="will-launch-happen-by-friday",
            is_open=True,
            position_value=7200,
            position_share=0.6,
            position_count_wallet=3,
            entry_anchor_ts=now_ts - 900,
            latest_trade_ts=now_ts - 120,
            buys_5m=2,
            buys_1h=4,
            buys_24h=6,
            notional_5m=1200,
            notional_1h=2600,
            notional_24h=4300,
            unique_sizes_5m=2,
            max_trades_60s=3,
            position_insider_score=78,
            position_speculator_score=24,
            position_classification="position_insider_candidate",
            signal_reason="seed signal wallet1",
            analyzed_ts=now_ts,
        )

        if include_second_wallet:
            upsert_user_analysis(
                self.web.conn,
                wallet=wallet2,
                analyzed_ts=now_ts,
                position_count=7,
                pnl=32000,
                max_position_value=3500,
                top_open_position_title="Event X",
                top_open_position_value=3500,
                total_open_value=8200,
                trade_count=120,
                low_buy_hits_14d=2,
                avg_low_buy_notional_14d=3000,
                market_diversity_14d=8,
                concentration_ratio=0.33,
                suspicion_score=61,
                insider_score=61,
                speculator_score=35,
                classification="high_winrate_review",
                tier="watch",
                strategy_group="diversified_scanner",
                strategy_confidence=66,
                strategy_note="",
                wins=21,
                losses=7,
                closed_sample_size=28,
                win_rate=0.75,
                high_winrate_qualified=True,
                first_activity_ts=now_ts - 86400 * 30,
                account_age_days=30,
                is_fresh_account=True,
                position_signal_count=1,
                position_insider_candidate_count=0,
                position_speculator_count=1,
                max_position_insider_score=42,
                max_position_speculator_score=72,
                total_closed_count=28,
                qualifying_roi=25.0,
                qualifying_pnl=8500.0,
                qualifying_capital=34000.0,
                matches_criteria=False,
                reason="seed high winrate",
            )
            upsert_position_signal(
                self.web.conn,
                wallet=wallet2,
                condition_id=condition2,
                outcome="yes",
                title="Will event X resolve by date?",
                event_slug="will-event-x-resolve-by-date",
                is_open=True,
                position_value=3400,
                position_share=0.42,
                position_count_wallet=7,
                entry_anchor_ts=now_ts - 1200,
                entry_price=0.42,
                current_price=0.45,
                latest_trade_ts=now_ts - 90,
                buys_5m=8,
                buys_1h=14,
                buys_24h=20,
                notional_5m=2900,
                notional_1h=5200,
                notional_24h=7100,
                unique_sizes_5m=6,
                max_trades_60s=9,
                position_insider_score=42,
                position_speculator_score=72,
                position_classification="position_speculator_ladder",
                signal_reason="seed signal wallet2",
                analyzed_ts=now_ts,
            )

        self.web.conn.commit()

        return wallet1, wallet2, tx1, condition1

    def test_flagged_and_high_winrate_endpoints_include_new_fields(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)

        flagged = self.client.get("/api/flagged-users").get_json()
        self.assertEqual(flagged["count"], 1)
        item = flagged["items"][0]
        self.assertEqual(item["wallet"], wallet1)
        for key in [
            "username",
            "profile_image",
            "insider_score",
            "speculator_score",
            "classification",
            "win_rate",
            "closed_sample_size",
            "account_age_days",
            "is_fresh_account",
            "position_signal_count",
            "position_insider_candidate_count",
            "position_speculator_count",
            "max_position_insider_score",
            "max_position_speculator_score",
            "eligibility_blockers",
        ]:
            self.assertIn(key, item)

        high = self.client.get("/api/high-winrate-wallets").get_json()
        wallets = {row["wallet"] for row in high["items"]}
        self.assertIn(wallet2, wallets)
        self.assertNotIn(wallet1, wallets)
        self.assertIn("eligibility_blockers", high["items"][0])

    def test_wallet_leaderboard_endpoint_window_sort_and_fields(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)
        payload = self.client.get(
            "/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200"
        ).get_json()
        self.assertEqual(payload["window"], "30d")
        self.assertEqual(payload["sort"], "qualifying_roi")
        self.assertEqual(payload["metrics_source"], "user_analysis_phase1")
        self.assertEqual(payload["window_mode"], "activity_filter_only")
        wallets = {row["wallet"] for row in payload["items"]}
        self.assertIn(wallet2, wallets)
        self.assertNotIn(wallet1, wallets)
        item = payload["items"][0]
        for key in [
            "raw_trade_count",
            "qualifying_trade_count",
            "dollars_per_trade",
            "trades_per_position",
            "raw_trade_count_window",
            "last_trade_ts_window",
            "last_trade_at_window",
        ]:
            self.assertIn(key, item)

    def test_wallet_leaderboard_endpoint_validates_window_and_sort(self) -> None:
        self._seed(include_second_wallet=True)
        bad_window = self.client.get("/api/wallet-leaderboard?window=7d")
        self.assertEqual(bad_window.status_code, 400)
        bad_sort = self.client.get("/api/wallet-leaderboard?sort=unknown_metric")
        self.assertEqual(bad_sort.status_code, 400)

    def test_wallet_leaderboard_quality_only_filters_low_quality_classes(self) -> None:
        _, wallet2, _, _ = self._seed(include_second_wallet=True)
        now_ts = int(time.time())
        wallet3 = "0x3333333333333333333333333333333333333333"
        tx3 = "0xccccdddd111122223333444455556666777788889999aaaabbbbccccddddeeee"
        record_low_buy_trade(
            self.web.conn,
            {
                "transactionHash": tx3,
                "proxyWallet": wallet3,
                "conditionId": "cond-low-quality",
                "asset": "asset-low-quality",
                "side": "BUY",
                "price": 0.25,
                "size": 9000,
                "timestamp": now_ts,
                "title": "Low quality wallet trade",
                "slug": "low-quality-wallet-trade",
                "eventSlug": "low-quality-wallet-trade",
                "outcome": "Yes",
            },
            ws_price=0.25,
            ws_timestamp_ms=now_ts * 1000,
            market_volume_24h=95000,
            source="realtime",
            trigger_type="low_price_entry",
            trigger_reason="seed low quality",
        )
        upsert_user_analysis(
            self.web.conn,
            wallet=wallet3,
            analyzed_ts=now_ts,
            position_count=4,
            pnl=15000,
            max_position_value=5000,
            top_open_position_title="Low quality market",
            top_open_position_value=5000,
            total_open_value=9000,
            trade_count=40,
            low_buy_hits_14d=3,
            avg_low_buy_notional_14d=1800,
            market_diversity_14d=5,
            concentration_ratio=0.2,
            suspicion_score=35,
            insider_score=20,
            speculator_score=88,
            classification="speculator_bot",
            tier="watch",
            strategy_group="scalper",
            strategy_confidence=70,
            strategy_note="",
            wins=8,
            losses=2,
            closed_sample_size=10,
            total_closed_count=10,
            win_rate=0.8,
            high_winrate_qualified=True,
            first_activity_ts=now_ts - 86400 * 10,
            account_age_days=10,
            is_fresh_account=True,
            qualifying_roi=40.0,
            qualifying_pnl=1600.0,
            qualifying_capital=4000.0,
            matches_criteria=False,
            reason="seed speculator bot",
        )
        self.web.conn.commit()

        default_payload = self.client.get("/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200").get_json()
        all_payload = self.client.get("/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200&quality_only=0").get_json()
        self.assertTrue(default_payload["quality_only"])
        self.assertFalse(all_payload["quality_only"])

        default_wallets = {row["wallet"] for row in default_payload["items"]}
        all_wallets = {row["wallet"] for row in all_payload["items"]}
        self.assertIn(wallet2, default_wallets)
        self.assertNotIn(wallet3, default_wallets)
        self.assertIn(wallet3, all_wallets)

    def test_wallet_leaderboard_uses_phase2_metrics_when_market_outcomes_exist(self) -> None:
        _, wallet2, _, _ = self._seed(include_second_wallet=True)
        condition2 = "cond-high-winrate"
        now_ts = int(time.time())
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "yes",
            1.0,
            now_ts,
            source="test_phase2",
        )
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "no",
            0.0,
            now_ts,
            source="test_phase2",
        )
        self.web.conn.commit()

        payload = self.client.get(
            "/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200&quality_only=0"
        ).get_json()
        self.assertEqual(payload["metrics_source"], "market_outcomes_phase2")
        self.assertEqual(payload["window_mode"], "resolved_entry_window")
        rows = {row["wallet"]: row for row in payload["items"]}
        self.assertIn(wallet2, rows)
        row = rows[wallet2]
        self.assertGreaterEqual(int(row.get("qualifying_trade_count") or 0), 1)
        self.assertIsNotNone(row.get("qualifying_roi"))

    def test_wallet_leaderboard_falls_back_when_market_outcomes_have_no_winner(self) -> None:
        _, _, _, condition2 = self._seed(include_second_wallet=True)
        now_ts = int(time.time())
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "yes",
            0.0,
            now_ts,
            source="test_invalid_no_winner",
        )
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "no",
            0.0,
            now_ts,
            source="test_invalid_no_winner",
        )
        self.web.conn.commit()

        payload = self.client.get(
            "/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200&quality_only=0"
        ).get_json()
        self.assertEqual(payload["metrics_source"], "user_analysis_phase1")

    def test_wallet_leaderboard_falls_back_when_market_outcomes_resolve_in_future(self) -> None:
        _, _, _, condition2 = self._seed(include_second_wallet=True)
        future_ts = int(time.time()) + 24 * 60 * 60
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "yes",
            1.0,
            future_ts,
            source="test_invalid_future_resolve",
        )
        upsert_market_outcome(
            self.web.conn,
            condition2,
            "no",
            0.0,
            future_ts,
            source="test_invalid_future_resolve",
        )
        self.web.conn.commit()

        payload = self.client.get(
            "/api/wallet-leaderboard?window=30d&sort=qualifying_roi&limit=200&quality_only=0"
        ).get_json()
        self.assertEqual(payload["metrics_source"], "user_analysis_phase1")

    def test_open_position_signals_endpoint(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)
        payload = self.client.get("/api/open-position-signals").get_json()
        self.assertEqual(payload["count"], 2)
        wallets = {row["wallet"] for row in payload["items"]}
        self.assertIn(wallet1, wallets)
        self.assertIn(wallet2, wallets)

        first = payload["items"][0]
        for key in [
            "username",
            "profile_image",
            "profile_prediction_count",
            "profile_joined_ts",
            "win_rate_source",
            "position_classification",
            "position_insider_score",
            "position_speculator_score",
            "position_value",
            "position_share",
            "buys_1h",
            "notional_1h",
            "win_rate",
            "account_age_days",
            "classification",
            "insider_score",
            "speculator_score",
        ]:
            self.assertIn(key, first)

    def test_open_position_signals_group_by_wallet_dedupes_positions(self) -> None:
        wallet1, _, _, _ = self._seed(include_second_wallet=False)
        now_ts = int(time.time())
        upsert_position_signal(
            self.web.conn,
            wallet=wallet1,
            condition_id="cond-main-2",
            outcome="no",
            title="Secondary market",
            event_slug="secondary-market",
            is_open=True,
            position_value=4100,
            position_share=0.22,
            position_count_wallet=4,
            entry_anchor_ts=now_ts - 600,
            latest_trade_ts=now_ts - 120,
            buys_5m=0,
            buys_1h=2,
            buys_24h=3,
            trades_5m=0,
            trades_1h=2,
            trades_24h=3,
            sells_5m=0,
            sells_1h=0,
            sells_24h=0,
            notional_5m=0,
            notional_1h=700,
            notional_24h=1400,
            sell_notional_5m=0,
            sell_notional_1h=0,
            sell_notional_24h=0,
            buys_since_entry=2,
            sells_since_entry=0,
            buy_notional_since_entry=1400,
            sell_notional_since_entry=0,
            net_notional_24h=1400,
            net_notional_since_entry=1400,
            last_trade_side="BUY",
            activity_state="holding",
            hold_hours=12,
            unique_sizes_5m=0,
            max_trades_60s=1,
            position_insider_score=40,
            position_speculator_score=30,
            position_classification="position_low_signal",
            signal_reason="seed duplicate wallet signal",
            analyzed_ts=now_ts,
        )
        self.web.conn.commit()

        raw_payload = self.client.get("/api/open-position-signals?focused=0").get_json()
        grouped_payload = self.client.get("/api/open-position-signals?focused=0&group_by_wallet=1").get_json()

        self.assertGreaterEqual(int(raw_payload["count"]), 2)
        self.assertEqual(grouped_payload["group_by_wallet"], True)
        self.assertGreater(int(grouped_payload["raw_count"]), int(grouped_payload["count"]))
        self.assertEqual(grouped_payload["count"], 1)
        item = grouped_payload["items"][0]
        self.assertGreaterEqual(int(item.get("position_group_count") or 0), 2)

    def test_open_position_signals_uses_phase2_win_rate_when_available(self) -> None:
        wallet1, _, _, condition1 = self._seed(include_second_wallet=False)
        now_ts = int(time.time())
        upsert_market_outcome(
            self.web.conn,
            condition1,
            "yes",
            1.0,
            now_ts,
            source="test_phase2_signals",
        )
        upsert_market_outcome(
            self.web.conn,
            condition1,
            "no",
            0.0,
            now_ts,
            source="test_phase2_signals",
        )
        self.web.conn.commit()

        payload = self.client.get("/api/open-position-signals?focused=0").get_json()
        rows = {row["wallet"]: row for row in payload["items"]}
        self.assertIn(wallet1, rows)
        row = rows[wallet1]
        self.assertEqual(row.get("win_rate_source"), "market_outcomes_phase2")
        self.assertGreaterEqual(int(row.get("wins") or 0), 1)
        self.assertEqual(int(row.get("losses") or 0), 0)
        self.assertGreaterEqual(int(row.get("closed_sample_size") or 0), 1)

    def test_open_position_signals_ignores_invalid_market_outcomes(self) -> None:
        wallet1, _, _, condition1 = self._seed(include_second_wallet=False)
        now_ts = int(time.time())
        upsert_market_outcome(
            self.web.conn,
            condition1,
            "yes",
            0.0,
            now_ts,
            source="test_invalid_no_winner_signals",
        )
        upsert_market_outcome(
            self.web.conn,
            condition1,
            "no",
            0.0,
            now_ts,
            source="test_invalid_no_winner_signals",
        )
        self.web.conn.commit()

        payload = self.client.get("/api/open-position-signals?focused=0").get_json()
        rows = {row["wallet"]: row for row in payload["items"]}
        self.assertIn(wallet1, rows)
        row = rows[wallet1]
        self.assertEqual(row.get("win_rate_source"), "user_analysis")

    def test_open_position_signals_excludes_resolved_or_closed_markets(self) -> None:
        wallet1, _, _, _ = self._seed(include_second_wallet=False)
        now_ts = int(time.time())

        # Signal with explicit past end_date should never be shown in open feeds.
        past_end = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        upsert_position_signal(
            self.web.conn,
            wallet=wallet1,
            condition_id="cond-resolved-past",
            outcome="yes",
            title="Resolved past market",
            event_slug="resolved-past-market",
            is_open=True,
            position_value=12000,
            position_share=1.0,
            position_count_wallet=1,
            entry_anchor_ts=now_ts - 3600,
            latest_trade_ts=now_ts - 300,
            position_insider_score=92,
            position_speculator_score=8,
            position_classification="position_insider_candidate",
            signal_reason="seed resolved signal",
            analyzed_ts=now_ts,
            end_date_iso=past_end,
        )

        # Signal with market_metadata marked closed should also be excluded.
        upsert_market_metadata(
            self.web.conn,
            "cond-market-closed",
            title="Closed via metadata",
            slug="closed-via-metadata",
            event_slug="closed-via-metadata",
            end_date_iso=(dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            category="politics",
            category_source="test",
            volume_24h=1000.0,
            is_open=0,
        )
        upsert_position_signal(
            self.web.conn,
            wallet=wallet1,
            condition_id="cond-market-closed",
            outcome="yes",
            title="Closed metadata market",
            event_slug="closed-via-metadata",
            is_open=True,
            position_value=12500,
            position_share=1.0,
            position_count_wallet=1,
            entry_anchor_ts=now_ts - 1800,
            latest_trade_ts=now_ts - 120,
            position_insider_score=91,
            position_speculator_score=9,
            position_classification="position_insider_candidate",
            signal_reason="seed closed metadata signal",
            analyzed_ts=now_ts,
        )
        self.web.conn.commit()

        payload = self.client.get("/api/open-position-signals").get_json()
        condition_ids = {str(item.get("condition_id") or "") for item in payload["items"]}
        self.assertNotIn("cond-resolved-past", condition_ids)
        self.assertNotIn("cond-market-closed", condition_ids)

    def test_thesis_candidates_endpoint_returns_relaxed_size_near_miss(self) -> None:
        now_ts = int(time.time())
        wallet = "0x4444444444444444444444444444444444444444"
        condition = "cond-thesis-candidate"
        end_date_iso = (
            dt.datetime.now(dt.UTC) + dt.timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        upsert_wallet_profile(
            self.web.conn,
            wallet=wallet,
            username="thesis_candidate_wallet",
            pseudonym="",
            profile_image="",
            profile_image_optimized="",
            bio="",
            first_activity_ts=now_ts - 86400 * 5,
            last_profile_sync_ts=now_ts,
        )
        upsert_user_analysis(
            self.web.conn,
            wallet=wallet,
            analyzed_ts=now_ts,
            position_count=1,
            pnl=1200,
            max_position_value=4800,
            top_open_position_title="Candidate market",
            top_open_position_value=4800,
            total_open_value=4800,
            trade_count=2,
            low_buy_hits_14d=1,
            avg_low_buy_notional_14d=2400,
            market_diversity_14d=1,
            concentration_ratio=1.0,
            suspicion_score=82,
            insider_score=82,
            speculator_score=14,
            classification="insider_candidate",
            tier="watch",
            strategy_group="single_thesis",
            strategy_confidence=70,
            strategy_note="",
            wins=1,
            losses=0,
            closed_sample_size=1,
            win_rate=1.0,
            high_winrate_qualified=False,
            first_activity_ts=now_ts - 86400 * 5,
            account_age_days=5.0,
            is_fresh_account=True,
            matches_criteria=True,
            reason="seed thesis candidate",
        )
        upsert_market_metadata(
            self.web.conn,
            condition,
            title="Will candidate event resolve?",
            slug="will-candidate-event-resolve",
            event_slug="will-candidate-event-resolve",
            end_date_iso=end_date_iso,
            category="politics",
            category_source="test",
            volume_24h=120000.0,
            is_open=1,
        )
        upsert_position_signal(
            self.web.conn,
            wallet=wallet,
            condition_id=condition,
            outcome="yes",
            title="Will candidate event resolve?",
            event_slug="will-candidate-event-resolve",
            is_open=True,
            position_value=4800,
            position_share=1.0,
            position_count_wallet=1,
            entry_anchor_ts=now_ts - 1800,
            entry_price=0.21,
            entry_size=22857,
            entry_notional=4800,
            current_price=0.23,
            current_size=22857,
            latest_trade_ts=now_ts - 60,
            buys_1h=1,
            buys_24h=1,
            trades_1h=1,
            trades_24h=1,
            notional_1h=4800,
            notional_24h=4800,
            position_insider_score=82,
            position_speculator_score=14,
            position_classification="position_insider_candidate",
            signal_reason="seed thesis candidate signal",
            analyzed_ts=now_ts,
            end_date_iso=end_date_iso,
        )
        self.web.conn.commit()

        strict = self.client.get("/api/thesis-signals?limit=50").get_json()
        self.assertEqual(strict["count"], 0)

        payload = self.client.get("/api/thesis-candidates?limit=50").get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["wallet"], wallet)
        self.assertEqual(item["blocking_gate"], "position_value")
        self.assertIn("<", item["blocking_detail"])
        self.assertFalse(bool(item["high_conviction"]))
        self.assertLess(float(item["position_value"]), float(payload["strict_min_position_value"]))

    def test_followed_wallet_compat_endpoints_exist(self) -> None:
        wallet1, _, _, _ = self._seed(include_second_wallet=False)
        add_resp = self.client.post(
            "/api/followed-wallets",
            json={"wallet": wallet1, "label": "seed followed"},
        )
        self.assertEqual(add_resp.status_code, 200)
        registry_row = self.web.conn.execute(
            "SELECT display_label, copy_status FROM wallet_registry WHERE wallet = ?",
            (wallet1,),
        ).fetchone()
        membership_row = self.web.conn.execute(
            "SELECT status FROM wallet_memberships WHERE wallet = ? AND scope = 'watchlist'",
            (wallet1,),
        ).fetchone()
        self.assertEqual(registry_row["display_label"], "seed followed")
        self.assertEqual(registry_row["copy_status"], "not_followed")
        self.assertEqual(membership_row["status"], "active")

        list_resp = self.client.get("/api/followed-wallets")
        self.assertEqual(list_resp.status_code, 200)
        listed_wallets = {row["wallet"] for row in list_resp.get_json()["items"]}
        self.assertIn(wallet1, listed_wallets)

        portfolio_a = self.client.get(f"/api/followed-wallets/{wallet1}/portfolio")
        portfolio_b = self.client.get(f"/api/wallet-portfolio/{wallet1}")
        self.assertEqual(portfolio_a.status_code, 200)
        self.assertEqual(portfolio_b.status_code, 200)
        self.assertEqual(portfolio_a.get_json()["wallet"], wallet1)
        self.assertEqual(portfolio_b.get_json()["wallet"], wallet1)
        self.assertEqual(portfolio_a.get_json()["count"], portfolio_b.get_json()["count"])

    def test_copy_pipeline_endpoints_are_explicit_and_do_not_shadow_followed_wallets(self) -> None:
        wallet = "0x6666666666666666666666666666666666666666"
        add_resp = self.client.post(
            "/api/copy-pipeline/wallets",
            json={"wallet": wallet, "label": "Copy Wallet", "reason": "manual copy"},
        )
        self.assertEqual(add_resp.status_code, 200)

        registry_row = self.web.conn.execute(
            "SELECT display_label, copy_status FROM wallet_registry WHERE wallet = ?",
            (wallet,),
        ).fetchone()
        membership_row = self.web.conn.execute(
            """
            SELECT status, reason
            FROM wallet_memberships
            WHERE wallet = ? AND scope = 'copy_execution'
            """,
            (wallet,),
        ).fetchone()
        self.assertEqual(registry_row["display_label"], "Copy Wallet")
        self.assertEqual(registry_row["copy_status"], "active")
        self.assertEqual(membership_row["status"], "active")
        self.assertEqual(membership_row["reason"], "manual copy")

        list_resp = self.client.get("/api/copy-pipeline/wallets")
        self.assertEqual(list_resp.status_code, 200)
        payload = list_resp.get_json()
        self.assertEqual(payload["status"], "active")
        item = next(row for row in payload["items"] if row["wallet"] == wallet)
        self.assertEqual(item["label"], "Copy Wallet")
        self.assertEqual(item["reason"], "manual copy")
        self.assertEqual(item["status"], "active")

        remove_resp = self.client.delete(f"/api/copy-pipeline/wallets/{wallet}")
        self.assertEqual(remove_resp.status_code, 200)
        removed_row = self.web.conn.execute(
            """
            SELECT status
            FROM wallet_memberships
            WHERE wallet = ? AND scope = 'copy_execution'
            """,
            (wallet,),
        ).fetchone()
        self.assertEqual(removed_row["status"], "unfollowed")

        inactive_list = self.client.get("/api/copy-pipeline/wallets?status=unfollowed")
        self.assertEqual(inactive_list.status_code, 200)
        inactive_item = next(row for row in inactive_list.get_json()["items"] if row["wallet"] == wallet)
        self.assertEqual(inactive_item["status"], "unfollowed")

    def test_strategies_blueprint_no_longer_claims_followed_wallet_compat_routes(self) -> None:
        followed_strategy_routes = []
        for rule in self.web.app.url_map.iter_rules():
            methods = {m for m in rule.methods if m not in {"HEAD", "OPTIONS"}}
            if "followed-wallets" in rule.rule and rule.endpoint.startswith("api_strategies."):
                followed_strategy_routes.append((rule.rule, tuple(sorted(methods))))
        self.assertEqual(followed_strategy_routes, [])

    def test_followed_wallets_list_includes_profile_identity_fields(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        now_ts = int(time.time())
        upsert_wallet_profile(
            self.web.conn,
            wallet=wallet,
            username="alpha_whale",
            pseudonym="Alpha",
            profile_image="https://example.com/alpha.png",
            profile_image_optimized="",
            bio="",
            first_activity_ts=now_ts - 1000,
            last_profile_sync_ts=now_ts,
            profile_joined_ts=now_ts - 5000,
            profile_prediction_count=12,
            profile_enriched_ts=now_ts,
            profile_enrichment_source="test",
        )
        self.web.conn.execute(
            """
            INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts)
            VALUES (?, ?, ?, ?)
            """,
            (wallet, "Manual Label", now_ts, now_ts),
        )
        self.web.conn.commit()

        payload = self.client.get("/api/followed-wallets").get_json()
        item = next(row for row in payload["items"] if row["wallet"] == wallet)
        self.assertEqual(item["username"], "alpha_whale")
        self.assertEqual(item["pseudonym"], "Alpha")
        self.assertEqual(item["profile_image"], "https://example.com/alpha.png")
        self.assertEqual(item["display_name"], "alpha_whale")

    def test_followed_positions_defaults_to_manual_only(self) -> None:
        manual_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        auto_wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        now_ts = int(time.time())

        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manual_wallet,
                "cond-manual",
                "yes",
                "Manual market",
                "manual-market",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.42,
                0.45,
                1200.0,
                1285.0,
                2857.0,
                85.0,
                0.0,
            ),
        )
        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                auto_wallet,
                "cond-auto",
                "no",
                "Auto market",
                "auto-market",
                "Auto",
                "auto_relevant",
                now_ts,
                now_ts,
                0.33,
                0.35,
                1000.0,
                1060.0,
                3030.0,
                60.0,
                0.0,
            ),
        )
        self.web.conn.commit()

        manual_only = self.client.get("/api/followed-positions").get_json()
        include_auto = self.client.get("/api/followed-positions?include_auto=1").get_json()

        self.assertEqual(manual_only["count"], 1)
        self.assertEqual(include_auto["count"], 2)
        wallets_manual = {row["wallet"] for row in manual_only["items"]}
        wallets_all = {row["wallet"] for row in include_auto["items"]}
        self.assertIn(manual_wallet, wallets_manual)
        self.assertNotIn(auto_wallet, wallets_manual)
        self.assertIn(manual_wallet, wallets_all)
        self.assertIn(auto_wallet, wallets_all)

    def test_real_trades_endpoint_returns_open_and_closed_groups(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )

        # Open group (net shares > 0)
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xopenbuy1", "cond-open", "Open market", "open-market", "YES", "BUY", 0.40, 100.0, 40.0, now_ts - 90),
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xopenbuy2", "cond-open", "Open market", "open-market", "YES", "BUY", 0.50, 50.0, 25.0, now_ts - 60),
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xopensell1", "cond-open", "Open market", "open-market", "YES", "SELL", 0.45, 30.0, 13.5, now_ts - 30),
        )

        # Closed group (net shares == 0)
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xclosedbuy1", "cond-closed", "Closed market", "closed-market", "NO", "BUY", 0.30, 100.0, 30.0, now_ts - 40),
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xclosedsell1", "cond-closed", "Closed market", "closed-market", "NO", "SELL", 0.60, 100.0, 60.0, now_ts - 10),
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        self.assertEqual(payload["wallet"], wallet)
        self.assertEqual(payload["summary"]["open_count"], 1)
        self.assertEqual(payload["summary"]["closed_count"], 1)
        self.assertEqual(len(payload["open"]), 1)
        self.assertEqual(len(payload["closed"]), 1)

        open_row = payload["open"][0]
        self.assertEqual(open_row["condition_id"], "cond-open")
        self.assertAlmostEqual(float(open_row["spent"]), 51.5, places=4)
        self.assertEqual(int(open_row["trades"]), 3)

        closed_row = payload["closed"][0]
        self.assertEqual(closed_row["condition_id"], "cond-closed")
        self.assertAlmostEqual(float(closed_row["pnl"]), 30.0, places=4)

    def test_real_trades_endpoint_adds_resolution_display_fields(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )

        # Open position (net shares > 0) that is resolved by market_outcomes
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xresbuy", "cond-resolved-open", "Resolved Open", "resolved-open", "YES", "BUY", 0.40, 100.0, 40.0, now_ts - 60),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-resolved-open",
            "yes",
            resolved_price=1.0,
            resolved_at=now_ts - 10,
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        # Resolved positions are moved from open → resolved bucket
        open_row = next((row for row in payload["open"] if row["condition_id"] == "cond-resolved-open"), None)
        self.assertIsNone(open_row, "Resolved position should not remain in open list")
        resolved_row = next((row for row in payload["resolved"] if row["condition_id"] == "cond-resolved-open"), None)
        self.assertIsNotNone(resolved_row, "Resolved position should appear in resolved list")
        self.assertTrue(bool(resolved_row["is_resolved"]))
        self.assertEqual(resolved_row["display_status"], "resolved")
        self.assertEqual(resolved_row["status"], "resolved")
        self.assertEqual(resolved_row["price_source"], "market_outcomes")
        self.assertAlmostEqual(float(resolved_row["settlement_price"]), 1.0, places=6)
        # claim_value = settlement_price * size_shares = 1.0 * 100 = 100.0
        self.assertAlmostEqual(float(resolved_row["claim_value"]), 100.0, places=2)
        # claim_pnl = claim_value - spent = 100.0 - 40.0 = 60.0
        self.assertAlmostEqual(float(resolved_row["claim_pnl"]), 60.0, places=2)

    def test_real_trades_resolves_named_outcome_market_via_fallback_yes(self) -> None:
        """YES trade resolves to 1.0 when named winner exists in market_outcomes."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xsportbuy", "cond-sports-yes", "Rockets vs Hornets", "rockets-hornets", "YES", "BUY", 0.55, 100.0, 55.0, now_ts - 60),
        )
        upsert_market_outcome(self.web.conn, "cond-sports-yes", "rockets", resolved_price=1.0, resolved_at=now_ts - 10)
        upsert_market_outcome(self.web.conn, "cond-sports-yes", "hornets", resolved_price=0.0, resolved_at=now_ts - 10)
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        open_row = next((row for row in payload["open"] if row["condition_id"] == "cond-sports-yes"), None)
        self.assertIsNone(open_row, "Named-outcome resolved YES position should not remain in open list")
        resolved_row = next((row for row in payload["resolved"] if row["condition_id"] == "cond-sports-yes"), None)
        self.assertIsNotNone(resolved_row, "Named-outcome resolved YES position should appear in resolved list")
        self.assertTrue(bool(resolved_row["is_resolved"]))
        self.assertEqual(resolved_row["price_source"], "market_outcomes_fallback")
        # YES token: primary named outcome won → YES resolves to 1.0
        self.assertAlmostEqual(float(resolved_row["settlement_price"]), 1.0, places=6)

    def test_real_trades_resolves_named_outcome_market_via_fallback_no(self) -> None:
        """NO trade resolves to 0.0 when named winner exists — matches live Rockets/Hornets data shape."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        # Matches actual live data: user bought NO at 0.44, 13.79 shares
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xsportno", "cond-sports-no", "Rockets vs Hornets", "rockets-hornets", "NO", "BUY", 0.44, 13.79, 6.07, now_ts - 60),
        )
        # Rockets won (1.0), Hornets lost (0.0) — same as live market_outcomes
        upsert_market_outcome(self.web.conn, "cond-sports-no", "rockets", resolved_price=1.0, resolved_at=now_ts - 10)
        upsert_market_outcome(self.web.conn, "cond-sports-no", "hornets", resolved_price=0.0, resolved_at=now_ts - 10)
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        open_row = next((row for row in payload["open"] if row["condition_id"] == "cond-sports-no"), None)
        self.assertIsNone(open_row, "Named-outcome resolved NO position should not remain in open list")
        resolved_row = next((row for row in payload["resolved"] if row["condition_id"] == "cond-sports-no"), None)
        self.assertIsNotNone(resolved_row, "Named-outcome resolved NO position should appear in resolved list")
        self.assertTrue(bool(resolved_row["is_resolved"]))
        self.assertEqual(resolved_row["price_source"], "market_outcomes_fallback")
        # NO token: primary named outcome won → NO resolves to 0.0 (user lost)
        self.assertAlmostEqual(float(resolved_row["settlement_price"]), 0.0, places=6)

    def test_real_trades_uses_followed_positions_price_when_summary_lacks_current_price(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute("DROP TABLE IF EXISTS real_trades")
        self.web.conn.execute(
            """
            CREATE TABLE real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xinject", "cond-inject", "Inject market", "inject-market", "YES", "BUY", 0.40, 100.0, 40.0, now_ts - 60),
        )
        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                "cond-inject",
                "yes",
                "Inject market",
                "inject-market",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.40,
                0.89,
                40.0,
                89.0,
                100.0,
                49.0,
                0.0,
            ),
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        row = next((r for r in payload["open"] if r["condition_id"] == "cond-inject"), None)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row["current_price"]), 0.89, places=6)
        self.assertEqual(row["price_source"], "followed_positions")

    def test_real_trades_prefers_clob_price_over_unrelated_position_signal_price(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        condition_id = "0xfeedbeef00000000000000000000000000000000000000000000000000000001"
        now_ts = int(time.time())
        self.web.conn.execute("DROP TABLE IF EXISTS real_trades")
        self.web.conn.execute(
            """
            CREATE TABLE real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xclobpref", condition_id, "Hormuz", "hormuz", "NO", "BUY", 0.185, 54.05, 10.0, now_ts - 60),
        )
        upsert_position_signal(
            self.web.conn,
            wallet="0x1234567890123456789012345678901234567890",
            condition_id=condition_id,
            outcome="NO",
            title="Hormuz",
            event_slug="hormuz",
            is_open=True,
            position_value=1000.0,
            position_share=0.1,
            position_count_wallet=1,
            entry_anchor_ts=now_ts - 3600,
            latest_trade_ts=now_ts - 60,
            buys_5m=0,
            buys_1h=0,
            buys_24h=1,
            trades_5m=0,
            trades_1h=0,
            trades_24h=1,
            sells_5m=0,
            sells_1h=0,
            sells_24h=0,
            notional_5m=0.0,
            notional_1h=0.0,
            notional_24h=1000.0,
            sell_notional_5m=0.0,
            sell_notional_1h=0.0,
            sell_notional_24h=0.0,
            buys_since_entry=1,
            sells_since_entry=0,
            buy_notional_since_entry=1000.0,
            sell_notional_since_entry=0.0,
            net_notional_24h=1000.0,
            net_notional_since_entry=1000.0,
            last_trade_side="BUY",
            activity_state="holding",
            hold_hours=1.0,
            unique_sizes_5m=1,
            max_trades_60s=1,
            entry_price=0.185,
            current_price=0.425,
            current_size=1000.0,
            position_classification="position_low_signal",
            position_insider_score=20.0,
            position_speculator_score=10.0,
            signal_reason="test",
            analyzed_ts=now_ts,
            analysis_version=1,
        )
        self.web.conn.commit()

        with patch("sniper.routes.api_followed._fetch_clob_market_price", return_value=0.0205):
            payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()

        row = next((r for r in payload["open"] if r["condition_id"] == condition_id), None)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row["current_price"]), 0.0205, places=6)
        self.assertEqual(row["price_source"], "clob")

    def test_create_paper_trade_endpoint_persists_signal_entry_and_slippage(self) -> None:
        self._ensure_paper_trades_table()
        wallet = "0x1111111111111111111111111111111111111111"
        payload = {
            "wallet": wallet,
            "condition_id": "cond-paper-create",
            "outcome": "YES",
            "entry_price": 0.40,
            "size_usdc": 20.0,
            "title": "Created via API",
            "signal_type": "manual_copy",
            "position_value": 1000.0,
            "signal_entry_price": 0.35,
        }
        resp = self.client.post("/api/paper-trades", json=payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        trade_id = int(body["trade_id"])

        row = self.web.conn.execute(
            """
            SELECT wallet, condition_id, outcome, signal_entry_price, slippage_pct, status
            FROM paper_trades
            WHERE trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["wallet"]), wallet)
        self.assertEqual(str(row["condition_id"]), "cond-paper-create")
        self.assertEqual(str(row["outcome"]), "yes")  # stored as lowercase per normalize_outcome_key
        self.assertAlmostEqual(float(row["signal_entry_price"]), 0.35, places=6)
        self.assertAlmostEqual(float(row["slippage_pct"]), round(((0.40 - 0.35) / 0.35) * 100, 2), places=6)
        self.assertEqual(str(row["status"]), "open")

    def test_paper_trade_idempotency_deduplicates(self) -> None:
        """Sending the same client_request_id returns deduplicated=True without creating a second trade."""
        self._ensure_paper_trades_table()
        wallet = "0x1111111111111111111111111111111111111111"
        payload = {
            "wallet": wallet,
            "condition_id": "cond-idem",
            "outcome": "YES",
            "entry_price": 0.40,
            "size_usdc": 20.0,
            "title": "Idempotent test",
            "signal_type": "manual_copy",
            "client_request_id": "test-uuid-123",
        }
        resp1 = self.client.post("/api/paper-trades", json=payload)
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.get_json()
        self.assertTrue(body1["ok"])
        self.assertNotIn("deduplicated", body1)
        trade_id = body1["trade_id"]

        # Same request again
        resp2 = self.client.post("/api/paper-trades", json=payload)
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.get_json()
        self.assertTrue(body2["ok"])
        self.assertTrue(body2["deduplicated"])
        self.assertEqual(body2["trade_id"], trade_id)

        # Verify only one row exists
        count = self.web.conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE client_request_id = 'test-uuid-123'"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_paper_trade_create_deduplicates_existing_open_trade(self) -> None:
        self._ensure_paper_trades_table()
        wallet = "0x1111111111111111111111111111111111111111"
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares,
                position_value_at_entry, signal_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                wallet,
                "cond-open-dup",
                "yes",
                "Existing Open",
                "YES",
                0.40,
                0.40,
                20.0,
                50.0,
                20.0,
                "manual_copy",
            ),
        )
        self.web.conn.commit()
        existing_trade_id = int(self.web.conn.execute(
            "SELECT trade_id FROM paper_trades WHERE wallet = ? AND condition_id = ?",
            (wallet, "cond-open-dup"),
        ).fetchone()["trade_id"])

        payload = {
            "wallet": wallet,
            "condition_id": "cond-open-dup",
            "outcome": "YES",
            "entry_price": 0.41,
            "size_usdc": 25.0,
            "title": "Should dedupe against open",
            "signal_type": "manual_copy",
        }
        resp = self.client.post("/api/paper-trades", json=payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["deduplicated"])
        self.assertEqual(int(body["trade_id"]), existing_trade_id)

        count = self.web.conn.execute(
            """
            SELECT COUNT(*) as c
            FROM paper_trades
            WHERE wallet = ? AND condition_id = ? AND UPPER(outcome) = 'YES' AND status = 'open'
            """,
            (wallet, "cond-open-dup"),
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_get_paper_trades_returns_slippage_fields(self) -> None:
        self._ensure_paper_trades_table()
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares,
                position_value_at_entry, signal_type, status,
                signal_entry_price, slippage_pct, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "0x1111111111111111111111111111111111111111",
                "cond-paper-get",
                "YES",
                "Paper Get",
                "YES",
                0.50,
                0.52,
                25.0,
                50.0,
                500.0,
                "manual_copy",
                "open",
                0.48,
                4.17,
                now_ts,
            ),
        )
        self.web.conn.commit()

        payload = self.client.get("/api/paper-trades").get_json()
        item = next((row for row in payload["items"] if row["condition_id"] == "cond-paper-get"), None)
        self.assertIsNotNone(item)
        self.assertIn("signal_entry_price", item)
        self.assertIn("slippage_pct", item)
        self.assertAlmostEqual(float(item["signal_entry_price"]), 0.48, places=6)
        self.assertAlmostEqual(float(item["slippage_pct"]), 4.17, places=2)

    def test_get_paper_trades_prefers_live_signal_price_for_open_rows(self) -> None:
        self._ensure_paper_trades_table()
        wallet = "0x1111111111111111111111111111111111111111"
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares,
                position_value_at_entry, signal_type, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                wallet,
                "cond-paper-live",
                "YES",
                "Paper Live Price",
                "YES",
                0.50,
                0.52,
                25.0,
                50.0,
                500.0,
                "manual_copy",
                "open",
            ),
        )
        self.web.conn.execute(
            """
            INSERT INTO position_signals (
                wallet, condition_id, outcome, title, is_open, current_price, latest_trade_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "cond-paper-live", "yes", "Paper Live Price", 1, 0.61, int(time.time())),
        )
        self.web.conn.commit()

        payload = self.client.get("/api/paper-trades").get_json()
        item = next((row for row in payload["items"] if row["condition_id"] == "cond-paper-live"), None)
        self.assertIsNotNone(item)
        self.assertAlmostEqual(float(item["current_price"]), 0.61, places=6)

    def test_manual_close_paper_trade_updates_closed_fields_consistently(self) -> None:
        self._ensure_paper_trades_table()
        wallet = "0x1111111111111111111111111111111111111111"
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares,
                position_value_at_entry, signal_type, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                wallet,
                "cond-paper-close",
                "YES",
                "Paper Manual Close",
                "YES",
                0.50,
                0.52,
                100.0,
                200.0,
                500.0,
                "manual_copy",
                "open",
            ),
        )
        trade_id = int(self.web.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.web.conn.execute(
            """
            INSERT INTO position_signals (
                wallet, condition_id, outcome, title, is_open, current_price, latest_trade_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "cond-paper-close", "yes", "Paper Manual Close", 1, 0.61, int(time.time())),
        )
        self.web.conn.commit()

        resp = self.client.post(f"/api/paper-trades/{trade_id}/close")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertAlmostEqual(float(body["exit_price"]), 0.61, places=6)

        row = self.web.conn.execute(
            """
            SELECT status, exit_classification, exit_reason, current_price, exit_price,
                   pnl_usdc, roi_percent, closed_at
            FROM paper_trades
            WHERE trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        self.assertEqual(str(row["status"]), "closed")
        self.assertEqual(str(row["exit_classification"]), "manual")
        self.assertEqual(str(row["exit_reason"]), "manual_close")
        self.assertAlmostEqual(float(row["current_price"]), 0.61, places=6)
        self.assertAlmostEqual(float(row["exit_price"]), 0.61, places=6)
        self.assertIsInstance(row["closed_at"], str)

    def test_paper_trade_summary_counts_manual_terminal_status(self) -> None:
        self._ensure_paper_trades_table()
        self.web.conn.execute(
            """
            INSERT INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, exit_price, size_usdc, shares,
                position_value_at_entry, signal_type, status, pnl_usdc, roi_percent,
                exit_classification, exit_reason, opened_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "0x1111111111111111111111111111111111111111",
                "cond-paper-manual-status",
                "YES",
                "Paper Manual Status",
                "YES",
                0.40,
                0.55,
                0.55,
                50.0,
                125.0,
                500.0,
                "manual_copy",
                "manual",
                18.75,
                37.5,
                "manual",
                "manual_close",
            ),
        )
        self.web.conn.commit()

        payload = self.client.get("/api/paper-trades").get_json()
        self.assertEqual(payload["closed_count"], 1)

    def test_followed_positions_enriches_resolution_and_unknown_price(self) -> None:
        manual_wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        now_ts = int(time.time())

        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manual_wallet,
                "cond-followed-resolved",
                "yes",
                "Resolved Followed",
                "resolved-followed",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.42,
                0.0,
                1200.0,
                1285.0,
                2857.0,
                85.0,
                0.0,
            ),
        )
        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manual_wallet,
                "cond-followed-unknown",
                "no",
                "Unknown Price Followed",
                "unknown-followed",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.33,
                0.0,
                900.0,
                900.0,
                2727.0,
                0.0,
                0.0,
            ),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-followed-resolved",
            "yes",
            resolved_price=1.0,
            resolved_at=now_ts - 5,
        )
        self.web.conn.commit()

        payload = self.client.get("/api/followed-positions").get_json()
        resolved = next((row for row in payload["items"] if row["condition_id"] == "cond-followed-resolved"), None)
        unknown = next((row for row in payload["items"] if row["condition_id"] == "cond-followed-unknown"), None)
        self.assertIsNotNone(resolved)
        self.assertTrue(bool(resolved["is_resolved"]))
        self.assertEqual(resolved["display_status"], "resolved")
        self.assertEqual(resolved["price_source"], "market_outcomes")
        self.assertAlmostEqual(float(resolved["current_price"]), 1.0, places=6)

        self.assertIsNotNone(unknown)
        self.assertFalse(bool(unknown["is_resolved"]))
        self.assertEqual(unknown["price_source"], "unknown")
        self.assertIsNone(unknown["current_price"])

    def test_real_positions_redeem_dry_run_logs_attempt(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xredeembuy", "cond-1", "Redeem test", "redeem-test", "YES", "BUY", 0.45, 100.0, 45.0, now_ts - 60),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-1",
            "yes",
            resolved_price=1.0,
            resolved_at=now_ts - 10,
        )
        self.web.conn.commit()

        with patch.dict(
            os.environ,
            {"REDEEM_DRY_RUN": "1", "REDEEM_ARMED": "0", "REDEEM_MIN_RESOLVED_PRICE": "0.97"},
            clear=False,
        ):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-1", "outcome": "YES"},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(bool(payload["ok"]))
        self.assertTrue(bool(payload["dry_run"]))
        self.assertEqual(payload["outcome"], "YES")

        row = self.web.conn.execute(
            """
            SELECT status, attempt_mode, resolved_price, threshold_price
            FROM redemption_log
            WHERE wallet = ? AND condition_id = ? AND outcome = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (wallet.lower(), "cond-1", "yes"),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "dry_run")
        self.assertEqual(str(row["attempt_mode"]), "dry_run")
        self.assertAlmostEqual(float(row["resolved_price"]), 1.0, places=6)
        self.assertAlmostEqual(float(row["threshold_price"]), 0.97, places=6)

    def test_real_positions_redeem_noop_when_success_already_logged(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xredeembuy2", "cond-noop", "Redeem noop", "redeem-noop", "YES", "BUY", 0.35, 100.0, 35.0, now_ts - 60),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-noop",
            "yes",
            resolved_price=1.0,
            resolved_at=now_ts - 5,
        )
        self.web.conn.execute(
            """
            INSERT INTO redemption_log (
                wallet, condition_id, outcome, attempt_mode, status, resolved_price,
                threshold_price, tx_hash, metadata_json, attempted_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet.lower(), "cond-noop", "yes", "live", "success", 1.0, 0.97, "0xabc", "{}", now_ts - 2),
        )
        self.web.conn.commit()

        resp = self.client.post(
            "/api/real-positions/redeem",
            json={"wallet": wallet, "condition_id": "cond-noop", "outcome": "YES"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(bool(payload["ok"]))
        self.assertTrue(bool(payload["already_redeemed"]))

        count = self.web.conn.execute(
            "SELECT COUNT(*) AS c FROM redemption_log WHERE wallet = ? AND condition_id = ? AND outcome = ?",
            (wallet.lower(), "cond-noop", "yes"),
        ).fetchone()["c"]
        self.assertEqual(int(count), 1)

    def test_real_positions_redeem_rejects_below_threshold_resolution(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xredeembuy3", "cond-low-price", "Redeem low", "redeem-low", "YES", "BUY", 0.50, 100.0, 50.0, now_ts - 60),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-low-price",
            "yes",
            resolved_price=0.95,
            resolved_at=now_ts - 5,
        )
        self.web.conn.commit()

        with patch.dict(os.environ, {"REDEEM_MIN_RESOLVED_PRICE": "0.97"}, clear=False):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-low-price", "outcome": "YES"},
            )

        self.assertEqual(resp.status_code, 409)
        payload = resp.get_json()
        self.assertFalse(bool(payload["ok"]))
        self.assertEqual(payload["code"], "not_redeemable_loss")
        self.assertIn("resolved against", str(payload["error"]).lower())

    def test_real_positions_redeem_falls_back_to_followed_positions(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                "cond-followed-redeem",
                "yes",
                "Followed redeem source",
                "followed-redeem-source",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.42,
                0.99,
                1200.0,
                1300.0,
                1500.0,
                100.0,
                0.0,
            ),
        )
        upsert_market_outcome(
            self.web.conn,
            "cond-followed-redeem",
            "yes",
            resolved_price=1.0,
            resolved_at=now_ts - 5,
        )
        self.web.conn.commit()

        with patch.dict(os.environ, {"REDEEM_DRY_RUN": "1", "REDEEM_ARMED": "0"}, clear=False):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-followed-redeem", "outcome": "YES"},
            )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(bool(payload["ok"]))
        self.assertTrue(bool(payload["dry_run"]))
        self.assertEqual(payload["position_source"], "followed_positions")

    def test_real_positions_redeem_auto_syncs_missing_market_outcome(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            INSERT INTO followed_positions (
                wallet, condition_id, outcome, title, event_slug, label, added_source,
                added_ts, last_synced_ts, entry_price, current_price, entry_notional,
                current_amount, current_size, cash_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wallet,
                "cond-sync-redeem",
                "yes",
                "Sync redeem source",
                "sync-redeem-source",
                "Manual",
                "manual",
                now_ts,
                now_ts,
                0.33,
                0.50,
                900.0,
                1000.0,
                1200.0,
                100.0,
                0.0,
            ),
        )
        self.web.conn.commit()

        class _FakeResp:
            status_code = 200
            content = b"{}"

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "closed": True,
                    "closedTime": now_ts - 10,
                    "tokens": [
                        {"outcome": "YES", "winner": True, "price": 1.0},
                        {"outcome": "NO", "winner": False, "price": 0.0},
                    ],
                }

        class _FakeSession:
            def get(self, *_args, **_kwargs):
                return _FakeResp()

            def close(self):
                return None

        with patch.dict(
            os.environ,
            {"REDEEM_DRY_RUN": "1", "REDEEM_ARMED": "0", "REDEEM_AUTO_SYNC_OUTCOME": "1"},
            clear=False,
        ):
            with patch("sniper.routes.api_followed.build_session", return_value=_FakeSession()):
                resp = self.client.post(
                    "/api/real-positions/redeem",
                    json={"wallet": wallet, "condition_id": "cond-sync-redeem", "outcome": "YES"},
                )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(bool(payload["ok"]))
        self.assertEqual(payload["position_source"], "followed_positions")

        outcome_row = self.web.conn.execute(
            """
            SELECT resolved_price, source
            FROM market_outcomes
            WHERE condition_id = ? AND outcome = ?
            ORDER BY resolved_at DESC
            LIMIT 1
            """,
            ("cond-sync-redeem", "yes"),
        ).fetchone()
        self.assertIsNotNone(outcome_row)
        self.assertAlmostEqual(float(outcome_row["resolved_price"]), 1.0, places=6)
        self.assertEqual(str(outcome_row["source"]), "redeem_condition_sync")

    def test_execute_real_trade_endpoint_exists_and_defaults_disabled(self) -> None:
        payload = {
            "condition_id": "cond-demo",
            "outcome": "YES",
            "size_usdc": 10.0,
        }
        with patch.dict(os.environ, {"REAL_TRADING_ENABLED": "false"}, clear=False):
            resp = self.client.post("/api/execute-real-trade", json=payload)

        self.assertEqual(resp.status_code, 501)
        data = resp.get_json()
        self.assertFalse(bool(data.get("enabled")))
        self.assertIn("disabled", str(data.get("error", "")).lower())

    def test_execute_real_trade_endpoint_validates_payload(self) -> None:
        resp_missing = self.client.post("/api/execute-real-trade", json={})
        self.assertEqual(resp_missing.status_code, 400)

        resp_bad_size = self.client.post(
            "/api/execute-real-trade",
            json={"condition_id": "cond-demo", "outcome": "YES", "size_usdc": 0},
        )
        self.assertEqual(resp_bad_size.status_code, 400)

    def test_open_position_signals_respects_focused_and_active_non_holding_params(self) -> None:
        wallet1, _, _, _ = self._seed(include_second_wallet=True)
        now_ts = int(time.time())
        upsert_position_signal(
            self.web.conn,
            wallet=wallet1,
            condition_id="cond-low-signal",
            outcome="Yes",
            title="Low signal holding row",
            event_slug="low-signal-holding-row",
            is_open=True,
            position_value=2500,
            position_share=0.08,
            position_count_wallet=6,
            entry_anchor_ts=now_ts - 3600,
            latest_trade_ts=0,
            buys_5m=0,
            buys_1h=0,
            buys_24h=0,
            trades_5m=0,
            trades_1h=0,
            trades_24h=0,
            sells_5m=0,
            sells_1h=0,
            sells_24h=0,
            notional_5m=0,
            notional_1h=0,
            notional_24h=0,
            sell_notional_5m=0,
            sell_notional_1h=0,
            sell_notional_24h=0,
            buys_since_entry=1,
            sells_since_entry=0,
            buy_notional_since_entry=2500,
            sell_notional_since_entry=0,
            net_notional_24h=0,
            net_notional_since_entry=2500,
            last_trade_side="BUY",
            activity_state="holding",
            hold_hours=48,
            unique_sizes_5m=0,
            max_trades_60s=0,
            position_insider_score=12,
            position_speculator_score=8,
            position_classification="position_low_signal",
            signal_reason="seed low-signal holding",
            analyzed_ts=now_ts,
        )
        self.web.conn.commit()

        payload_default = self.client.get("/api/open-position-signals").get_json()
        payload_focused = self.client.get("/api/open-position-signals?focused=1").get_json()
        payload_unfocused = self.client.get("/api/open-position-signals?focused=0").get_json()
        payload_active = self.client.get("/api/open-position-signals?focused=0&active_non_holding=1").get_json()
        payload_hide_low = self.client.get("/api/open-position-signals?focused=0&hide_low_signal=1").get_json()

        self.assertTrue(payload_default["focused_only"])
        self.assertTrue(payload_focused["focused_only"])
        self.assertFalse(payload_unfocused["focused_only"])
        self.assertEqual(payload_default["count"], payload_focused["count"])
        self.assertGreater(payload_unfocused["count"], payload_focused["count"])
        self.assertTrue(payload_active["active_non_holding_only"])
        self.assertLess(payload_active["count"], payload_unfocused["count"])
        self.assertTrue(payload_hide_low["hide_low_signal"])
        self.assertLess(payload_hide_low["count"], payload_unfocused["count"])
        self.assertTrue(
            all(
                row.get("position_classification") != "position_low_signal"
                for row in payload_hide_low["items"]
            )
        )

    def test_force_sync_endpoints_report_queue_state(self) -> None:
        self._seed(include_second_wallet=False)
        with patch.object(self.web, "_enqueue_background_sync", return_value=True) as enqueue:
            watch = self.client.get("/api/watchlist-trades?force=1").get_json()
            followed = self.client.get("/api/followed-trades?force=1").get_json()
            positions = self.client.get("/api/followed-positions?force=1").get_json()

        self.assertTrue(watch["force_requested"])
        self.assertTrue(watch["sync_queued"])
        self.assertTrue(followed["force_requested"])
        self.assertTrue(followed["sync_queued"])
        self.assertTrue(positions["force_requested"])
        self.assertTrue(positions["sync_queued"])

        queued_jobs = [call.args[0] for call in enqueue.call_args_list]
        self.assertIn("watchlist:*", queued_jobs)
        self.assertIn("followed-trades:*", queued_jobs)
        self.assertIn("followed-positions:*", queued_jobs)

    def test_emerging_insiders_endpoint(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)
        now_ts = int(time.time())
        self.web.conn.execute(
            """
            UPDATE user_analysis
               SET classification = 'emerging_insider',
                   insider_score = 67,
                   speculator_score = 24,
                   position_count = 4,
                   max_position_value = 7200,
                   top_open_position_value = 7200,
                   total_open_value = 18000,
                   position_insider_candidate_count = 1,
                   position_speculator_count = 0,
                   max_position_insider_score = 72,
                   max_position_speculator_score = 18,
                   analyzed_ts = ?,
                   reason = 'seed emerging insider'
             WHERE wallet = ?
            """,
            (now_ts, wallet2),
        )
        self.web.conn.commit()

        payload = self.client.get("/api/emerging-insiders").get_json()
        wallets = {row["wallet"] for row in payload["items"]}
        self.assertIn(wallet2, wallets)
        self.assertNotIn(wallet1, wallets)
        row = next(item for item in payload["items"] if item["wallet"] == wallet2)
        self.assertEqual(row["classification"], "emerging_insider")
        for key in [
            "username",
            "profile_image",
            "insider_score",
            "speculator_score",
            "position_signal_count",
            "position_insider_candidate_count",
            "max_position_insider_score",
            "eligibility_blockers",
        ]:
            self.assertIn(key, row)

    def test_speculator_flow_movers_endpoint(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)
        payload = self.client.get("/api/speculator-flow-movers").get_json()
        wallets = {str(row["wallet"]).lower() for row in payload["items"]}
        self.assertIn(wallet2, wallets)
        self.assertNotIn(wallet1, wallets)

        self.assertGreaterEqual(payload["count"], 1)
        row = payload["items"][0]
        for key in [
            "username",
            "profile_image",
            "classification",
            "insider_score",
            "speculator_score",
            "position_speculator_count",
            "max_position_speculator_score",
            "strategy_group",
            "position_count",
            "pnl",
            "max_position_value",
            "total_open_value",
            "eligibility_blockers",
        ]:
            self.assertIn(key, row)

    def test_closed_suspicious_trades_hidden_unless_followed(self) -> None:
        wallet1, _, tx1, condition1 = self._seed(include_second_wallet=False)
        now_ts = int(time.time())
        key = (condition1.lower(), "yes")
        self.web._set_suspicious_status_cache(
            wallet1,
            open_by_key={},
            closed_by_key={
                key: {
                    "conditionId": condition1,
                    "outcome": "Yes",
                    "realizedPnl": 250,
                    "timestamp": now_ts,
                    "curPrice": 0.91,
                    "endDate": "2030-01-01T00:00:00Z",
                }
            },
            synced_ts=now_ts,
        )

        suspicious = self.client.get("/api/recent-suspicious-trades").get_json()
        self.assertEqual(suspicious["count"], 0)

        self.web.upsert_followed_trade(
            self.web.conn,
            wallet=wallet1,
            transaction_hash=tx1,
            condition_id=condition1,
            side="BUY",
            outcome="Yes",
            title="Will launch happen by Friday?",
            event_slug="will-launch-happen-by-friday",
            entry_price=0.15,
            entry_size=20000,
            entry_ts=now_ts,
        )

        suspicious = self.client.get("/api/recent-suspicious-trades").get_json()
        self.assertEqual(suspicious["count"], 1)
        item = suspicious["items"][0]
        self.assertEqual(item["transaction_hash"], tx1)
        self.assertTrue(item["is_followed_trade"])
        self.assertIn("trigger_type", item)
        self.assertIn("trigger_reason", item)
        self.assertIn("username", item)
        self.assertIn("position_classification", item)
        self.assertIn("eligibility_blockers", item)

    def test_followed_status_marks_pending_settlement_open_rows(self) -> None:
        status = self.web._resolve_followed_status(
            open_position={
                "curPrice": 1.0,
                "currentValue": 5000,
                "size": 5000,
                "cashPnl": 900,
                "endDate": "2020-01-01T00:00:00Z",
            },
            closed_position=None,
        )
        self.assertEqual(status["status"], "pending_settlement_win")
        self.assertEqual(status["status_label"], "Pending Settlement (Likely Won)")
        self.assertFalse(status["is_closed"])

    def test_global_delete_endpoint_tombstones_and_removes_trade_across_tables(self) -> None:
        wallet1, _, tx1, condition1 = self._seed(include_second_wallet=False)
        now_ts = int(time.time())

        self.web.conn.execute(
            """
            INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts)
            VALUES (?, ?, ?, ?)
            """,
            (wallet1, "seed watch", now_ts, now_ts),
        )
        self.web.conn.execute(
            """
            INSERT INTO watchlist_trades (
                wallet,
                transaction_hash,
                condition_id,
                side,
                price,
                size,
                notional,
                timestamp,
                title,
                outcome,
                event_slug,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watchlist_sync')
            """,
            (
                wallet1,
                tx1,
                condition1,
                "BUY",
                0.15,
                20000.0,
                3000.0,
                now_ts,
                "Will launch happen by Friday?",
                "Yes",
                "will-launch-happen-by-friday",
            ),
        )
        self.web.upsert_followed_trade(
            self.web.conn,
            wallet=wallet1,
            transaction_hash=tx1,
            condition_id=condition1,
            side="BUY",
            outcome="Yes",
            title="Will launch happen by Friday?",
            event_slug="will-launch-happen-by-friday",
            entry_price=0.15,
            entry_size=20000,
            entry_ts=now_ts,
        )

        resp = self.client.delete(f"/api/trades/{tx1}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["tombstoned"])
        self.assertEqual(str(body["transaction_hash"]).lower(), tx1.lower())
        self.assertGreaterEqual(int(body["deleted_counts"]["low_buy_trades"]), 1)
        self.assertGreaterEqual(int(body["deleted_counts"]["watchlist_trades"]), 1)
        self.assertGreaterEqual(int(body["deleted_counts"]["followed_trades"]), 1)
        self.assertTrue(is_trade_deleted(self.web.conn, tx1))

        suspicious = self.client.get("/api/recent-suspicious-trades").get_json()
        self.assertFalse(any(str(row["transaction_hash"]).lower() == tx1.lower() for row in suspicious["items"]))
        watchlist = self.client.get("/api/watchlist-trades").get_json()
        self.assertFalse(any(str(row["transaction_hash"]).lower() == tx1.lower() for row in watchlist["items"]))
        followed = self.client.get("/api/followed-trades").get_json()
        self.assertFalse(any(str(row["transaction_hash"]).lower() == tx1.lower() for row in followed["items"]))

        follow_resp = self.client.post(
            "/api/followed-trades",
            json={
                "wallet": wallet1,
                "transaction_hash": tx1,
                "condition_id": condition1,
                "side": "BUY",
                "outcome": "Yes",
                "title": "Will launch happen by Friday?",
                "event_slug": "will-launch-happen-by-friday",
                "entry_price": 0.15,
                "entry_size": 20000,
                "entry_ts": now_ts,
            },
        )
        self.assertEqual(follow_resp.status_code, 409)

    def test_delete_alias_endpoints_delegate_to_global_delete(self) -> None:
        wallet1, _, tx1, condition1 = self._seed(include_second_wallet=False)
        now_ts = int(time.time())

        self.web.conn.execute(
            """
            INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts)
            VALUES (?, ?, ?, ?)
            """,
            (wallet1, "seed watch", now_ts, now_ts),
        )
        self.web.conn.execute(
            """
            INSERT INTO watchlist_trades (
                wallet,
                transaction_hash,
                condition_id,
                side,
                price,
                size,
                notional,
                timestamp,
                title,
                outcome,
                event_slug,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watchlist_sync')
            """,
            (
                wallet1,
                tx1,
                condition1,
                "BUY",
                0.15,
                20000.0,
                3000.0,
                now_ts,
                "Will launch happen by Friday?",
                "Yes",
                "will-launch-happen-by-friday",
            ),
        )
        self.web.upsert_followed_trade(
            self.web.conn,
            wallet=wallet1,
            transaction_hash=tx1,
            condition_id=condition1,
            side="BUY",
            outcome="Yes",
            title="Will launch happen by Friday?",
            event_slug="will-launch-happen-by-friday",
            entry_price=0.15,
            entry_size=20000,
            entry_ts=now_ts,
        )

        resp = self.client.delete(f"/api/suspicious-trades/{tx1}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("deleted_counts", body)
        self.assertTrue(body["tombstoned"])
        self.assertTrue(is_trade_deleted(self.web.conn, tx1))

        # Idempotent alias call should still succeed and remain tombstoned.
        resp2 = self.client.delete(f"/api/watchlist-trades/{wallet1}/{tx1}")
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.get_json()["ok"])
        self.assertTrue(is_trade_deleted(self.web.conn, tx1))

    def test_ignore_wallet_hides_wallet_across_key_endpoints(self) -> None:
        wallet1, _, _, _ = self._seed(include_second_wallet=True)

        before_flagged = self.client.get("/api/flagged-users").get_json()
        self.assertTrue(any(str(row["wallet"]).lower() == wallet1 for row in before_flagged["items"]))

        before_suspicious = self.client.get("/api/recent-suspicious-trades").get_json()
        self.assertTrue(any(str(row["wallet"]).lower() == wallet1 for row in before_suspicious["items"]))

        before_open = self.client.get("/api/open-position-signals").get_json()
        self.assertTrue(any(str(row["wallet"]).lower() == wallet1 for row in before_open["items"]))

        resp = self.client.post(
            "/api/ignored-wallets",
            json={"wallet": wallet1, "reason": "not significant"},
        )
        self.assertEqual(resp.status_code, 200)

        ignored = self.client.get("/api/ignored-wallets").get_json()
        self.assertTrue(any(str(row["wallet"]).lower() == wallet1 for row in ignored["items"]))

        after_flagged = self.client.get("/api/flagged-users").get_json()
        self.assertFalse(any(str(row["wallet"]).lower() == wallet1 for row in after_flagged["items"]))

        after_suspicious = self.client.get("/api/recent-suspicious-trades").get_json()
        self.assertFalse(any(str(row["wallet"]).lower() == wallet1 for row in after_suspicious["items"]))

        after_open = self.client.get("/api/open-position-signals").get_json()
        self.assertFalse(any(str(row["wallet"]).lower() == wallet1 for row in after_open["items"]))

        resp = self.client.delete(f"/api/ignored-wallets/{wallet1}")
        self.assertEqual(resp.status_code, 200)
        final_flagged = self.client.get("/api/flagged-users").get_json()
        self.assertTrue(any(str(row["wallet"]).lower() == wallet1 for row in final_flagged["items"]))

    def test_agent_high_signal_and_alert_outbox_flow(self) -> None:
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)
        self.web.conn.execute(
            """
            UPDATE position_signals
               SET position_value = 9000
             WHERE wallet = ?
               AND position_classification = 'position_speculator_ladder'
            """,
            (wallet2,),
        )
        self.web.conn.commit()

        feed = self.client.get("/api/agent/high-signal?enqueue_alerts=1").get_json()
        self.assertGreaterEqual(feed["count"], 1)
        self.assertIn("alerts", feed)
        self.assertGreaterEqual(int(feed["alerts"]["created"]), 1)
        self.assertTrue(all(str(item["wallet"]).lower() != "" for item in feed["items"]))

        feed_deduped = self.client.get("/api/agent/high-signal?enqueue_alerts=1").get_json()
        self.assertGreaterEqual(int(feed_deduped["alerts"]["deduped"]), 1)

        feed_no_spec = self.client.get("/api/agent/high-signal?include_speculators=0").get_json()
        self.assertFalse(any(item["signal_type"] == "position_speculator_ladder" for item in feed_no_spec["items"]))
        feed_with_spec = self.client.get("/api/agent/high-signal?include_speculators=1").get_json()
        self.assertTrue(any(item["signal_type"] == "position_speculator_ladder" for item in feed_with_spec["items"]))

        alerts = self.client.get("/api/agent/alerts?status=new,sent,acknowledged").get_json()
        self.assertGreaterEqual(alerts["count"], 1)
        first = alerts["items"][0]
        self.assertIn("dedupe_key", first)
        self.assertIn("payload", first)
        self.assertIn("username", first)

        alert_id = int(first["id"])
        sent_resp = self.client.post(f"/api/agent/alerts/{alert_id}/sent")
        self.assertEqual(sent_resp.status_code, 200)
        self.assertEqual(sent_resp.get_json()["status"], "sent")

        ack_resp = self.client.post(f"/api/agent/alerts/{alert_id}/ack")
        self.assertEqual(ack_resp.status_code, 200)
        self.assertEqual(ack_resp.get_json()["status"], "acknowledged")

        ack_key_resp = self.client.post(
            "/api/agent/alerts/ack-by-key",
            json={"dedupe_key": str(first["dedupe_key"])},
        )
        self.assertEqual(ack_key_resp.status_code, 200)
        self.assertEqual(ack_key_resp.get_json()["status"], "acknowledged")

    def test_agent_capabilities_and_bulk_ack_endpoints(self) -> None:
        self._seed(include_second_wallet=True)
        capabilities = self.client.get("/api/agent/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        cap = capabilities.get_json()
        self.assertTrue(cap["ok"])
        self.assertIn("api_version", cap)
        self.assertIn("auth_required", cap)
        self.assertIn("limits", cap)
        self.assertIn("endpoints", cap)

        generated = self.client.post("/api/agent/alerts/generate", json={"limit": 25})
        self.assertEqual(generated.status_code, 200)
        alerts = self.client.get("/api/agent/alerts?status=new,sent,acknowledged").get_json()
        self.assertGreaterEqual(alerts["count"], 1)
        first = alerts["items"][0]

        bulk = self.client.post(
            "/api/agent/alerts/ack-bulk",
            json={
                "ids": [int(first["id"])],
                "dedupe_keys": [str(first["dedupe_key"])],
            },
        )
        self.assertEqual(bulk.status_code, 200)
        body = bulk.get_json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(int(body["ack_count"]), 1)

    def test_agent_mute_rules_filter_feed(self) -> None:
        wallet1, _, _, condition1 = self._seed(include_second_wallet=True)
        feed_before = self.client.get("/api/agent/high-signal?include_speculators=1").get_json()
        wallets_before = {str(item["wallet"]).lower() for item in feed_before["items"]}
        self.assertIn(wallet1, wallets_before)

        mute_wallet = self.client.post(
            "/api/agent/mutes",
            json={
                "scope_type": "wallet",
                "wallet": wallet1,
                "reason": "test wallet mute",
                "ttl_seconds": 3600,
            },
        )
        self.assertEqual(mute_wallet.status_code, 200)

        mute_position = self.client.post(
            "/api/agent/mutes",
            json={
                "scope_type": "position",
                "condition_id": condition1,
                "outcome": "yes",
                "reason": "test position mute",
                "ttl_seconds": 3600,
            },
        )
        self.assertEqual(mute_position.status_code, 200)
        position_mute_id = int(mute_position.get_json()["item"]["id"])

        muted_list = self.client.get("/api/agent/mutes").get_json()
        self.assertGreaterEqual(muted_list["count"], 2)

        feed_after = self.client.get("/api/agent/high-signal?include_speculators=1").get_json()
        wallets_after = {str(item["wallet"]).lower() for item in feed_after["items"]}
        self.assertNotIn(wallet1, wallets_after)

        delete_resp = self.client.delete(f"/api/agent/mutes/{position_mute_id}")
        self.assertEqual(delete_resp.status_code, 200)
        self.assertTrue(delete_resp.get_json()["ok"])

    def test_api_key_guard_is_opt_in_and_blocks_without_key(self) -> None:
        wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        original_key = str(getattr(self.web, "SNIPER_API_KEY", "") or "")
        original_protect_mutating = bool(getattr(self.web, "SNIPER_API_KEY_PROTECT_MUTATING", False))
        self.web.SNIPER_API_KEY = "test-secret-key"
        self.web.SNIPER_API_KEY_PROTECT_MUTATING = False
        original_proxy_secret = self.web._TRUSTED_PROXY_AUTH_VALUE
        self.web._TRUSTED_PROXY_AUTH_VALUE = "test-proxy-secret"
        trusted_proxy_headers = {"X-Sniper-Proxy-Auth": "test-proxy-secret"}
        trusted_proxy_environ = {"REMOTE_ADDR": "127.0.0.1"}
        try:
            denied_agent = self.client.get("/api/agent/high-signal")
            self.assertEqual(denied_agent.status_code, 401)

            denied_mutating = self.client.post("/api/ignored-wallets", json={"wallet": wallet, "reason": "test"})
            self.assertEqual(denied_mutating.status_code, 200)

            allowed_agent = self.client.get(
                "/api/agent/high-signal",
                headers={"X-API-Key": "test-secret-key"},
            )
            self.assertEqual(allowed_agent.status_code, 200)

            allowed_agent_via_proxy = self.client.get(
                "/api/agent/high-signal",
                headers=trusted_proxy_headers,
                environ_overrides=trusted_proxy_environ,
            )
            self.assertEqual(allowed_agent_via_proxy.status_code, 200)

            allowed_mutating = self.client.post(
                "/api/ignored-wallets",
                json={"wallet": wallet, "reason": "test"},
                headers={"X-API-Key": "test-secret-key"},
            )
            self.assertEqual(allowed_mutating.status_code, 200)
            self.assertTrue(allowed_mutating.get_json()["ok"])

            self.web.SNIPER_API_KEY_PROTECT_MUTATING = True
            denied_mutating_hardened = self.client.post(
                "/api/ignored-wallets",
                json={"wallet": wallet, "reason": "test"},
            )
            self.assertEqual(denied_mutating_hardened.status_code, 401)

            allowed_mutating_hardened_via_proxy = self.client.post(
                "/api/ignored-wallets",
                json={"wallet": wallet, "reason": "test"},
                headers=trusted_proxy_headers,
                environ_overrides=trusted_proxy_environ,
            )
            self.assertEqual(allowed_mutating_hardened_via_proxy.status_code, 200)
            self.assertTrue(allowed_mutating_hardened_via_proxy.get_json()["ok"])

            allowed_mutating_hardened = self.client.post(
                "/api/ignored-wallets",
                json={"wallet": wallet, "reason": "test"},
                headers={"X-API-Key": "test-secret-key"},
            )
            self.assertEqual(allowed_mutating_hardened.status_code, 200)
            self.assertTrue(allowed_mutating_hardened.get_json()["ok"])

            denied_execute = self.client.post(
                "/api/trade-ideas/123/execute",
                json={"size_usdc": 10.0},
            )
            self.assertEqual(denied_execute.status_code, 401)

            with patch("sniper.routes.api_core.execute_trade_idea", return_value={"ok": True, "paper_trade_id": 1}) as mocked_execute:
                allowed_execute_via_proxy = self.client.post(
                    "/api/trade-ideas/123/execute",
                    json={"size_usdc": 10.0},
                    headers=trusted_proxy_headers,
                    environ_overrides=trusted_proxy_environ,
                )
            self.assertEqual(allowed_execute_via_proxy.status_code, 200)
            self.assertTrue(allowed_execute_via_proxy.get_json()["ok"])
            mocked_execute.assert_called_once()

            home = self.client.get("/").get_data(as_text=True)
            self.assertNotIn("window.SNIPER_CONFIG", home)
            self.assertNotIn("test-secret-key", home)
        finally:
            self.web.SNIPER_API_KEY = original_key
            self.web.SNIPER_API_KEY_PROTECT_MUTATING = original_protect_mutating
            self.web._TRUSTED_PROXY_AUTH_VALUE = original_proxy_secret

    def test_basic_auth_trusts_only_the_marked_loopback_gateway(self) -> None:
        marker = {"X-Sniper-Proxy-Auth": "test-proxy-secret"}
        with patch.object(self.web, "_DASH_USER", "test"), \
                patch.object(self.web, "_DASH_PASS", "secret"), \
                patch.object(self.web, "_TRUSTED_PROXY_AUTH_VALUE", "test-proxy-secret"):
            allowed = self.client.get(
                "/health", headers=marker,
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            unmarked = self.client.get(
                "/health", environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            remote_spoof = self.client.get(
                "/health", headers=marker,
                environ_overrides={"REMOTE_ADDR": "203.0.113.4"},
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(unmarked.status_code, 401)
        self.assertEqual(remote_spoof.status_code, 401)

    def test_the_old_constant_marker_from_the_tunnel_is_refused(self) -> None:
        """2026-09-27: cloudflared forwards the internet to 127.0.0.1:5000, so a request through the
        tunnel reaches Flask from loopback. The marker was the constant "nginx-basic": sending it
        skipped Basic Auth on every route. Now it is a secret, and unset means never trusted."""
        loopback = {"REMOTE_ADDR": "127.0.0.1"}
        with patch.object(self.web, "_DASH_USER", "test"), \
                patch.object(self.web, "_DASH_PASS", "secret"), \
                patch.object(self.web, "_TRUSTED_PROXY_AUTH_VALUE", ""):
            for path in ("/health", "/api/memecoin/csrf-token"):
                for value in ("nginx-basic", "", "NGINX-BASIC"):
                    r = self.client.get(path, headers={"X-Sniper-Proxy-Auth": value},
                                        environ_overrides=loopback)
                    self.assertEqual(r.status_code, 401, (path, value))
        with patch.object(self.web, "_DASH_USER", "test"), \
                patch.object(self.web, "_DASH_PASS", "secret"), \
                patch.object(self.web, "_TRUSTED_PROXY_AUTH_VALUE", "test-proxy-secret"):
            wrong = self.client.get("/api/memecoin/csrf-token",
                                    headers={"X-Sniper-Proxy-Auth": "nginx-basic"},
                                    environ_overrides=loopback)
            right = self.client.get("/api/memecoin/csrf-token",
                                    headers={"X-Sniper-Proxy-Auth": "test-proxy-secret"},
                                    environ_overrides=loopback)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(right.status_code, 200)

    def test_the_proxy_secret_is_read_from_the_environment_and_defaults_to_untrusted(self) -> None:
        import inspect
        src = inspect.getsource(self.web)
        self.assertIn('_TRUSTED_PROXY_AUTH_VALUE = os.getenv("SNIPER_PROXY_AUTH_SECRET", "").strip()', src)
        self.assertNotIn('"nginx-basic"', src)

    def test_basic_auth_issues_cookie_for_stream_handshakes(self) -> None:
        with patch.object(self.web, "_DASH_USER", "test"), \
                patch.object(self.web, "_DASH_PASS", "secret"):
            denied = self.client.get("/stream")
            authenticated = self.client.get(
                "/health", headers={"Authorization": "Basic dGVzdDpzZWNyZXQ="},
            )
            handshake = self.client.get("/stream")
            self.client.set_cookie("sniper_dash_session", "0.tampered")
            tampered = self.client.get("/stream")

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)
        cookie = authenticated.headers.get("Set-Cookie", "")
        self.assertIn("sniper_dash_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotEqual(handshake.status_code, 401)
        self.assertEqual(tampered.status_code, 401)

    def test_api_response_field_snapshots(self) -> None:
        """Phase 1 hardening: ensure key API endpoints return expected field structure."""
        wallet1, wallet2, _, _ = self._seed(include_second_wallet=True)

        # Test /api/flagged-users
        flagged = self.client.get("/api/flagged-users").get_json()
        self.assertIn("items", flagged)
        if flagged["items"]:
            item = flagged["items"][0]
            expected_fields = {
                "wallet", "username", "insider_score", "speculator_score",
                "classification", "pnl", "max_position_value", "total_open_value",
                "position_count", "low_buy_hits_14d", "profile_image", "flagged_ts"
            }
            self.assertTrue(expected_fields.issubset(item.keys()), f"Missing fields in flagged-users: {expected_fields - item.keys()}")

        # Test /api/high-winrate-wallets
        high_winrate = self.client.get("/api/high-winrate-wallets").get_json()
        self.assertIn("items", high_winrate)
        if high_winrate["items"]:
            item = high_winrate["items"][0]
            expected_fields = {
                "wallet", "username", "insider_score", "pnl",
                "max_position_value", "win_rate", "trade_count"
            }
            self.assertTrue(expected_fields.issubset(item.keys()), f"Missing fields in high-winrate: {expected_fields - item.keys()}")

        # Test /api/emerging-insiders
        emerging = self.client.get("/api/emerging-insiders").get_json()
        self.assertIn("items", emerging)
        if emerging["items"]:
            item = emerging["items"][0]
            expected_fields = {
                "wallet", "username", "insider_score", "speculator_score",
                "classification", "total_open_value", "max_position_value"
            }
            self.assertTrue(expected_fields.issubset(item.keys()), f"Missing fields in emerging-insiders: {expected_fields - item.keys()}")

        # Test /api/speculator-flow-movers
        flow_movers = self.client.get("/api/speculator-flow-movers").get_json()
        self.assertIn("items", flow_movers)
        if flow_movers["items"]:
            item = flow_movers["items"][0]
            expected_fields = {
                "wallet", "username", "speculator_score", "pnl",
                "max_position_value", "position_count"
            }
            self.assertTrue(expected_fields.issubset(item.keys()), f"Missing fields in flow-movers: {expected_fields - item.keys()}")

        # Test /api/open-position-signals
        open_signals = self.client.get("/api/open-position-signals").get_json()
        self.assertIn("items", open_signals)
        if open_signals["items"]:
            item = open_signals["items"][0]
            expected_fields = {
                "wallet", "username", "condition_id", "position_value",
                "position_insider_score", "position_classification", "outcome",
                "market_volume_24h", "market_is_open", "market_end_date_iso"
            }
            self.assertTrue(expected_fields.issubset(item.keys()), f"Missing fields in open-position-signals: {expected_fields - item.keys()}")


    # ------------------------------------------------------------------
    # Redeem UX: redeemable / settled_loss split tests
    # ------------------------------------------------------------------

    def _seed_real_trade(self, wallet, tx_hash, condition_id, title, slug, outcome, side, price, size, traded_at):
        """Insert a real_trades row (creates table if needed)."""
        self.web.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """,
        )
        usdc = price * size
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, tx_hash, condition_id, title, slug, outcome, side, price, size, usdc, traded_at),
        )

    def test_real_trades_splits_resolved_into_redeemable_and_settled_loss(self) -> None:
        """Winning resolved → redeemable, losing resolved → settled_loss, both in resolved compat bucket."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())

        # Winner position (settlement_price=1.0)
        self._seed_real_trade(wallet, "0xwinbuy", "cond-win", "Winner", "winner", "YES", "BUY", 0.40, 100.0, now_ts - 60)
        upsert_market_outcome(self.web.conn, "cond-win", "yes", resolved_price=1.0, resolved_at=now_ts - 10)

        # Loser position (settlement_price=0.0)
        self._seed_real_trade(wallet, "0xlosebuy", "cond-lose", "Loser", "loser", "YES", "BUY", 0.60, 100.0, now_ts - 60)
        upsert_market_outcome(self.web.conn, "cond-lose", "yes", resolved_price=0.0, resolved_at=now_ts - 10)

        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()

        # New split buckets
        self.assertEqual(len(payload["redeemable"]), 1)
        self.assertEqual(len(payload["settled_loss"]), 1)

        winner = payload["redeemable"][0]
        self.assertEqual(winner["condition_id"], "cond-win")
        self.assertTrue(winner["can_redeem"])
        self.assertEqual(winner["redeem_state"], "claimable")

        loser = payload["settled_loss"][0]
        self.assertEqual(loser["condition_id"], "cond-lose")
        self.assertFalse(loser["can_redeem"])
        self.assertEqual(loser["redeem_state"], "settled_loss")

        # Compat: resolved = redeemable + settled_loss
        self.assertEqual(len(payload["resolved"]), 2)

        # Summary counts
        self.assertEqual(payload["summary"]["redeemable_count"], 1)
        self.assertEqual(payload["summary"]["settled_loss_count"], 1)
        self.assertEqual(payload["summary"]["resolved_count"], 2)

    def test_redeem_losing_position_returns_not_redeemable_loss_code(self) -> None:
        """POST /api/real-positions/redeem on a loser returns 409 with code=not_redeemable_loss."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self._seed_real_trade(wallet, "0xlosebuy2", "cond-lose2", "Loser2", "loser2", "YES", "BUY", 0.50, 100.0, now_ts - 60)
        upsert_market_outcome(self.web.conn, "cond-lose2", "yes", resolved_price=0.0, resolved_at=now_ts - 5)
        self.web.conn.commit()

        with patch.dict(os.environ, {"REDEEM_MIN_RESOLVED_PRICE": "0.97"}, clear=False):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-lose2", "outcome": "YES"},
            )

        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "not_redeemable_loss")
        self.assertIn("resolved against", data["error"].lower())
        self.assertAlmostEqual(data["resolved_price"], 0.0, places=6)
        self.assertAlmostEqual(data["threshold_price"], 0.97, places=6)

    def test_redeem_unresolved_returns_outcome_unresolved_code(self) -> None:
        """POST /api/real-positions/redeem on unresolved market returns 409 with code=outcome_unresolved."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self._seed_real_trade(wallet, "0xunresbuy", "cond-unres", "Unresolved", "unres", "YES", "BUY", 0.50, 100.0, now_ts - 60)
        self.web.conn.commit()

        with patch.dict(os.environ, {"REDEEM_AUTO_SYNC_OUTCOME": "0"}, clear=False):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-unres", "outcome": "YES"},
            )

        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "outcome_unresolved")
        self.assertIn("not resolved", data["error"].lower())

    def test_redeem_winner_does_not_return_threshold_409(self) -> None:
        """POST /api/real-positions/redeem on a winner should not 409 with threshold error."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self._seed_real_trade(wallet, "0xwinbuy2", "cond-win2", "Winner2", "winner2", "YES", "BUY", 0.30, 100.0, now_ts - 60)
        upsert_market_outcome(self.web.conn, "cond-win2", "yes", resolved_price=1.0, resolved_at=now_ts - 5)
        self.web.conn.commit()

        with patch.dict(os.environ, {"REDEEM_DRY_RUN": "1", "REDEEM_ARMED": "0", "REDEEM_MIN_RESOLVED_PRICE": "0.97"}, clear=False):
            resp = self.client.post(
                "/api/real-positions/redeem",
                json={"wallet": wallet, "condition_id": "cond-win2", "outcome": "YES"},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["dry_run"])

    def test_real_trades_moves_already_redeemed_winner_to_closed(self) -> None:
        """A resolved winner with redemption_log success should be in closed, not redeemable."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())

        self._seed_real_trade(wallet, "0xrwinbuy", "cond-rwin", "Redeemed Winner", "rwin", "YES", "BUY", 0.40, 100.0, now_ts - 120)
        upsert_market_outcome(self.web.conn, "cond-rwin", "yes", resolved_price=1.0, resolved_at=now_ts - 60)

        # Record a successful redemption
        self.web.conn.execute(
            """
            INSERT INTO redemption_log (
                wallet, condition_id, outcome, attempt_mode, status, resolved_price,
                threshold_price, tx_hash, amount_claimed, metadata_json, attempted_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet.lower(), "cond-rwin", "yes", "live", "success", 1.0, 0.97, "0xtxhash123", 100.0, "{}", now_ts - 30),
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()

        # Should NOT be in redeemable
        self.assertEqual(len(payload["redeemable"]), 0)
        # Should be in closed
        redeemed = [r for r in payload["closed"] if r.get("close_type") == "redeemed"]
        self.assertEqual(len(redeemed), 1)
        row = redeemed[0]
        self.assertEqual(row["condition_id"], "cond-rwin")
        self.assertEqual(row["close_type"], "redeemed")
        self.assertEqual(row["closed_source"], "redemption_log")
        self.assertEqual(row["tx_hash"], "0xtxhash123")
        self.assertAlmostEqual(float(row["exit_price"]), 1.0, places=6)
        self.assertAlmostEqual(float(row["received"]), 100.0, places=2)
        self.assertAlmostEqual(float(row["realized_pnl"]), 60.0, places=2)  # 100 - 40
        # Should NOT be in resolved compat bucket either
        resolved_cids = [r["condition_id"] for r in payload["resolved"]]
        self.assertNotIn("cond-rwin", resolved_cids)
        # Summary counts
        self.assertEqual(payload["summary"]["redeemable_count"], 0)

    def test_real_trades_unredeemed_winner_stays_in_redeemable(self) -> None:
        """A resolved winner without redemption success stays in redeemable."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())

        self._seed_real_trade(wallet, "0xurwinbuy", "cond-urwin", "Unredeemed Winner", "urwin", "YES", "BUY", 0.30, 100.0, now_ts - 120)
        upsert_market_outcome(self.web.conn, "cond-urwin", "yes", resolved_price=1.0, resolved_at=now_ts - 60)
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()

        self.assertEqual(len(payload["redeemable"]), 1)
        self.assertEqual(payload["redeemable"][0]["condition_id"], "cond-urwin")
        self.assertTrue(payload["redeemable"][0]["can_redeem"])
        # No redeemed rows in closed
        redeemed_in_closed = [r for r in payload["closed"] if r.get("close_type") == "redeemed"]
        self.assertEqual(len(redeemed_in_closed), 0)

    def test_real_trades_summary_counts_include_redeemed_in_closed(self) -> None:
        """Summary closed_count includes redeemed winners."""
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())

        # Regular closed trade (fully sold)
        self._seed_real_trade(wallet, "0xregbuy", "cond-reg", "Regular", "reg", "YES", "BUY", 0.30, 100.0, now_ts - 200)
        self._seed_real_trade(wallet, "0xregsell", "cond-reg", "Regular", "reg", "YES", "SELL", 0.60, 100.0, now_ts - 100)

        # Redeemed winner
        self._seed_real_trade(wallet, "0xrwbuy2", "cond-rw2", "RedeemedW2", "rw2", "YES", "BUY", 0.40, 100.0, now_ts - 120)
        upsert_market_outcome(self.web.conn, "cond-rw2", "yes", resolved_price=1.0, resolved_at=now_ts - 60)
        self.web.conn.execute(
            """
            INSERT INTO redemption_log (
                wallet, condition_id, outcome, attempt_mode, status, resolved_price,
                threshold_price, tx_hash, metadata_json, attempted_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet.lower(), "cond-rw2", "yes", "live", "success", 1.0, 0.97, "0xtx2", "{}", now_ts - 30),
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        self.assertEqual(payload["summary"]["closed_count"], 2)  # regular + redeemed
        self.assertEqual(payload["summary"]["redeemable_count"], 0)

    def test_real_trades_closed_row_uses_exit_price_not_signal_price(self) -> None:
        wallet = "0x92b83435aa738e4748382413e655ad565426ef05"
        now_ts = int(time.time())
        self.web.conn.execute("DROP TABLE IF EXISTS real_trades")
        self.web.conn.execute(
            """
            CREATE TABLE real_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                condition_id TEXT,
                title TEXT,
                event_slug TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                usdc_size REAL,
                traded_at INTEGER
            )
            """
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xbuy", "cond-closed-price", "Closed market", "closed-market", "NO", "BUY", 0.185, 54.05, 10.0, now_ts - 60),
        )
        self.web.conn.execute(
            """
            INSERT INTO real_trades
                (wallet, tx_hash, condition_id, title, event_slug, outcome, side, price, size, usdc_size, traded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, "0xsell", "cond-closed-price", "Closed market", "closed-market", "NO", "SELL", 0.0265, 54.05, 1.4324324324, now_ts - 10),
        )
        upsert_position_signal(
            self.web.conn,
            wallet="0x1234567890123456789012345678901234567890",
            condition_id="cond-closed-price",
            outcome="NO",
            title="Closed market",
            event_slug="closed-market",
            is_open=True,
            position_value=1000.0,
            position_share=0.1,
            position_count_wallet=1,
            entry_anchor_ts=now_ts - 3600,
            latest_trade_ts=now_ts - 60,
            buys_5m=0,
            buys_1h=0,
            buys_24h=1,
            trades_5m=0,
            trades_1h=0,
            trades_24h=1,
            sells_5m=0,
            sells_1h=0,
            sells_24h=0,
            notional_5m=0.0,
            notional_1h=0.0,
            notional_24h=1000.0,
            sell_notional_5m=0.0,
            sell_notional_1h=0.0,
            sell_notional_24h=0.0,
            buys_since_entry=1,
            sells_since_entry=0,
            buy_notional_since_entry=1000.0,
            sell_notional_since_entry=0.0,
            net_notional_24h=1000.0,
            net_notional_since_entry=1000.0,
            last_trade_side="BUY",
            activity_state="holding",
            hold_hours=1.0,
            unique_sizes_5m=1,
            max_trades_60s=1,
            entry_price=0.185,
            current_price=0.425,
            current_size=1000.0,
            position_classification="position_low_signal",
            position_insider_score=20.0,
            position_speculator_score=10.0,
            signal_reason="test",
            analyzed_ts=now_ts,
            analysis_version=1,
        )
        self.web.conn.commit()

        payload = self.client.get(f"/api/real-trades?wallet={wallet}").get_json()
        row = next((r for r in payload["closed"] if r["condition_id"] == "cond-closed-price"), None)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row["exit_price"]), 0.0265, places=4)
        self.assertAlmostEqual(float(row["current_price"]), 0.0265, places=4)
        self.assertEqual(row["price_source"], "exit_price")


class MigrationOrderTests(unittest.TestCase):
    """Phase 1 hardening: ensure migration structure handles schema evolution correctly."""

    def test_migrations_all_columns_present_after_init(self) -> None:
        """Verify init_db() creates all MIGRATIONS columns on a fresh database."""
        from db import MIGRATIONS

        conn = get_connection(":memory:")
        init_db(conn)

        # Verify all MIGRATIONS columns exist in their respective tables
        for table_name, columns in MIGRATIONS.items():
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            existing_columns = {row[1] for row in cursor.fetchall()}
            
            for col_name, _col_def in columns:
                self.assertIn(
                    col_name,
                    existing_columns,
                    f"Migration column {col_name} missing from {table_name} after init_db()"
                )
        
        conn.close()

    def test_migrations_are_idempotent(self) -> None:
        """Verify running init_db() twice doesn't fail."""
        conn = get_connection(":memory:")
        init_db(conn)
        # Second init should not raise
        init_db(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
