"""
Regression tests for Track A deliverables (2026-03-01).

Covers:
1. apply_phase_b_hardening() — market_metadata table + Phase B indexes created
2. upsert_market_metadata() — single-row upsert and conflict resolution
3. bulk_upsert_market_metadata() — batch upsert, empty-list guard, partial data
4. get_market_metadata() — read-back after upsert
5. META_REFRESH_INTERVAL_SECONDS env var honoured by stream_low_buys module
6. _metadata_refresh_task disabled when interval=0
7. _listener_tasks list managed correctly in run() startup
"""

import asyncio
import importlib
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ─── helpers ──────────────────────────────────────────────────────────────────

def _fresh_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from db import init_db
    init_db(conn)
    return conn


# ─── 1. apply_phase_b_hardening ──────────────────────────────────────────────

class TestApplyPhaseBHardening(unittest.TestCase):
    """apply_phase_b_hardening creates all expected tables and indexes."""

    def setUp(self):
        self.conn = _fresh_conn()

    def tearDown(self):
        self.conn.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return bool(row[0])

    def _index_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        return bool(row[0])

    def test_market_metadata_table_created(self):
        self.assertTrue(self._table_exists("market_metadata"))

    def test_market_outcomes_table_created(self):
        self.assertTrue(self._table_exists("market_outcomes"))

    def test_market_outcomes_has_expected_columns(self):
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(market_outcomes)")
        }
        expected = {
            "condition_id", "outcome", "resolved_price", "resolved_at",
            "source", "inserted_at", "updated_at",
        }
        self.assertEqual(expected, cols & expected)

    def test_market_metadata_has_expected_columns(self):
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(market_metadata)")
        }
        expected = {
            "condition_id", "title", "slug", "event_slug", "end_date_iso",
            "volume_24h", "is_open", "last_synced_ts", "inserted_at", "updated_at",
        }
        self.assertEqual(expected, cols & expected)

    def test_phase_b_indexes_created(self):
        # idx_low_buy_condition_ts is conditionally created (only when condition_id
        # column exists in low_buy_trades) — not checked here.
        expected_indexes = [
            "idx_market_metadata_synced",
            "idx_market_metadata_open_volume",
            "idx_market_outcomes_resolved_at",
            "idx_low_buy_wallet_ts",
            "idx_candidate_last_seen",
            "idx_candidate_hits",
            "idx_ua_class_score",
            "idx_ua_bot_match",
            "idx_alerts_sent_wallet_type_ts",
        ]
        missing = [idx for idx in expected_indexes if not self._index_exists(idx)]
        self.assertEqual([], missing, f"Missing indexes: {missing}")

    def test_condition_id_index_created_when_column_exists(self):
        """idx_low_buy_condition_ts is created on fresh DB (condition_id present)."""
        # Fresh in-memory DB always has condition_id — index must exist
        self.assertTrue(
            self._index_exists("idx_low_buy_condition_ts"),
            "idx_low_buy_condition_ts should exist on fresh DB with condition_id column",
        )

    def test_idempotent_double_call(self):
        """Calling apply_phase_b_hardening twice must not raise."""
        from db import apply_phase_b_hardening
        apply_phase_b_hardening(self.conn)  # second call
        self.assertTrue(self._table_exists("market_metadata"))

    def test_called_automatically_by_init_db(self):
        """init_db must call apply_phase_b_hardening — verified by table presence."""
        # _fresh_conn already called init_db; market_metadata must exist
        self.assertTrue(self._table_exists("market_metadata"))

    def test_alerts_sent_schema_supports_alerter_insert(self):
        """Fresh DB created by init_db must accept alerter.log_alert insert shape."""
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(alerts_sent)")
        }
        for required in ("alert_type", "wallet", "message", "channel", "sent_at"):
            self.assertIn(required, cols, f"alerts_sent missing column: {required}")

        # Must support INSERT used by alerter.log_alert(...)
        self.conn.execute(
            """
            INSERT INTO alerts_sent (alert_type, wallet, message, channel)
            VALUES (?, ?, ?, ?)
            """,
            ("new_insider", "0xabc", "test message", "telegram"),
        )
        self.conn.commit()

    def test_alerts_sent_legacy_variant_backfilled(self):
        """Legacy alerts_sent(id,payload-only) must be upgraded in-place."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE low_buy_trades (
                transaction_hash TEXT PRIMARY KEY,
                proxy_wallet TEXT,
                condition_id TEXT,
                timestamp INTEGER
            );
            CREATE TABLE candidate_users (
                wallet TEXT PRIMARY KEY,
                low_buy_hits INTEGER DEFAULT 0,
                last_seen_ts INTEGER DEFAULT 0
            );
            CREATE TABLE user_analysis (
                wallet TEXT PRIMARY KEY,
                insider_score REAL DEFAULT 0,
                is_bot INTEGER DEFAULT 0,
                matches_criteria INTEGER DEFAULT 0,
                classification TEXT DEFAULT '',
                analyzed_ts INTEGER DEFAULT 0
            );
            CREATE TABLE alerts_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL DEFAULT '',
                wallet TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payload TEXT
            );
            """
        )
        from db import apply_phase_b_hardening
        apply_phase_b_hardening(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts_sent)")}
        for required in ("message", "channel", "acknowledged", "response"):
            self.assertIn(required, cols, f"alerts_sent missing backfilled column: {required}")
        conn.execute(
            """
            INSERT INTO alerts_sent (alert_type, wallet, message, channel)
            VALUES (?, ?, ?, ?)
            """,
            ("top_performer", "0xdef", "ok", "telegram"),
        )
        conn.commit()
        conn.close()


# ─── 2. upsert_market_metadata ───────────────────────────────────────────────

class TestUpsertMarketMetadata(unittest.TestCase):
    """upsert_market_metadata correctly inserts and updates rows."""

    def setUp(self):
        self.conn = _fresh_conn()

    def tearDown(self):
        self.conn.close()

    def test_insert_new_row(self):
        from db import upsert_market_metadata, get_market_metadata
        upsert_market_metadata(
            self.conn,
            "cond_abc",
            title="Will X happen?",
            slug="will-x-happen",
            event_slug="event-x",
            end_date_iso="2026-06-01T00:00:00Z",
            volume_24h=12345.67,
            is_open=1,
        )
        self.conn.commit()
        row = get_market_metadata(self.conn, "cond_abc")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Will X happen?")
        self.assertAlmostEqual(row["volume_24h"], 12345.67, places=1)
        self.assertEqual(row["is_open"], 1)

    def test_update_existing_row(self):
        from db import upsert_market_metadata, get_market_metadata
        upsert_market_metadata(self.conn, "cond_upd", title="Old title", volume_24h=100.0)
        self.conn.commit()
        upsert_market_metadata(self.conn, "cond_upd", title="New title", volume_24h=999.0)
        self.conn.commit()
        row = get_market_metadata(self.conn, "cond_upd")
        self.assertEqual(row["title"], "New title")
        self.assertAlmostEqual(row["volume_24h"], 999.0, places=1)

    def test_defaults_applied_for_missing_fields(self):
        from db import upsert_market_metadata, get_market_metadata
        upsert_market_metadata(self.conn, "cond_min")
        self.conn.commit()
        row = get_market_metadata(self.conn, "cond_min")
        self.assertEqual(row["title"], "")
        self.assertEqual(row["is_open"], 1)
        self.assertAlmostEqual(row["volume_24h"], 0.0, places=1)

    def test_last_synced_ts_set(self):
        import time
        from db import upsert_market_metadata, get_market_metadata
        before = int(time.time()) - 1
        upsert_market_metadata(self.conn, "cond_ts", volume_24h=50.0)
        self.conn.commit()
        row = get_market_metadata(self.conn, "cond_ts")
        self.assertGreaterEqual(row["last_synced_ts"], before)


# ─── 3. bulk_upsert_market_metadata ─────────────────────────────────────────

class TestBulkUpsertMarketMetadata(unittest.TestCase):
    """bulk_upsert_market_metadata handles batches correctly."""

    def setUp(self):
        self.conn = _fresh_conn()

    def tearDown(self):
        self.conn.close()

    def test_empty_list_returns_zero(self):
        from db import bulk_upsert_market_metadata
        result = bulk_upsert_market_metadata(self.conn, [])
        self.assertEqual(result, 0)

    def test_list_without_condition_id_skipped(self):
        from db import bulk_upsert_market_metadata
        result = bulk_upsert_market_metadata(self.conn, [{"title": "no cond"}])
        self.assertEqual(result, 0)

    def test_batch_insert(self):
        from db import bulk_upsert_market_metadata, get_market_metadata
        markets = [
            {"condition_id": f"cond_{i}", "title": f"Market {i}", "volume_24h": float(i * 100)}
            for i in range(5)
        ]
        result = bulk_upsert_market_metadata(self.conn, markets)
        self.assertEqual(result, 5)
        for i in range(5):
            row = get_market_metadata(self.conn, f"cond_{i}")
            self.assertIsNotNone(row)
            self.assertEqual(row["title"], f"Market {i}")

    def test_batch_update_existing(self):
        from db import bulk_upsert_market_metadata, get_market_metadata
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": "cond_bulk", "title": "Old", "volume_24h": 10.0}
        ])
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": "cond_bulk", "title": "New", "volume_24h": 20.0}
        ])
        row = get_market_metadata(self.conn, "cond_bulk")
        self.assertEqual(row["title"], "New")
        self.assertAlmostEqual(row["volume_24h"], 20.0, places=1)

    def test_mixed_valid_and_invalid(self):
        from db import bulk_upsert_market_metadata
        markets = [
            {"condition_id": "cond_valid", "title": "Valid"},
            {"title": "no cond id"},   # should be skipped
            {"condition_id": "", "title": "empty cond"},  # falsy → skipped
        ]
        result = bulk_upsert_market_metadata(self.conn, markets)
        self.assertEqual(result, 1)

    def test_commit_false_does_not_autocommit(self):
        """With commit=False the caller controls the transaction."""
        from db import bulk_upsert_market_metadata, get_market_metadata
        bulk_upsert_market_metadata(
            self.conn,
            [{"condition_id": "cond_nocommit", "title": "pending"}],
            commit=False,
        )
        # Data should be visible within the same connection (same transaction)
        row = get_market_metadata(self.conn, "cond_nocommit")
        self.assertIsNotNone(row)


# ─── 4. get_market_metadata ──────────────────────────────────────────────────

class TestGetMarketMetadata(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_conn()

    def tearDown(self):
        self.conn.close()

    def test_returns_none_for_unknown(self):
        from db import get_market_metadata
        self.assertIsNone(get_market_metadata(self.conn, "does_not_exist"))

    def test_returns_row_after_upsert(self):
        from db import upsert_market_metadata, get_market_metadata
        upsert_market_metadata(self.conn, "cond_get", title="Got it")
        self.conn.commit()
        row = get_market_metadata(self.conn, "cond_get")
        self.assertEqual(row["condition_id"], "cond_get")
        self.assertEqual(row["title"], "Got it")


# ─── 5. META_REFRESH_INTERVAL_SECONDS env var ─────────────────────────────────

class TestMetaRefreshIntervalEnvVar(unittest.TestCase):
    """META_REFRESH_INTERVAL_SECONDS is read at module load time from env."""

    def test_default_is_3600(self):
        import stream_low_buys
        # Default value — may be overridden by env in CI
        val = int(os.getenv("META_REFRESH_INTERVAL_SECONDS", "3600"))
        self.assertEqual(stream_low_buys.META_REFRESH_INTERVAL_SECONDS, val)

    def test_module_has_constant(self):
        import stream_low_buys
        self.assertTrue(hasattr(stream_low_buys, "META_REFRESH_INTERVAL_SECONDS"))
        self.assertIsInstance(stream_low_buys.META_REFRESH_INTERVAL_SECONDS, int)


class TestLoadSubscriptionsPersistsMetadata(unittest.IsolatedAsyncioTestCase):
    """load_subscriptions should persist market metadata snapshot into DB."""

    async def test_startup_snapshot_calls_bulk_upsert(self):
        import stream_low_buys as slb

        fake_assets = ["asset_1"]
        fake_a2c = {"asset_1": "cond_1"}
        fake_meta = {
            "cond_1": {
                "title": "Will X happen?",
                "slug": "will-x-happen",
                "event_slug": "event-x",
                "volume_24h": "1234.56",
                "end_date_iso": "2026-06-01T00:00:00Z",
            }
        }

        with patch.object(slb, "get_connection", return_value=MagicMock()), \
             patch.object(slb, "init_db"), \
             patch.object(slb, "init_discovery_schema"), \
             patch.object(slb, "build_session", return_value=MagicMock()), \
             patch.object(slb, "fetch_market_index", return_value=(fake_assets, fake_a2c, fake_meta)), \
             patch.object(slb, "fetch_event_tags_by_condition", return_value={}), \
             patch.object(slb, "bulk_upsert_market_tags", return_value=0), \
             patch.object(slb, "bulk_upsert_market_metadata", return_value=1) as mock_bulk:
            ingestor = slb.LowPriceIngestor()
            chunks = await ingestor.load_subscriptions()

        self.assertEqual(chunks, [["asset_1"]])
        self.assertEqual(ingestor.asset_to_condition["asset_1"], "cond_1")
        self.assertIn("cond_1", ingestor.condition_meta)
        mock_bulk.assert_called_once()
        rows_arg = mock_bulk.call_args.args[1]
        self.assertEqual(len(rows_arg), 1)
        self.assertEqual(rows_arg[0]["condition_id"], "cond_1")
        self.assertEqual(rows_arg[0]["is_open"], 1)


# ─── 6. _metadata_refresh_task disabled when interval=0 ──────────────────────

class TestMetadataRefreshTaskDisabled(unittest.IsolatedAsyncioTestCase):
    """When META_REFRESH_INTERVAL_SECONDS=0, the task exits immediately."""

    async def test_exits_immediately_when_disabled(self):
        import stream_low_buys as slb

        original = slb.META_REFRESH_INTERVAL_SECONDS
        slb.META_REFRESH_INTERVAL_SECONDS = 0
        try:
            # Build a minimal ingestor with mocked DB
            with patch.object(slb, "get_connection", return_value=MagicMock()), \
                 patch.object(slb, "init_db"), \
                 patch.object(slb, "init_discovery_schema"), \
                 patch.object(slb, "build_session", return_value=MagicMock()):
                ingestor = slb.LowPriceIngestor()
                # Should complete without sleeping (interval=0 → early return)
                await asyncio.wait_for(ingestor._metadata_refresh_task(), timeout=1.0)
        finally:
            slb.META_REFRESH_INTERVAL_SECONDS = original


# ─── 7. _listener_tasks populated by run() ──────────────────────────────────

class TestListenerTasksInitialized(unittest.TestCase):
    """LowPriceIngestor.__init__ initialises _listener_tasks as empty list."""

    def test_listener_tasks_starts_empty(self):
        import stream_low_buys as slb
        with patch.object(slb, "get_connection", return_value=MagicMock()), \
             patch.object(slb, "init_db"), \
             patch.object(slb, "init_discovery_schema"), \
             patch.object(slb, "build_session", return_value=MagicMock()):
            ingestor = slb.LowPriceIngestor()
            self.assertIsInstance(ingestor._listener_tasks, list)
            self.assertEqual(len(ingestor._listener_tasks), 0)

    def test_last_meta_refresh_ts_starts_zero(self):
        import stream_low_buys as slb
        with patch.object(slb, "get_connection", return_value=MagicMock()), \
             patch.object(slb, "init_db"), \
             patch.object(slb, "init_discovery_schema"), \
             patch.object(slb, "build_session", return_value=MagicMock()):
            ingestor = slb.LowPriceIngestor()
            self.assertEqual(ingestor._last_meta_refresh_ts, 0.0)


class TestListenerRebalancePlanning(unittest.TestCase):
    """Listener rebalance should trigger before refresh-spawned tasks grow forever."""

    def _make_ingestor(self):
        import stream_low_buys as slb

        with patch.object(slb, "get_connection", return_value=MagicMock()), \
             patch.object(slb, "init_db"), \
             patch.object(slb, "init_discovery_schema"), \
             patch.object(slb, "build_session", return_value=MagicMock()):
            return slb.LowPriceIngestor()

    def test_rebalance_when_listener_is_mostly_dead_assets(self):
        ingestor = self._make_ingestor()
        task = MagicMock()
        task.done.return_value = False
        ingestor._listener_tasks = [task]
        ingestor._listener_task_assets = {task: ["asset_live", "asset_dead_1", "asset_dead_2"]}

        required, reason = ingestor._listener_rebalance_required(["asset_live"], [])

        self.assertTrue(required)
        self.assertIn("stale_listeners=", reason)

    def test_rebalance_when_new_assets_arrive_but_listener_count_is_capped(self):
        ingestor = self._make_ingestor()
        task = MagicMock()
        task.done.return_value = False
        ingestor._listener_tasks = [task]
        ingestor._listener_task_assets = {task: ["asset_1"]}

        required, reason = ingestor._listener_rebalance_required(
            ["asset_1", "asset_2"],
            ["asset_2"],
        )

        self.assertTrue(required)
        self.assertIn("fresh_assets_without_capacity", reason)

    def test_prune_listener_tasks_cleans_task_asset_mapping(self):
        ingestor = self._make_ingestor()
        done_task = MagicMock()
        done_task.done.return_value = True
        active_task = MagicMock()
        active_task.done.return_value = False
        ingestor._listener_tasks = [done_task, active_task]
        ingestor._listener_task_assets = {
            done_task: ["dead_asset"],
            active_task: ["live_asset"],
        }

        pruned = ingestor._prune_listener_tasks()

        self.assertEqual(pruned, 1)
        self.assertEqual(ingestor._listener_tasks, [active_task])
        self.assertNotIn(done_task, ingestor._listener_task_assets)
        self.assertIn(active_task, ingestor._listener_task_assets)


class TestMemoryTrimHelpers(unittest.TestCase):
    """Memory trimming should use gc plus libc malloc_trim when available."""

    def test_collect_and_trim_memory_calls_malloc_trim(self):
        import stream_low_buys as slb

        fake_trim = MagicMock(return_value=1)
        with patch.object(slb.gc, "collect", return_value=7), \
             patch.object(slb, "_MALLOC_TRIM", fake_trim):
            collected, trimmed = slb._collect_and_trim_memory()

        self.assertEqual(collected, 7)
        self.assertTrue(trimmed)
        fake_trim.assert_called_once_with(0)

    def test_collect_and_trim_memory_handles_missing_malloc_trim(self):
        import stream_low_buys as slb

        with patch.object(slb.gc, "collect", return_value=3), \
             patch.object(slb, "_MALLOC_TRIM", None):
            collected, trimmed = slb._collect_and_trim_memory()

        self.assertEqual(collected, 3)
        self.assertFalse(trimmed)


# ─── 8. post_deploy_checks.sh contains Phase B section ───────────────────────

class TestPostDeployChecksContainsPhaseB(unittest.TestCase):
    """post_deploy_checks.sh must include the Phase B schema section."""

    def _read_script(self) -> str:
        path = Path(__file__).resolve().parents[1] / "scripts" / "post_deploy_checks.sh"
        return path.read_text(encoding="utf-8")

    def test_phase_b_section_present(self):
        content = self._read_script()
        self.assertIn("Phase B schema hardening", content)

    def test_market_metadata_check_present(self):
        content = self._read_script()
        self.assertIn("market_metadata", content)

    def test_phase_b_indexes_checked(self):
        content = self._read_script()
        for idx in [
            "idx_market_outcomes_resolved_at",
            "idx_low_buy_wallet_ts",
            "idx_ua_class_score",
            "idx_alerts_sent_wallet_type_ts",
        ]:
            self.assertIn(idx, content, f"Missing index check in post_deploy_checks.sh: {idx}")


# ─── 8. bulk_mark_markets_closed ─────────────────────────────────────────────


class TestBulkMarkMarketsClosed(unittest.TestCase):
    """bulk_mark_markets_closed flips is_open=1 rows to is_open=0 in batches."""

    def setUp(self):
        self.conn = _fresh_conn()

    def tearDown(self):
        self.conn.close()

    def test_empty_iterable_returns_zero(self):
        from db import bulk_mark_markets_closed
        self.assertEqual(bulk_mark_markets_closed(self.conn, []), 0)

    def test_flips_is_open_to_zero(self):
        from db import bulk_upsert_market_metadata, bulk_mark_markets_closed, get_market_metadata
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": "cond_a", "title": "A", "is_open": 1},
            {"condition_id": "cond_b", "title": "B", "is_open": 1},
            {"condition_id": "cond_c", "title": "C", "is_open": 1},
        ])
        changed = bulk_mark_markets_closed(self.conn, ["cond_a", "cond_c"])
        self.assertEqual(changed, 2)
        self.assertEqual(get_market_metadata(self.conn, "cond_a")["is_open"], 0)
        self.assertEqual(get_market_metadata(self.conn, "cond_b")["is_open"], 1)
        self.assertEqual(get_market_metadata(self.conn, "cond_c")["is_open"], 0)

    def test_idempotent_skips_already_closed(self):
        """Calling twice on the same set returns 0 the second time."""
        from db import bulk_upsert_market_metadata, bulk_mark_markets_closed
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": "cond_x", "title": "X", "is_open": 1},
        ])
        first = bulk_mark_markets_closed(self.conn, ["cond_x"])
        second = bulk_mark_markets_closed(self.conn, ["cond_x"])
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_ignores_unknown_condition_ids(self):
        from db import bulk_mark_markets_closed
        # Unknown condition_ids just return 0 rows changed
        self.assertEqual(bulk_mark_markets_closed(self.conn, ["not_there"]), 0)

    def test_filters_falsy_condition_ids(self):
        from db import bulk_upsert_market_metadata, bulk_mark_markets_closed
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": "cond_real", "title": "Real", "is_open": 1},
        ])
        # Empty strings and None should be filtered, not raise
        changed = bulk_mark_markets_closed(
            self.conn, ["cond_real", "", None],  # type: ignore[list-item]
        )
        self.assertEqual(changed, 1)

    def test_chunking_preserves_total(self):
        """chunk_size=2 on 5 rows still flips all 5."""
        from db import bulk_upsert_market_metadata, bulk_mark_markets_closed
        cids = [f"cond_chunk_{i}" for i in range(5)]
        bulk_upsert_market_metadata(self.conn, [
            {"condition_id": c, "title": c, "is_open": 1} for c in cids
        ])
        changed = bulk_mark_markets_closed(self.conn, cids, chunk_size=2)
        self.assertEqual(changed, 5)


# ─── 9. meta_refresh persists evictions to SQLite ────────────────────────────


class TestMetaRefreshPersistsEvictions(unittest.IsolatedAsyncioTestCase):
    """Regression for 2026-04-20 stink-bid-tennis incident.

    The streamer's meta_refresh used to evict closed markets from the
    in-memory condition_meta dict at line 1312 but never wrote is_open=0 to
    SQLite. Downstream strategies scanning market_metadata WHERE is_open=1
    kept rediscovering dead markets forever. This test drives one refresh
    cycle end-to-end with a mocked fetch that drops one condition_id, then
    asserts the evicted row lands is_open=0 in SQLite.
    """

    async def test_evicted_markets_get_is_open_zero_in_sqlite(self):
        import stream_low_buys as slb
        from db import bulk_upsert_market_metadata, get_market_metadata

        conn = _fresh_conn()
        # Seed SQLite: two markets are open; one will be evicted on refresh
        bulk_upsert_market_metadata(conn, [
            {"condition_id": "cond_live", "title": "Live market", "is_open": 1},
            {"condition_id": "cond_dead", "title": "Stale market", "is_open": 1},
        ])

        # Set interval to 0.1s so the task's "initial wait" is negligible;
        # we'll cancel it after one cycle.
        original = slb.META_REFRESH_INTERVAL_SECONDS
        slb.META_REFRESH_INTERVAL_SECONDS = 1
        try:
            # Fresh fetch returns only cond_live — cond_dead is evicted
            fake_new_assets = ["asset_live"]
            fake_new_a2c = {"asset_live": "cond_live"}
            fake_new_meta = {
                "cond_live": {
                    "title": "Live market",
                    "slug": "live",
                    "event_slug": "event-live",
                    "volume_24h": "1000",
                    "end_date_iso": "2026-12-31T00:00:00Z",
                }
            }

            with patch.object(slb, "get_connection", return_value=conn), \
                 patch.object(slb, "init_db"), \
                 patch.object(slb, "init_discovery_schema"), \
                 patch.object(slb, "build_session", return_value=MagicMock()):
                ingestor = slb.LowPriceIngestor()
                # Pre-populate in-memory dict so evicted_cids is non-empty
                ingestor.condition_meta = {
                    "cond_live": {"title": "Live market"},
                    "cond_dead": {"title": "Stale market"},
                }
                ingestor.asset_to_condition = {
                    "asset_live": "cond_live",
                    "asset_dead": "cond_dead",
                }

                with patch.object(
                    slb, "fetch_market_index",
                    return_value=(fake_new_assets, fake_new_a2c, fake_new_meta),
                ), patch.object(slb, "fetch_event_tags_by_condition", return_value={}), \
                     patch.object(slb, "bulk_upsert_market_tags", return_value=0), \
                     patch.object(ingestor, "_flush_stream_heartbeat"), \
                     patch.object(ingestor, "_prune_listener_tasks", return_value=0), \
                     patch.object(
                         ingestor, "_listener_rebalance_required",
                         return_value=(False, ""),
                     ):
                    # Run one iteration of the loop by calling the task and
                    # stopping after first pass via stop_event
                    async def _stop_after_first_cycle():
                        # Allow the task enough time to complete one refresh,
                        # then signal stop so the outer while-loop exits.
                        await asyncio.sleep(3.0)
                        ingestor.stop_event.set()

                    stop_task = asyncio.create_task(_stop_after_first_cycle())
                    try:
                        await asyncio.wait_for(
                            ingestor._metadata_refresh_task(),
                            timeout=10.0,
                        )
                    finally:
                        stop_task.cancel()

            # Assert: cond_dead was closed in SQLite, cond_live still open
            dead = get_market_metadata(conn, "cond_dead")
            live = get_market_metadata(conn, "cond_live")
            self.assertEqual(
                dead["is_open"], 0,
                "evicted market must be persisted as is_open=0 in market_metadata",
            )
            self.assertEqual(
                live["is_open"], 1,
                "live market must remain is_open=1",
            )
        finally:
            slb.META_REFRESH_INTERVAL_SECONDS = original
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
