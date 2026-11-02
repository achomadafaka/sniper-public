"""Tests for Phase 1.5: immutable signal_history + score_history capture.

Uses an in-memory SQLite database to verify append-only semantics,
column correctness, and edge cases.
"""

import sqlite3

import pytest

from db import (
    init_db,
    insert_score_history,
    insert_signal_history,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# signal_history tests
# ---------------------------------------------------------------------------


class TestSignalHistory:
    def test_insert_basic(self, conn):
        signals = [
            {
                "wallet": "0xaaa",
                "condition_id": "cond1",
                "outcome": "yes",
                "title": "Will X?",
                "is_open": True,
                "entry_price": 0.35,
                "entry_notional": 500.0,
                "current_price": 0.42,
                "position_insider_score": 85.0,
                "position_speculator_score": 15.0,
                "position_classification": "position_insider_candidate",
                "activity_state": "accumulating",
                "hold_hours": 12.5,
                "entry_anchor_ts": 1699990000,
            }
        ]
        count = insert_signal_history(
            conn, signals=signals, analyzed_ts=1700000000, analysis_version=42
        )
        assert count == 1
        conn.commit()

        row = conn.execute("SELECT * FROM signal_history").fetchone()
        assert row["wallet"] == "0xaaa"
        assert row["condition_id"] == "cond1"
        assert row["outcome"] == "yes"
        assert row["title"] == "Will X?"
        assert row["is_open"] == 1
        assert row["entry_price"] == 0.35
        assert row["entry_notional"] == 500.0
        assert row["current_price"] == 0.42
        assert row["position_insider_score"] == 85.0
        assert row["position_speculator_score"] == 15.0
        assert row["position_classification"] == "position_insider_candidate"
        assert row["activity_state"] == "accumulating"
        assert row["hold_hours"] == 12.5
        assert row["entry_anchor_ts"] == 1699990000
        assert row["analyzed_ts"] == 1700000000
        assert row["analysis_version"] == 42

    def test_insert_multiple(self, conn):
        signals = [
            {"wallet": "0xa", "condition_id": "c1", "outcome": "yes",
             "position_insider_score": 90.0},
            {"wallet": "0xb", "condition_id": "c2", "outcome": "no",
             "position_insider_score": 60.0},
            {"wallet": "0xc", "condition_id": "c3", "outcome": "yes",
             "position_insider_score": 75.0},
        ]
        count = insert_signal_history(
            conn, signals=signals, analyzed_ts=1700000000
        )
        assert count == 3
        conn.commit()

        rows = conn.execute("SELECT * FROM signal_history ORDER BY id").fetchall()
        assert len(rows) == 3
        assert rows[0]["wallet"] == "0xa"
        assert rows[1]["wallet"] == "0xb"
        assert rows[2]["wallet"] == "0xc"

    def test_empty_signals(self, conn):
        count = insert_signal_history(
            conn, signals=[], analyzed_ts=1700000000
        )
        assert count == 0
        rows = conn.execute("SELECT COUNT(*) FROM signal_history").fetchone()
        assert rows[0] == 0

    def test_append_only(self, conn):
        """Same signal appended twice with different timestamps = 2 rows."""
        signal = [{"wallet": "0xaaa", "condition_id": "c1", "outcome": "yes",
                    "position_insider_score": 80.0, "current_price": 0.40}]
        insert_signal_history(conn, signals=signal, analyzed_ts=1700000000)
        # Same signal, different ts and different score
        signal[0]["position_insider_score"] = 85.0
        signal[0]["current_price"] = 0.50
        insert_signal_history(conn, signals=signal, analyzed_ts=1700001000)
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM signal_history WHERE wallet='0xaaa' ORDER BY analyzed_ts"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["position_insider_score"] == 80.0
        assert rows[0]["current_price"] == 0.40
        assert rows[1]["position_insider_score"] == 85.0
        assert rows[1]["current_price"] == 0.50

    def test_defaults_for_missing_keys(self, conn):
        """Signal dict with minimal keys should use defaults."""
        signal = [{"wallet": "0xmin", "condition_id": "c1", "outcome": "yes"}]
        count = insert_signal_history(
            conn, signals=signal, analyzed_ts=1700000000
        )
        assert count == 1
        conn.commit()

        row = conn.execute("SELECT * FROM signal_history").fetchone()
        assert row["title"] == ""
        assert row["is_open"] == 1
        assert row["entry_price"] == 0.0
        assert row["position_insider_score"] == 0.0
        assert row["activity_state"] == "holding"
        assert row["hold_hours"] == 0.0
        assert row["entry_anchor_ts"] == 0

    def test_entry_anchor_ts_preserved_across_snapshots(self, conn):
        """entry_anchor_ts should be immutable across analysis runs."""
        anchor = 1699990000
        for ts, price in [(1700000000, 0.40), (1700001000, 0.55)]:
            insert_signal_history(
                conn,
                signals=[{
                    "wallet": "0xanchor",
                    "condition_id": "c1",
                    "outcome": "yes",
                    "entry_anchor_ts": anchor,
                    "current_price": price,
                    "position_insider_score": 80.0,
                }],
                analyzed_ts=ts,
            )
        conn.commit()

        rows = conn.execute(
            "SELECT entry_anchor_ts, current_price FROM signal_history "
            "WHERE wallet = '0xanchor' ORDER BY analyzed_ts"
        ).fetchall()
        assert len(rows) == 2
        # Anchor stays the same even as price evolves
        assert rows[0]["entry_anchor_ts"] == anchor
        assert rows[1]["entry_anchor_ts"] == anchor
        # But current_price changes
        assert rows[0]["current_price"] == 0.40
        assert rows[1]["current_price"] == 0.55


# ---------------------------------------------------------------------------
# score_history tests
# ---------------------------------------------------------------------------


class TestScoreHistory:
    def test_insert_basic(self, conn):
        insert_score_history(
            conn,
            wallet="0xwallet1",
            insider_score=88.5,
            speculator_score=12.0,
            classification="insider_candidate",
            tier="elite",
            strategy_group="conviction_sniper",
            win_rate=72.5,
            closed_sample_size=40,
            qualifying_roi=35.0,
            qualifying_capital=5000.0,
            pnl=1200.0,
            position_count=8,
            wallet_edge_tier="strong",
            followability_cached=82.3,
            is_bot=False,
            analyzed_ts=1700000000,
            analysis_version=42,
        )
        conn.commit()

        row = conn.execute("SELECT * FROM score_history").fetchone()
        assert row["wallet"] == "0xwallet1"
        assert row["insider_score"] == 88.5
        assert row["speculator_score"] == 12.0
        assert row["classification"] == "insider_candidate"
        assert row["tier"] == "elite"
        assert row["strategy_group"] == "conviction_sniper"
        assert row["win_rate"] == 72.5
        assert row["closed_sample_size"] == 40
        assert row["qualifying_roi"] == 35.0
        assert row["qualifying_capital"] == 5000.0
        assert row["pnl"] == 1200.0
        assert row["position_count"] == 8
        assert row["wallet_edge_tier"] == "strong"
        assert row["followability_cached"] == 82.3
        assert row["is_bot"] == 0
        assert row["analyzed_ts"] == 1700000000
        assert row["analysis_version"] == 42

    def test_append_only(self, conn):
        """Same wallet, two snapshots = 2 rows."""
        insert_score_history(
            conn, wallet="0xw", insider_score=80.0,
            classification="insider_candidate", analyzed_ts=1700000000,
        )
        insert_score_history(
            conn, wallet="0xw", insider_score=75.0,
            classification="emerging_insider", analyzed_ts=1700001000,
        )
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM score_history WHERE wallet='0xw' ORDER BY analyzed_ts"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["insider_score"] == 80.0
        assert rows[0]["classification"] == "insider_candidate"
        assert rows[1]["insider_score"] == 75.0
        assert rows[1]["classification"] == "emerging_insider"

    def test_nullable_fields(self, conn):
        """qualifying_roi and followability_cached can be None."""
        insert_score_history(
            conn, wallet="0xn",
            qualifying_roi=None,
            followability_cached=None,
            analyzed_ts=1700000000,
        )
        conn.commit()

        row = conn.execute("SELECT * FROM score_history").fetchone()
        assert row["qualifying_roi"] is None
        assert row["followability_cached"] is None

    def test_bot_flag(self, conn):
        insert_score_history(
            conn, wallet="0xbot", is_bot=True,
            classification="speculator_bot", analyzed_ts=1700000000,
        )
        conn.commit()

        row = conn.execute("SELECT * FROM score_history").fetchone()
        assert row["is_bot"] == 1

    def test_defaults(self, conn):
        """Minimal call should use zero defaults for all numeric fields."""
        insert_score_history(conn, wallet="0xdef", analyzed_ts=1700000000)
        conn.commit()

        row = conn.execute("SELECT * FROM score_history").fetchone()
        assert row["insider_score"] == 0
        assert row["speculator_score"] == 0
        assert row["classification"] == ""
        assert row["win_rate"] == 0
        assert row["pnl"] == 0


# ---------------------------------------------------------------------------
# Integration: both tables together
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_signal_and_score_together(self, conn):
        """Simulate what run_once does: insert signals + score for one wallet."""
        signals = [
            {"wallet": "0xw1", "condition_id": "c1", "outcome": "yes",
             "position_insider_score": 90.0, "entry_price": 0.30},
            {"wallet": "0xw1", "condition_id": "c2", "outcome": "no",
             "position_insider_score": 70.0, "entry_price": 0.55},
        ]
        insert_signal_history(
            conn, signals=signals, analyzed_ts=1700000000, analysis_version=1
        )
        insert_score_history(
            conn, wallet="0xw1", insider_score=85.0,
            classification="insider_candidate", win_rate=65.0,
            analyzed_ts=1700000000, analysis_version=1,
        )
        conn.commit()

        sig_count = conn.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0]
        score_count = conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
        assert sig_count == 2
        assert score_count == 1

    def test_multiple_analysis_runs(self, conn):
        """Two analysis runs for same wallet accumulate history."""
        for ts, insider in [(1700000000, 80.0), (1700001000, 85.0)]:
            insert_signal_history(
                conn,
                signals=[{"wallet": "0xw", "condition_id": "c1", "outcome": "yes",
                          "position_insider_score": insider}],
                analyzed_ts=ts,
            )
            insert_score_history(
                conn, wallet="0xw", insider_score=insider, analyzed_ts=ts,
            )
        conn.commit()

        sig_rows = conn.execute(
            "SELECT * FROM signal_history WHERE wallet='0xw' ORDER BY analyzed_ts"
        ).fetchall()
        score_rows = conn.execute(
            "SELECT * FROM score_history WHERE wallet='0xw' ORDER BY analyzed_ts"
        ).fetchall()
        assert len(sig_rows) == 2
        assert len(score_rows) == 2
        # Scores evolved over time
        assert sig_rows[0]["position_insider_score"] == 80.0
        assert sig_rows[1]["position_insider_score"] == 85.0
        assert score_rows[0]["insider_score"] == 80.0
        assert score_rows[1]["insider_score"] == 85.0
