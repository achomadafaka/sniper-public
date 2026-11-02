import os
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import backfill_oi_history as oi_backfill


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class TestBackfillOIHistory(unittest.TestCase):
    def test_ensure_table_adds_metadata_and_marks_legacy_rows(self):
        conn = _conn()
        conn.execute("""
            CREATE TABLE hl_oi_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                open_interest REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'coinalyze',
                UNIQUE(asset, timestamp_ms)
            )
        """)
        conn.execute(
            "INSERT INTO hl_oi_history (asset, timestamp_ms, open_interest, source) VALUES (?, ?, ?, ?)",
            ("BTC", 1000, 123.0, "coinalyze"),
        )
        conn.execute(
            "INSERT INTO hl_oi_history (asset, timestamp_ms, open_interest, source) VALUES (?, ?, ?, ?)",
            ("BTC", 2000, 456.0, "snapshots_fallback"),
        )

        oi_backfill.ensure_table(conn)

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(hl_oi_history)")}
        self.assertIn("oi_units", cols)
        self.assertIn("source_detail", cols)

        rows = conn.execute("""
            SELECT source, oi_units, source_detail
            FROM hl_oi_history
            ORDER BY timestamp_ms
        """).fetchall()
        self.assertEqual(rows[0]["oi_units"], "asset_units")
        self.assertEqual(rows[0]["source_detail"], "coinalyze_open_interest_history")
        self.assertEqual(rows[1]["oi_units"], "usd_notional")
        self.assertEqual(rows[1]["source_detail"], "hyperliquid_snapshots_last_daily_reading")

    @patch("scripts.backfill_oi_history.requests.get")
    def test_try_coinalyze_requests_usd_and_upserts(self, mock_get):
        conn = _conn()
        oi_backfill.ensure_table(conn)
        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 1000, 50.0, "coinalyze", "asset_units", "coinalyze_open_interest_history"))
        conn.commit()

        response = Mock()
        response.status_code = 200
        response.raise_for_status = Mock()
        response.json.return_value = [
            {"symbol": "BTCUSDT.6", "history": [{"t": 1, "c": 123.4}]}
        ]
        mock_get.return_value = response

        inserted = oi_backfill.try_coinalyze(conn, "BTC", months=6, api_key="test-key")

        self.assertGreaterEqual(inserted, 1)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["convert_to_usd"], "true")

        row = conn.execute("""
            SELECT open_interest, source, oi_units, source_detail
            FROM hl_oi_history
            WHERE asset = 'BTC' AND timestamp_ms = 1000
        """).fetchone()
        self.assertEqual(row["open_interest"], 123.4)
        self.assertEqual(row["source"], "coinalyze")
        self.assertEqual(row["oi_units"], "usd_notional")
        self.assertEqual(row["source_detail"], "coinalyze_open_interest_history")

    def test_fallback_snapshots_store_usd_notional(self):
        conn = _conn()
        oi_backfill.ensure_table(conn)
        conn.execute("""
            CREATE TABLE hyperliquid_snapshots (
                asset TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                open_interest REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO hyperliquid_snapshots (asset, captured_at, open_interest) VALUES (?, ?, ?)",
            ("BTC", "2026-03-01 03:00:00", 1000.0),
        )
        conn.execute(
            "INSERT INTO hyperliquid_snapshots (asset, captured_at, open_interest) VALUES (?, ?, ?)",
            ("BTC", "2026-03-01 22:00:00", 1200.0),
        )
        conn.commit()

        inserted = oi_backfill.fallback_snapshots(conn, "BTC", months=1)

        self.assertEqual(inserted, 1)
        row = conn.execute("""
            SELECT open_interest, source, oi_units, source_detail
            FROM hl_oi_history
            WHERE asset = 'BTC'
        """).fetchone()
        self.assertEqual(row["open_interest"], 1200.0)
        self.assertEqual(row["source"], "snapshots_fallback")
        self.assertEqual(row["oi_units"], "usd_notional")
        self.assertEqual(row["source_detail"], "hyperliquid_snapshots_last_daily_reading")

    def test_fallback_snapshots_returns_minus_one_without_source_table(self):
        conn = _conn()
        oi_backfill.ensure_table(conn)
        self.assertEqual(oi_backfill.fallback_snapshots(conn, "BTC", months=1), -1)


if __name__ == "__main__":
    unittest.main()
