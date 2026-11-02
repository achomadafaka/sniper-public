import os
import tempfile
import unittest

from db import (
    ensure_wallet_directory_schema,
    auto_follow_scope_wallets,
    followed_wallet_activity,
    followed_wallet_research_scope_wallets,
    get_followed_wallets,
    get_connection,
    init_db,
    mark_watchlist_wallet_synced,
    remove_watchlist_wallet,
    set_followed_wallet_status,
    strategy_research_notes,
    upsert_followed_wallet_record,
    upsert_followed_wallet_activity,
    upsert_strategy_research_note,
    upsert_user_analysis,
    watchlist_wallets,
    upsert_watchlist_wallet,
)
from db import upsert_position


class DbCoreExtractionTests(unittest.TestCase):
    def test_db_core_exports_connection_helpers_reexported_by_db(self) -> None:
        from sniper.persistence import db_core
        import db

        self.assertIs(db.get_db_path, db_core.get_db_path)
        self.assertIs(db.get_connection, db_core.get_connection)
        self.assertIs(db.connection_scope, db_core.connection_scope)
        self.assertIs(db.get_readonly_connection, db_core.get_readonly_connection)
        self.assertIs(db.with_db_init_lock, db_core.with_db_init_lock)

    def test_writable_connections_cap_wal_recycle_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn = get_connection(os.path.join(tmpdir, "wal-cap.db"))
            try:
                limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
            finally:
                conn.close()

        # Lowered 64MiB -> 16MiB on 2026-07-29: at 64MiB this WAL parked at exactly 64MiB while
        # holding 717KB of live frames. See tests/test_wal_pinning.py for the mechanism.
        self.assertEqual(limit, 16 * 1024 * 1024)


class DbFollowedExtractionTests(unittest.TestCase):
    def test_db_followed_exports_wallet_directory_helpers_reexported_by_db(self) -> None:
        from sniper.persistence import db_followed
        import db

        self.assertIs(db.upsert_wallet_registry, db_followed.upsert_wallet_registry)
        self.assertIs(db.upsert_wallet_membership, db_followed.upsert_wallet_membership)
        self.assertIs(db.deactivate_wallet_membership, db_followed.deactivate_wallet_membership)
        self.assertIs(db.upsert_wallet_tag, db_followed.upsert_wallet_tag)
        self.assertIs(db.deactivate_wallet_tag, db_followed.deactivate_wallet_tag)
        self.assertIs(db.replace_wallet_tags, db_followed.replace_wallet_tags)
        self.assertIs(db.ensure_wallet_directory_schema, db_followed.ensure_wallet_directory_schema)


class DbMarketsExtractionTests(unittest.TestCase):
    def test_db_markets_exports_market_helpers_reexported_by_db(self) -> None:
        from sniper.persistence import db_markets
        import db

        self.assertIs(db.redemption_success_exists, db_markets.redemption_success_exists)
        self.assertIs(db.latest_redemption_success, db_markets.latest_redemption_success)
        self.assertIs(db.record_redemption_attempt, db_markets.record_redemption_attempt)
        self.assertIs(db.upsert_market_metadata, db_markets.upsert_market_metadata)
        self.assertIs(db.get_market_metadata, db_markets.get_market_metadata)
        self.assertIs(db.bulk_upsert_market_metadata, db_markets.bulk_upsert_market_metadata)
        self.assertIs(db.bulk_upsert_market_tags, db_markets.bulk_upsert_market_tags)
        self.assertIs(db.fetch_market_tags_for_cids, db_markets.fetch_market_tags_for_cids)
        self.assertIs(db.fetch_tag_nav_category_map, db_markets.fetch_tag_nav_category_map)
        self.assertIs(db.bulk_mark_markets_closed, db_markets.bulk_mark_markets_closed)
        self.assertIs(db.upsert_market_outcome, db_markets.upsert_market_outcome)
        self.assertIs(db.get_market_outcome, db_markets.get_market_outcome)
        self.assertIs(db.record_low_buy_trade, db_markets.record_low_buy_trade)
        self.assertIs(db.upsert_position, db_markets.upsert_position)
        self.assertIs(db.wallets_for_open_position_scan, db_markets.wallets_for_open_position_scan)


class DbMigrationTests(unittest.TestCase):
    def _new_db(self) -> tuple[str, object]:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = get_connection(path)
        return path, conn

    def test_init_db_fresh_schema_contains_expected_columns(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(user_analysis)").fetchall()}
            self.assertIn("insider_score", cols)
            self.assertIn("speculator_score", cols)
            self.assertIn("classification", cols)
            profile_cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_profiles)").fetchall()}
            self.assertIn("profile_joined_ts", profile_cols)
            self.assertIn("profile_prediction_count", profile_cols)
        finally:
            conn.close()
            os.remove(path)

    def test_init_db_upgrades_existing_table_with_migrations(self) -> None:
        path, conn = self._new_db()
        try:
            conn.execute(
                """
                CREATE TABLE low_buy_trades (
                    transaction_hash TEXT PRIMARY KEY,
                    proxy_wallet TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'BUY',
                    price REAL NOT NULL DEFAULT 0,
                    size REAL,
                    timestamp INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
            init_db(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(low_buy_trades)").fetchall()}
            self.assertIn("notional", cols)
            self.assertIn("trigger_type", cols)
            self.assertIn("trigger_reason", cols)
        finally:
            conn.close()
            os.remove(path)

    def test_init_db_creates_positions_table(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
            self.assertIn("wallet", cols)
            self.assertIn("condition_id", cols)
            self.assertIn("outcome", cols)
            self.assertIn("total_shares", cols)
            self.assertIn("avg_entry_price", cols)
            self.assertIn("total_notional", cols)
            self.assertIn("trade_count", cols)
            self.assertIn("first_trade_ts", cols)
            self.assertIn("last_trade_ts", cols)
            self.assertIn("is_open", cols)
            self.assertIn("trigger_type", cols)
        finally:
            conn.close()
            os.remove(path)

    def test_init_db_creates_research_tables(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            strategy_cols = {row[1] for row in conn.execute("PRAGMA table_info(strategy_research)").fetchall()}
            activity_cols = {row[1] for row in conn.execute("PRAGMA table_info(followed_wallet_activity)").fetchall()}
            followed_cols = {row[1] for row in conn.execute("PRAGMA table_info(followed_wallets)").fetchall()}
            registry_cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_registry)").fetchall()}
            membership_cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_memberships)").fetchall()}
            tag_cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_tags)").fetchall()}
            self.assertIn("edge_summary", strategy_cols)
            self.assertIn("raw_json", activity_cols)
            self.assertIn("status", followed_cols)
            self.assertIn("copy_status", registry_cols)
            self.assertIn("scope", membership_cols)
            self.assertIn("tag_type", tag_cols)
        finally:
            conn.close()
            os.remove(path)

    def test_init_db_creates_paper_limit_tables_when_schema_gate_reports_current(self) -> None:
        """Regression: paper_limit_* tables must be created even when the
        _core_schema_is_current gate reports the schema as current.

        Before the fix, init_db() early-returned when user_version matched
        CORE_SCHEMA_VERSION and the sentinel tables existed, skipping the
        idempotent ensure_paper_limit_order_tables() call. DBs that existed
        before paper_limit_* was added (all production deployments) never
        got these tables.
        """
        path, conn = self._new_db()
        try:
            # Bring the DB to schema-current by running init_db once, then
            # simulate a pre-paper_limit DB by dropping the three tables.
            init_db(conn)
            conn.execute("DROP TABLE paper_limit_fills")
            conn.execute("DROP TABLE paper_limit_events")
            conn.execute("DROP TABLE paper_limit_orders")
            conn.commit()

            self.assertFalse(self._table_exists(conn, "paper_limit_orders"))
            self.assertFalse(self._table_exists(conn, "paper_limit_fills"))
            self.assertFalse(self._table_exists(conn, "paper_limit_events"))
            # Gate still reports schema current → pre-fix init_db would early-return.
            from db import _core_schema_is_current
            self.assertTrue(_core_schema_is_current(conn))

            init_db(conn)

            self.assertTrue(self._table_exists(conn, "paper_limit_orders"))
            self.assertTrue(self._table_exists(conn, "paper_limit_fills"))
            self.assertTrue(self._table_exists(conn, "paper_limit_events"))
        finally:
            conn.close()
            os.remove(path)

    @staticmethod
    def _table_exists(conn, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def test_watchlist_wallet_dual_writes_wallet_directory(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            wallet = "0x1111111111111111111111111111111111111111"
            upsert_watchlist_wallet(conn, wallet, "Scout")
            mark_watchlist_wallet_synced(conn, wallet, synced_ts=1773000000)

            registry = conn.execute(
                "SELECT display_label, copy_status FROM wallet_registry WHERE wallet = ?",
                (wallet,),
            ).fetchone()
            membership = conn.execute(
                "SELECT status, label, metadata_json FROM wallet_memberships WHERE wallet = ? AND scope = 'watchlist'",
                (wallet,),
            ).fetchone()
            tag = conn.execute(
                "SELECT active FROM wallet_tags WHERE wallet = ? AND tag = 'watchlist' AND tag_type = 'scope'",
                (wallet,),
            ).fetchone()

            self.assertEqual(registry["display_label"], "Scout")
            self.assertEqual(registry["copy_status"], "not_followed")
            self.assertEqual(membership["status"], "active")
            self.assertEqual(membership["label"], "Scout")
            self.assertIn('"last_synced_ts": 1773000000', membership["metadata_json"])
            self.assertEqual(tag["active"], 1)

            remove_watchlist_wallet(conn, wallet)
            removed = conn.execute(
                "SELECT status FROM wallet_memberships WHERE wallet = ? AND scope = 'watchlist'",
                (wallet,),
            ).fetchone()
            removed_tag = conn.execute(
                "SELECT active FROM wallet_tags WHERE wallet = ? AND tag = 'watchlist' AND tag_type = 'scope'",
                (wallet,),
            ).fetchone()
            self.assertEqual(removed["status"], "removed")
            self.assertEqual(removed_tag["active"], 0)
        finally:
            conn.close()
            os.remove(path)

    def test_followed_wallet_dual_writes_wallet_directory(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            wallet = "0x2222222222222222222222222222222222222222"
            upsert_followed_wallet_record(
                conn,
                wallet=wallet,
                label="Copy Me",
                reason="manual review",
                status="active",
                total_positions=2,
                total_pnl_usdc=125.5,
            )
            conn.commit()

            registry = conn.execute(
                "SELECT display_label, copy_status FROM wallet_registry WHERE wallet = ?",
                (wallet,),
            ).fetchone()
            membership = conn.execute(
                "SELECT status, reason, metadata_json FROM wallet_memberships WHERE wallet = ? AND scope = 'copy_execution'",
                (wallet,),
            ).fetchone()
            tag = conn.execute(
                "SELECT active FROM wallet_tags WHERE wallet = ? AND tag = 'copy_execution' AND tag_type = 'scope'",
                (wallet,),
            ).fetchone()

            self.assertEqual(registry["display_label"], "Copy Me")
            self.assertEqual(registry["copy_status"], "active")
            self.assertEqual(membership["status"], "active")
            self.assertEqual(membership["reason"], "manual review")
            self.assertIn('"total_positions": 2', membership["metadata_json"])
            self.assertEqual(tag["active"], 1)

            set_followed_wallet_status(conn, wallet=wallet, status="unfollowed")
            conn.commit()
            updated = conn.execute(
                "SELECT copy_status FROM wallet_registry WHERE wallet = ?",
                (wallet,),
            ).fetchone()
            updated_membership = conn.execute(
                "SELECT status FROM wallet_memberships WHERE wallet = ? AND scope = 'copy_execution'",
                (wallet,),
            ).fetchone()
            updated_tag = conn.execute(
                "SELECT active FROM wallet_tags WHERE wallet = ? AND tag = 'copy_execution' AND tag_type = 'scope'",
                (wallet,),
            ).fetchone()
            self.assertEqual(updated["copy_status"], "inactive")
            self.assertEqual(updated_membership["status"], "unfollowed")
            self.assertEqual(updated_tag["active"], 0)
        finally:
            conn.close()
            os.remove(path)

    def test_wallet_directory_backfills_legacy_rows(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            wallet = "0x3333333333333333333333333333333333333333"
            conn.execute(
                "INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts) VALUES (?, ?, ?, ?)",
                (wallet, "Legacy Watch", 100, 200),
            )
            conn.execute(
                """
                INSERT INTO followed_wallets (
                    wallet, label, added_reason, status, total_positions, total_pnl_usdc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (wallet, "Legacy Copy", "legacy", "active", 4, 88.0),
            )
            conn.execute(
                "UPDATE followed_wallets SET label = ? WHERE wallet = ?",
                ("Legacy Copy", wallet),
            )
            upsert_user_analysis(
                conn,
                wallet=wallet,
                analyzed_ts=123456,
                position_count=0,
                pnl=0.0,
                max_position_value=0.0,
                top_open_position_title="",
                top_open_position_value=0.0,
                total_open_value=0.0,
                trade_count=0,
                low_buy_hits_14d=0,
                avg_low_buy_notional_14d=0.0,
                market_diversity_14d=0,
                concentration_ratio=0.0,
                suspicion_score=0.0,
                insider_score=0.0,
                speculator_score=0.0,
                classification="high_winrate_review",
                tier="watch",
                strategy_group="balanced_mixed",
                strategy_confidence=0.0,
                strategy_note="",
                wins=0,
                losses=0,
                closed_sample_size=0,
                win_rate=0.0,
                high_winrate_qualified=False,
                first_activity_ts=0,
                account_age_days=0.0,
                is_fresh_account=False,
                matches_criteria=False,
                reason="legacy analysis",
                commit=False,
            )
            conn.commit()

            ensure_wallet_directory_schema(conn)

            registry = conn.execute(
                "SELECT display_label, copy_status, analysis_status, classification FROM wallet_registry WHERE wallet = ?",
                (wallet,),
            ).fetchone()
            self.assertIn(registry["display_label"], {"Legacy Watch", "Legacy Copy"})
            self.assertEqual(registry["copy_status"], "active")
            self.assertEqual(registry["analysis_status"], "analyzed")
            self.assertEqual(registry["classification"], "high_winrate_review")
        finally:
            conn.close()
            os.remove(path)

    def test_read_helpers_delegate_to_wallet_memberships(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            watch_wallet = "0x4444444444444444444444444444444444444444"
            copy_wallet = "0x5555555555555555555555555555555555555555"
            upsert_watchlist_wallet(conn, watch_wallet, "Watch Me")
            mark_watchlist_wallet_synced(conn, watch_wallet, synced_ts=1773000100)
            upsert_followed_wallet_record(
                conn,
                wallet=copy_wallet,
                label="Copy Me",
                reason="manual",
                status="active",
                total_positions=3,
                total_pnl_usdc=42.0,
                added_at="2026-03-25 12:00:00",
                last_copied_at="2026-03-25 13:00:00",
            )
            conn.commit()

            conn.execute("DELETE FROM watchlist_wallets WHERE wallet = ?", (watch_wallet,))
            conn.execute("DELETE FROM followed_wallets WHERE wallet = ?", (copy_wallet,))
            conn.commit()

            watch_rows = watchlist_wallets(conn)
            old_db_path = os.environ.get("DB_PATH")
            os.environ["DB_PATH"] = path
            try:
                followed_rows = get_followed_wallets(status="active")
            finally:
                if old_db_path is None:
                    os.environ.pop("DB_PATH", None)
                else:
                    os.environ["DB_PATH"] = old_db_path
            research_scope = set(followed_wallet_research_scope_wallets(conn))
            auto_scope = set(auto_follow_scope_wallets(conn))

            self.assertEqual([row["wallet"] for row in watch_rows], [watch_wallet])
            self.assertEqual(watch_rows[0]["label"], "Watch Me")
            self.assertEqual(int(watch_rows[0]["last_synced_ts"]), 1773000100)

            self.assertEqual([row["wallet"] for row in followed_rows], [copy_wallet])
            self.assertEqual(followed_rows[0]["label"], "Copy Me")
            self.assertEqual(int(followed_rows[0]["total_positions"]), 3)
            self.assertAlmostEqual(float(followed_rows[0]["total_pnl_usdc"]), 42.0, places=6)
            self.assertEqual(followed_rows[0]["last_copied_at"], "2026-03-25 13:00:00")

            self.assertIn(watch_wallet, research_scope)
            self.assertIn(copy_wallet, research_scope)
            self.assertIn(watch_wallet, auto_scope)
            self.assertNotIn(copy_wallet, auto_scope)
        finally:
            conn.close()
            os.remove(path)

    def test_strategy_research_note_upserts_and_lists(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            upsert_strategy_research_note(
                conn,
                slug="paper-edge",
                title="Paper Edge",
                category="paper_entry",
                edge_summary="Initial summary",
                tags=["paper", "edge"],
                sample_size=3,
            )
            upsert_strategy_research_note(
                conn,
                slug="paper-edge",
                title="Paper Edge Updated",
                category="paper_entry",
                edge_summary="Updated summary",
                tags=["paper", "updated"],
                sample_size=5,
            )
            rows = strategy_research_notes(conn, status="active", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Paper Edge Updated")
            self.assertEqual(rows[0]["sample_size"], 5)
            self.assertIn("updated", rows[0]["tags_json"])
        finally:
            conn.close()
            os.remove(path)

    def test_followed_wallet_activity_upsert_is_idempotent_and_scope_includes_followed_wallets(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            conn.execute(
                """
                INSERT INTO followed_wallets (wallet, label, status)
                VALUES ('0x1111111111111111111111111111111111111111', 'Test Wallet', 'active')
                """
            )
            conn.commit()
            row = {
                "transactionHash": "0xabc123",
                "conditionId": "cond-1",
                "type": "TRADE",
                "side": "BUY",
                "outcome": "Yes",
                "title": "Test Market",
                "slug": "test-market",
                "eventSlug": "event-a",
                "price": 0.42,
                "size": 50,
                "usdcSize": 21,
                "timestamp": 1772800000,
            }
            saved1 = upsert_followed_wallet_activity(
                conn,
                wallet="0x1111111111111111111111111111111111111111",
                activities=[row],
            )
            saved2 = upsert_followed_wallet_activity(
                conn,
                wallet="0x1111111111111111111111111111111111111111",
                activities=[row],
            )
            rows = followed_wallet_activity(conn, wallet="0x1111111111111111111111111111111111111111", limit=10)
            scope = followed_wallet_research_scope_wallets(conn)
            self.assertEqual(saved1, 1)
            self.assertEqual(saved2, 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["activity_type"], "TRADE")
            self.assertIn("0x1111111111111111111111111111111111111111", scope)
        finally:
            conn.close()
            os.remove(path)

    def test_upsert_position_aggregates_repeated_fills(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            upsert_position(
                conn,
                {
                    "wallet": "0x1111111111111111111111111111111111111111",
                    "condition_id": "cond-1",
                    "outcome": "Yes",
                    "price": 0.20,
                    "size": 100,
                    "timestamp": 1000,
                    "trigger_type": "low_price_entry",
                },
                market_meta={
                    "current_price": 0.21,
                    "end_date_iso": "2026-03-10T00:00:00Z",
                    "category": "politics",
                },
            )
            upsert_position(
                conn,
                {
                    "wallet": "0x1111111111111111111111111111111111111111",
                    "condition_id": "cond-1",
                    "outcome": "yes",
                    "price": 0.40,
                    "size": 50,
                    "timestamp": 1200,
                    "trigger_type": "large_bet",
                },
                market_meta={
                    "current_price": 0.35,
                    "end_date_iso": "2026-03-11T00:00:00Z",
                    "category": "politics",
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM positions WHERE wallet = ? AND condition_id = ? AND outcome = ?",
                ("0x1111111111111111111111111111111111111111", "cond-1", "yes"),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row["total_shares"], 150.0, places=6)
            self.assertAlmostEqual(row["total_notional"], 40.0, places=6)
            self.assertAlmostEqual(row["avg_entry_price"], 40.0 / 150.0, places=6)
            self.assertEqual(row["trade_count"], 2)
            self.assertEqual(row["first_trade_ts"], 1000)
            self.assertEqual(row["last_trade_ts"], 1200)
            self.assertAlmostEqual(row["current_price"], 0.35, places=6)
            self.assertEqual(row["market_end_date_iso"], "2026-03-11T00:00:00Z")
            self.assertEqual(row["market_category"], "politics")
            self.assertEqual(row["trigger_type"], "large_bet")
            self.assertEqual(row["is_open"], 1)
        finally:
            conn.close()
            os.remove(path)

    def test_upsert_position_preserves_market_meta_when_not_provided(self) -> None:
        path, conn = self._new_db()
        try:
            init_db(conn)
            upsert_position(
                conn,
                {
                    "wallet": "0x2222222222222222222222222222222222222222",
                    "condition_id": "cond-2",
                    "outcome": "no",
                    "price": 0.30,
                    "size": 20,
                    "timestamp": 2000,
                    "trigger_type": "low_price_entry",
                },
                market_meta={
                    "current_price": 0.31,
                    "end_date_iso": "2026-03-15T00:00:00Z",
                    "category": "sports",
                },
            )
            upsert_position(
                conn,
                {
                    "wallet": "0x2222222222222222222222222222222222222222",
                    "condition_id": "cond-2",
                    "outcome": "no",
                    "price": 0.25,
                    "size": 20,
                    "timestamp": 2100,
                    "trigger_type": "low_price_entry",
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT current_price, market_end_date_iso, market_category FROM positions "
                "WHERE wallet = ? AND condition_id = ? AND outcome = ?",
                ("0x2222222222222222222222222222222222222222", "cond-2", "no"),
            ).fetchone()
            self.assertAlmostEqual(row["current_price"], 0.31, places=6)
            self.assertEqual(row["market_end_date_iso"], "2026-03-15T00:00:00Z")
            self.assertEqual(row["market_category"], "sports")
        finally:
            conn.close()
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
