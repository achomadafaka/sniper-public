"""
Tests for signal_queue table CRUD operations.

Validates:
- Table creation is idempotent
- Insert creates rows with correct defaults
- Update modifies status and timing fields
- reject_reason is recorded
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.persistence.db_hyperliquid import (
    ensure_signal_queue_table,
    insert_signal_queue_entry,
    update_signal_queue_status,
)


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_signal_queue_table(conn)
    return conn


class TestSignalQueueInsert:

    def test_insert_returns_rowid(self):
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="insider_copy_v1",
            venue="polymarket",
            asset_id="cond_123",
            direction="yes",
            confidence=85.0,
            signal_ts=1700000000,
        )
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_insert_defaults(self):
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="hl_liquidation_v1",
            venue="hyperliquid",
            asset_id="BTC",
            direction="long",
            confidence=70.0,
            signal_ts=1700000000,
        )
        row = conn.execute("SELECT * FROM signal_queue WHERE id = ?", (row_id,)).fetchone()
        assert row["status"] == "pending"
        assert row["venue"] == "hyperliquid"
        assert row["metadata_json"] == "{}"
        assert row["reject_reason"] == ""
        assert row["entry_ts"] is None
        assert row["fill_ts"] is None
        assert row["latency_ms"] is None

    def test_insert_with_metadata(self):
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="test",
            venue="polymarket",
            asset_id="cond_456",
            direction="no",
            confidence=50.0,
            signal_ts=1700000000,
            metadata_json='{"wallet": "0xabc"}',
        )
        row = conn.execute("SELECT * FROM signal_queue WHERE id = ?", (row_id,)).fetchone()
        assert row["metadata_json"] == '{"wallet": "0xabc"}'


class TestSignalQueueUpdate:

    def test_update_to_entered(self):
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="test",
            venue="polymarket",
            asset_id="cond_1",
            direction="yes",
            confidence=80.0,
            signal_ts=1700000000,
        )
        update_signal_queue_status(
            conn, row_id,
            status="entered",
            entry_ts=1700000005,
            fill_ts=1700000005,
            latency_ms=5000,
        )
        row = conn.execute("SELECT * FROM signal_queue WHERE id = ?", (row_id,)).fetchone()
        assert row["status"] == "entered"
        assert row["entry_ts"] == 1700000005
        assert row["fill_ts"] == 1700000005
        assert row["latency_ms"] == 5000

    def test_update_to_rejected_with_reason(self):
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="test",
            venue="polymarket",
            asset_id="cond_2",
            direction="yes",
            confidence=40.0,
            signal_ts=1700000000,
        )
        update_signal_queue_status(
            conn, row_id,
            status="rejected",
            reject_reason="conviction_gate",
        )
        row = conn.execute("SELECT * FROM signal_queue WHERE id = ?", (row_id,)).fetchone()
        assert row["status"] == "rejected"
        assert row["reject_reason"] == "conviction_gate"
        # Timing fields should remain null
        assert row["entry_ts"] is None
        assert row["latency_ms"] is None

    def test_partial_update(self):
        """Only status is required; other fields are optional."""
        conn = _get_conn()
        row_id = insert_signal_queue_entry(
            conn,
            source="test",
            venue="polymarket",
            asset_id="cond_3",
            direction="yes",
            confidence=60.0,
            signal_ts=1700000000,
        )
        update_signal_queue_status(conn, row_id, status="expired")
        row = conn.execute("SELECT * FROM signal_queue WHERE id = ?", (row_id,)).fetchone()
        assert row["status"] == "expired"


class TestSignalQueueTableIdempotent:

    def test_double_create(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_signal_queue_table(conn)
        ensure_signal_queue_table(conn)
        # Should not raise
        row_id = insert_signal_queue_entry(
            conn,
            source="test",
            venue="polymarket",
            asset_id="cond_x",
            direction="yes",
            confidence=50.0,
            signal_ts=1700000000,
        )
        assert row_id > 0
