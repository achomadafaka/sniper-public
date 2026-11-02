import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from db import init_db, upsert_followed_position
from sniper.services.sync import SyncConfig, _derive_entry_notional, sync_followed_positions_wallet


class SyncCostBasisTests(unittest.TestCase):
    def test_uses_entry_price_times_size_as_primary(self) -> None:
        """avgPrice * size is the primary cost-basis formula per ADR-004."""
        row = {
            "initialValue": 1250.5,
            "totalBought": 900000.0,
            "conditionId": "cond-1",
        }
        value = _derive_entry_notional(row, entry_price=0.25, current_size=5000.0)
        # 0.25 * 5000 = 1250.0, NOT initialValue (1250.5)
        self.assertAlmostEqual(value, 1250.0, places=6)

    def test_falls_back_to_initial_value_when_entry_price_missing(self) -> None:
        """When avgPrice is 0/missing, initialValue is the fallback."""
        row = {
            "initialValue": 42.0,
            "totalBought": 250000.0,
            "conditionId": "cond-2",
        }
        value = _derive_entry_notional(row, entry_price=0.0, current_size=100.0)
        self.assertAlmostEqual(value, 42.0, places=6)

    def test_returns_zero_when_nothing_available(self) -> None:
        row = {
            "initialValue": 0,
            "totalBought": 500.0,
            "conditionId": "cond-3",
        }
        value = _derive_entry_notional(row, entry_price=0.0, current_size=0.0)
        self.assertAlmostEqual(value, 0.0, places=6)

    def test_ignores_total_bought_entirely(self) -> None:
        """totalBought is gross cumulative flow and must never be used."""
        row = {
            "initialValue": 0,
            "totalBought": 749315.711752,
            "conditionId": "cond-4",
        }
        value = _derive_entry_notional(row, entry_price=0.083838, current_size=110315.711752)
        expected = 0.083838 * 110315.711752
        self.assertAlmostEqual(value, expected, places=4)


class SyncFollowedPositionPruneTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.config = SyncConfig(
            watchlist_trade_fetch_limit=300,
            watchlist_sync_min_interval_seconds=30,
            watchlist_max_sync_wallets_per_request=6,
            max_followed_ui_trades=500,
            followed_trade_sync_min_interval_seconds=45,
            followed_max_sync_wallets_per_request=6,
            followed_position_sync_min_interval_seconds=60,
            followed_position_max_sync_wallets_per_request=6,
            max_followed_position_ui_rows=1000,
            position_min_open_value=0.0,
            position_settlement_price_eps=0.001,
            position_settlement_end_grace_seconds=21600,
        )

    def tearDown(self) -> None:
        self.conn.close()
        os.remove(self.db_path)

    def test_sync_removes_positions_missing_from_live_snapshot(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        upsert_followed_position(
            self.conn,
            wallet=wallet,
            condition_id="cond-live",
            outcome="yes",
            title="Still open",
            added_source="manual",
            entry_price=0.10,
            current_price=0.20,
            entry_notional=10.0,
            current_amount=20.0,
            current_size=100.0,
        )
        upsert_followed_position(
            self.conn,
            wallet=wallet,
            condition_id="cond-stale",
            outcome="yes",
            title="Should disappear",
            added_source="manual",
            entry_price=0.99,
            current_price=0.01,
            entry_notional=999.0,
            current_amount=10.0,
            current_size=1000.0,
        )

        live_rows = [
            {
                "conditionId": "cond-live",
                "outcome": "Yes",
                "title": "Still open",
                "eventSlug": "still-open",
                "size": 150.0,
                "avgPrice": 0.40,
                "curPrice": 0.55,
                "currentValue": 82.5,
            }
        ]

        with patch("sniper.services.sync.fetch_user_positions", return_value=live_rows):
            sync_followed_positions_wallet(
                wallet,
                config=self.config,
                force=True,
                conn_obj=self.conn,
                http_session=object(),
                auto_scope_wallets=set(),
            )

        rows = self.conn.execute(
            """
            SELECT condition_id, entry_price, current_size, entry_notional
            FROM followed_positions
            WHERE wallet = ?
            ORDER BY condition_id
            """,
            (wallet,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["condition_id"]), "cond-live")
        self.assertAlmostEqual(float(rows[0]["entry_price"]), 0.40, places=6)
        self.assertAlmostEqual(float(rows[0]["current_size"]), 150.0, places=6)
        self.assertAlmostEqual(float(rows[0]["entry_notional"]), 60.0, places=6)


if __name__ == "__main__":
    unittest.main()
