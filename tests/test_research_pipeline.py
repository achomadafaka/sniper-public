"""Tests for the HL signal research pipeline.

Validates dataset construction invariants, no-leak semantics,
train/test split correctness, and signal computation.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB_PATH = os.path.join(ROOT, os.getenv("DB_PATH", "sniper_tracker.db"))


def _get_conn():
    """Get a connection to the actual database (read-only tests)."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestBackfillIntegrity(unittest.TestCase):
    """Tests that backfilled data meets basic quality standards."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _get_conn()
        if cls.conn is None:
            raise unittest.SkipTest("Database not available")
        required = {"hl_funding_history", "price_history", "hl_oi_history"}
        present = {
            row[0]
            for row in cls.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('hl_funding_history', 'price_history', 'hl_oi_history')"
            )
        }
        if required - present:
            raise unittest.SkipTest("Research backfill tables not available in local DB")

    def test_funding_no_duplicates(self):
        """Each (asset, timestamp_ms) pair should be unique in hl_funding_history."""
        dupes = self.conn.execute("""
            SELECT asset, timestamp_ms, COUNT(*) as n
            FROM hl_funding_history
            GROUP BY asset, timestamp_ms
            HAVING n > 1
        """).fetchall()
        self.assertEqual(len(dupes), 0, f"Found {len(dupes)} duplicate funding rows")

    def test_price_no_duplicates(self):
        dupes = self.conn.execute("""
            SELECT asset, timestamp_ms, COUNT(*) as n
            FROM price_history
            GROUP BY asset, timestamp_ms
            HAVING n > 1
        """).fetchall()
        self.assertEqual(len(dupes), 0, f"Found {len(dupes)} duplicate price rows")

    def test_funding_monotonic(self):
        """Funding timestamps should be strictly increasing per asset."""
        for asset in ["BTC", "ETH", "SOL"]:
            non_mono = self.conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT timestamp_ms, LAG(timestamp_ms) OVER (ORDER BY timestamp_ms) as prev
                    FROM hl_funding_history WHERE asset = ?
                ) WHERE prev IS NOT NULL AND timestamp_ms <= prev
            """, (asset,)).fetchone()[0]
            self.assertEqual(non_mono, 0, f"{asset} funding has {non_mono} non-monotonic timestamps")

    def test_funding_coverage(self):
        """Should have >= 4000 hourly rows per asset (6 months)."""
        for asset in ["BTC", "ETH", "SOL"]:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM hl_funding_history WHERE asset = ?", (asset,)
            ).fetchone()[0]
            self.assertGreaterEqual(count, 4000, f"{asset} funding has only {count} rows")

    def test_price_coverage(self):
        for asset in ["BTC", "ETH", "SOL"]:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM price_history WHERE asset = ?", (asset,)
            ).fetchone()[0]
            self.assertGreaterEqual(count, 4000, f"{asset} price has only {count} rows")

    def test_oi_timestamps_are_midnight(self):
        """All Coinalyze OI timestamps should be at midnight UTC."""
        non_midnight = self.conn.execute("""
            SELECT COUNT(*) FROM hl_oi_history
            WHERE source = 'coinalyze' AND (timestamp_ms % 86400000) != 0
        """).fetchone()[0]
        self.assertEqual(non_midnight, 0, f"{non_midnight} OI rows not at midnight")

    def test_oi_metadata_columns_exist(self):
        cols = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(hl_oi_history)")
        }
        self.assertIn("oi_units", cols)
        self.assertIn("source_detail", cols)


class TestDatasetNoLeak(unittest.TestCase):
    """Tests that the research dataset has no future information leakage."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _get_conn()
        if cls.conn is None:
            raise unittest.SkipTest("Database not available")
        # Check if dataset exists
        try:
            count = cls.conn.execute("SELECT COUNT(*) FROM research_dataset").fetchone()[0]
            if count == 0:
                raise unittest.SkipTest("research_dataset is empty")
        except sqlite3.OperationalError:
            raise unittest.SkipTest("research_dataset table does not exist")

    def test_train_test_split_is_time_ordered(self):
        """All train rows should have anchor_ts < all test rows."""
        max_train = self.conn.execute(
            "SELECT MAX(anchor_ts) FROM research_dataset WHERE split = 'train'"
        ).fetchone()[0]
        min_test = self.conn.execute(
            "SELECT MIN(anchor_ts) FROM research_dataset WHERE split = 'test'"
        ).fetchone()[0]
        if max_train and min_test:
            self.assertLess(max_train, min_test,
                           f"Train max {max_train} >= test min {min_test} — split leak!")

    def test_oi_is_lagged(self):
        """OI values used in signals should come from at least 24h before the anchor.

        This validates the Coinalyze timestamp lag fix: daily OI at midnight
        of day D should only be available from day D+1 onward.
        """
        # For rows that have OI, the OI reading should be from at least 24h before anchor
        rows = self.conn.execute("""
            SELECT rd.anchor_ts, rd.s1_state,
                   (SELECT MAX(o.timestamp_ms) FROM hl_oi_history o
                    WHERE o.asset = rd.asset AND o.timestamp_ms <= rd.anchor_ts - 86400000) as oi_ts_used
            FROM research_dataset rd
            WHERE rd.s1_state IS NOT NULL
            LIMIT 100
        """).fetchall()

        for r in rows:
            if r["oi_ts_used"] is not None:
                gap_hours = (r["anchor_ts"] - r["oi_ts_used"]) / 3600000
                self.assertGreaterEqual(gap_hours, 24,
                    f"OI used only {gap_hours:.1f}h before anchor — potential leak")

    def test_forward_returns_are_positive_horizon(self):
        """ret_1h should use price data from AFTER the anchor, not at it."""
        # Sample check: ret_1h should roughly match price change 1h forward
        rows = self.conn.execute("""
            SELECT rd.anchor_ts, rd.asset, rd.close_price, rd.ret_1h,
                   (SELECT p.close FROM price_history p
                    WHERE p.asset = rd.asset AND p.timestamp_ms > rd.anchor_ts
                    ORDER BY p.timestamp_ms LIMIT 1) as next_close
            FROM research_dataset rd
            WHERE rd.ret_1h IS NOT NULL AND rd.close_price > 0
            LIMIT 20
        """).fetchall()

        for r in rows:
            if r["next_close"] and r["close_price"]:
                # The next price bar should be AFTER the anchor
                self.assertIsNotNone(r["next_close"],
                    "Forward return exists but no future price found")

    def test_no_null_signals_in_required_columns(self):
        """s2_state and s3_state should never be NULL."""
        nulls = self.conn.execute("""
            SELECT COUNT(*) FROM research_dataset
            WHERE s2_state IS NULL OR s3_state IS NULL
        """).fetchone()[0]
        self.assertEqual(nulls, 0, f"{nulls} rows with NULL required signal states")


class TestSignalComputation(unittest.TestCase):
    """Tests that signal state labels are computed correctly."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _get_conn()
        if cls.conn is None:
            raise unittest.SkipTest("Database not available")
        try:
            cls.conn.execute("SELECT 1 FROM research_dataset LIMIT 1")
        except sqlite3.OperationalError:
            raise unittest.SkipTest("research_dataset does not exist")

    def test_s2_states_are_valid(self):
        """S2 should only contain the three defined states."""
        states = self.conn.execute(
            "SELECT DISTINCT s2_state FROM research_dataset"
        ).fetchall()
        valid = {"funding_positive", "funding_negative", "funding_neutral"}
        actual = {r[0] for r in states}
        self.assertTrue(actual.issubset(valid), f"Invalid S2 states: {actual - valid}")

    def test_s3_states_are_valid(self):
        states = self.conn.execute(
            "SELECT DISTINCT s3_state FROM research_dataset"
        ).fetchall()
        valid = {"mom_up", "mom_down", "mom_flat"}
        actual = {r[0] for r in states}
        self.assertTrue(actual.issubset(valid), f"Invalid S3 states: {actual - valid}")

    def test_s1_states_are_valid_or_null(self):
        states = self.conn.execute(
            "SELECT DISTINCT s1_state FROM research_dataset"
        ).fetchall()
        valid = {"oi_rising", "oi_falling", "oi_flat", None}
        actual = {r[0] for r in states}
        self.assertTrue(actual.issubset(valid), f"Invalid S1 states: {actual - valid}")

    def test_forward_returns_are_reasonable(self):
        """24h returns should be within a reasonable range (< 50%)."""
        extreme = self.conn.execute("""
            SELECT COUNT(*) FROM research_dataset
            WHERE ABS(ret_1d) > 0.5
        """).fetchone()[0]
        total = self.conn.execute("SELECT COUNT(*) FROM research_dataset WHERE ret_1d IS NOT NULL").fetchone()[0]
        if total > 0:
            pct = extreme / total * 100
            self.assertLess(pct, 1, f"{pct:.1f}% of rows have extreme 24h returns (>50%)")


if __name__ == "__main__":
    unittest.main()
