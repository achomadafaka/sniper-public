"""Tests for instance_tag column on strategy/replay/audit tables.

Verifies:
- All 7 tables have the instance_tag column after init_db
- Backfill function tags NULL/empty rows
- Backfill is idempotent (already-tagged rows are NOT overwritten)
- Backfill skips when INSTANCE_TAG env var is empty
"""

import os
import sqlite3

import pytest

from db import ensure_instance_tags, init_db

INSTANCE_TAG_TABLES = [
    "strategies",
    "strategy_positions",
    "strategy_executions",
    "strategy_runs",
    "replay_configs",
    "replay_runs",
    "mcp_audit_log",
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Schema presence tests
# ---------------------------------------------------------------------------


class TestInstanceTagSchema:
    @pytest.mark.parametrize("table", INSTANCE_TAG_TABLES)
    def test_table_has_instance_tag_column(self, conn, table):
        cols = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert "instance_tag" in cols, (
            f"Table {table} is missing instance_tag column"
        )

    @pytest.mark.parametrize("table", INSTANCE_TAG_TABLES)
    def test_instance_tag_column_is_nullable(self, conn, table):
        """instance_tag should be nullable TEXT (notnull == 0)."""
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for row in rows:
            if row[1] == "instance_tag":
                assert row[2].upper() == "TEXT"
                assert row[3] == 0, "instance_tag should be nullable"
                return
        pytest.fail(f"instance_tag column not found in {table}")


# ---------------------------------------------------------------------------
# Index presence tests
# ---------------------------------------------------------------------------


class TestInstanceTagIndexes:
    EXPECTED_INDEXES = [
        "idx_strategies_instance_tag",
        "idx_strategy_positions_instance_tag",
        "idx_strategy_executions_instance_tag",
        "idx_strategy_runs_instance_tag",
        "idx_replay_configs_instance_tag",
        "idx_replay_runs_instance_tag",
        "idx_mcp_audit_log_instance_tag",
        "idx_strategy_executions_instance_tag_strategy",
        "idx_strategy_runs_instance_tag_strategy",
    ]

    @pytest.mark.parametrize("index_name", EXPECTED_INDEXES)
    def test_index_exists(self, conn, index_name):
        result = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        assert result is not None, f"Index {index_name} does not exist"


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------


class TestBackfill:
    def test_backfill_tags_null_rows(self, conn, monkeypatch):
        """Rows with NULL instance_tag get tagged."""
        # Insert a row with NULL instance_tag
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status) "
            "VALUES ('0xaaa', 'test', 'signal_only', 'active')"
        )
        conn.commit()

        # Verify it's NULL
        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xaaa'"
        ).fetchone()
        assert row["instance_tag"] is None

        # Backfill
        monkeypatch.setenv("INSTANCE_TAG", "vps-ams-01")
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xaaa'"
        ).fetchone()
        assert row["instance_tag"] == "vps-ams-01"

    def test_backfill_tags_empty_string_rows(self, conn, monkeypatch):
        """Rows with empty-string instance_tag get tagged."""
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status, instance_tag) "
            "VALUES ('0xbbb', 'test', 'signal_only', 'active', '')"
        )
        conn.commit()

        monkeypatch.setenv("INSTANCE_TAG", "vps-ams-01")
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xbbb'"
        ).fetchone()
        assert row["instance_tag"] == "vps-ams-01"

    def test_backfill_does_not_overwrite_existing_tags(self, conn, monkeypatch):
        """Already-tagged rows must NOT be overwritten by backfill."""
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status, instance_tag) "
            "VALUES ('0xccc', 'test', 'signal_only', 'active', 'existing-tag')"
        )
        conn.commit()

        monkeypatch.setenv("INSTANCE_TAG", "new-tag")
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xccc'"
        ).fetchone()
        assert row["instance_tag"] == "existing-tag"

    def test_backfill_skips_when_env_empty(self, conn, monkeypatch):
        """No updates when INSTANCE_TAG is not set."""
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status) "
            "VALUES ('0xddd', 'test', 'signal_only', 'active')"
        )
        conn.commit()

        monkeypatch.delenv("INSTANCE_TAG", raising=False)
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xddd'"
        ).fetchone()
        assert row["instance_tag"] is None

    def test_backfill_skips_when_env_whitespace(self, conn, monkeypatch):
        """No updates when INSTANCE_TAG is just whitespace."""
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status) "
            "VALUES ('0xeee', 'test', 'signal_only', 'active')"
        )
        conn.commit()

        monkeypatch.setenv("INSTANCE_TAG", "   ")
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xeee'"
        ).fetchone()
        assert row["instance_tag"] is None

    def test_backfill_all_seven_tables(self, conn, monkeypatch):
        """Backfill touches all 7 tables."""
        import time as _time

        now_ts = int(_time.time())

        # Insert one row into each table with NULL instance_tag
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status) "
            "VALUES ('0xaaa', 'test', 'signal_only', 'active')"
        )
        conn.execute(
            "INSERT INTO strategy_positions "
            "(wallet, condition_id, outcome, strategy_id, state, updated_ts) "
            "VALUES ('0xaaa', 'cond1', 'Yes', '1', 'open', ?)",
            (now_ts,),
        )
        conn.execute(
            "INSERT INTO strategy_executions "
            "(strategy_id, wallet, condition_id, outcome) "
            "VALUES (1, '0xaaa', 'cond1', 'Yes')"
        )
        conn.execute(
            "INSERT INTO strategy_runs (strategy_id, status) "
            "VALUES (1, 'running')"
        )
        conn.execute(
            "INSERT INTO replay_configs (name, config_json) "
            "VALUES ('test-config', '{}')"
        )
        conn.execute(
            "INSERT INTO replay_runs (config_json, summary_json, result_json) "
            "VALUES ('{}', '{}', '{}')"
        )
        conn.execute(
            "INSERT INTO mcp_audit_log (tool_name) "
            "VALUES ('test_tool')"
        )
        conn.commit()

        monkeypatch.setenv("INSTANCE_TAG", "vps-ams-01")
        ensure_instance_tags(conn)
        conn.commit()

        for table in INSTANCE_TAG_TABLES:
            row = conn.execute(
                f"SELECT instance_tag FROM {table} LIMIT 1"
            ).fetchone()
            assert row is not None, f"No rows in {table}"
            assert row["instance_tag"] == "vps-ams-01", (
                f"Backfill failed for {table}"
            )

    def test_backfill_idempotent_repeated_calls(self, conn, monkeypatch):
        """Calling backfill twice with same tag doesn't error or change data."""
        conn.execute(
            "INSERT INTO strategies (wallet, label, strategy_type, status) "
            "VALUES ('0xfff', 'test', 'signal_only', 'active')"
        )
        conn.commit()

        monkeypatch.setenv("INSTANCE_TAG", "vps-ams-01")
        ensure_instance_tags(conn)
        ensure_instance_tags(conn)
        conn.commit()

        row = conn.execute(
            "SELECT instance_tag FROM strategies WHERE wallet='0xfff'"
        ).fetchone()
        assert row["instance_tag"] == "vps-ams-01"
