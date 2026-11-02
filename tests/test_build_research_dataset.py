import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.build_research_dataset import compute_s1_state


DAY_MS = 24 * 3600 * 1000


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE hl_oi_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,
            open_interest REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'coinalyze',
            oi_units TEXT NOT NULL,
            source_detail TEXT NOT NULL,
            UNIQUE(asset, timestamp_ms)
        )
    """)
    return conn


class TestBuildResearchDataset(unittest.TestCase):
    def test_compute_s1_uses_only_usd_notional_rows(self):
        conn = _conn()
        anchor_ms = 4 * DAY_MS

        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 2 * DAY_MS, 100.0, "coinalyze", "usd_notional", "coinalyze_open_interest_history"))
        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 3 * DAY_MS, 130.0, "coinalyze", "usd_notional", "coinalyze_open_interest_history"))
        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 3 * DAY_MS - 3600000, 9999.0, "coinalyze", "asset_units", "coinalyze_open_interest_history"))
        conn.commit()

        self.assertEqual(compute_s1_state(conn, "BTC", anchor_ms), "oi_rising")

    def test_compute_s1_rejects_cross_source_splice(self):
        conn = _conn()
        anchor_ms = 4 * DAY_MS

        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 2 * DAY_MS, 100.0, "coinalyze", "usd_notional", "coinalyze_open_interest_history"))
        conn.execute("""
            INSERT INTO hl_oi_history
                (asset, timestamp_ms, open_interest, source, oi_units, source_detail)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("BTC", 3 * DAY_MS, 130.0, "snapshots_fallback", "usd_notional", "hyperliquid_snapshots_last_daily_reading"))
        conn.commit()

        self.assertIsNone(compute_s1_state(conn, "BTC", anchor_ms))


if __name__ == "__main__":
    unittest.main()
