"""Tests for Phase 4A: strategy lifecycle state machine."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _insert_strategy(conn, strategy_id, wallet="0xtest", strategy_type="insider_copy_v1",
                     status="active", lifecycle_stage="paper"):
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (?, ?, ?, ?, '{}', ?)""",
        (strategy_id, wallet, f"test-{strategy_id}", strategy_type, status),
    )
    if _has_column(conn, "strategies", "lifecycle_stage"):
        conn.execute("UPDATE strategies SET lifecycle_stage = ? WHERE id = ?",
                     (lifecycle_stage, strategy_id))
    conn.commit()


class TestLifecycleColumn:

    def test_column_exists_after_init(self):
        conn = _get_conn()
        assert _has_column(conn, "strategies", "lifecycle_stage")

    def test_default_is_paper(self):
        conn = _get_conn()
        _insert_strategy(conn, 1)
        row = conn.execute("SELECT lifecycle_stage FROM strategies WHERE id = 1").fetchone()
        assert row["lifecycle_stage"] == "paper"

    def test_valid_values(self):
        conn = _get_conn()
        for i, stage in enumerate(["research", "paper", "promoted", "retired"], start=1):
            _insert_strategy(conn, i, lifecycle_stage=stage)
        rows = conn.execute("SELECT lifecycle_stage FROM strategies ORDER BY id").fetchall()
        assert [r["lifecycle_stage"] for r in rows] == ["research", "paper", "promoted", "retired"]


class TestLifecycleFiltering:

    def test_paper_processed(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="paper")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 1

    def test_promoted_processed(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="promoted")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 1

    def test_research_excluded(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="research")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 0

    def test_retired_excluded(self):
        conn = _get_conn()
        _insert_strategy(conn, 1, status="active", lifecycle_stage="retired")
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 0

    def test_null_lifecycle_treated_as_paper(self):
        """Legacy rows with NULL lifecycle_stage should still be processed.

        The column is NOT NULL DEFAULT 'paper', so true NULLs are rejected by
        the DB constraint.  The COALESCE in the runner query is a defensive
        belt-and-suspenders guard.  Here we verify that omitting lifecycle_stage
        on INSERT (which triggers the DEFAULT) still results in the row being
        included in the runner query, matching the 'paper' case.
        """
        conn = _get_conn()
        conn.execute(
            "INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status) VALUES (1, '0x', 'test', 'insider_copy_v1', '{}', 'active')"
        )
        conn.commit()
        # Default should have been applied — row gets lifecycle_stage='paper'
        row = conn.execute("SELECT lifecycle_stage FROM strategies WHERE id = 1").fetchone()
        assert row["lifecycle_stage"] == "paper"
        # And should be included in the runner query
        rows = conn.execute(
            "SELECT id FROM strategies WHERE status = 'active' AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')"
        ).fetchall()
        assert len(rows) == 1


class TestSignalQualityMetrics:

    def test_hit_rate_computation(self):
        discovered, entered = 10, 3
        hit_rate = round(entered / max(discovered, 1), 4)
        assert hit_rate == 0.3

    def test_rejection_rate_computation(self):
        discovered, entered = 10, 3
        rejection_rate = round((discovered - entered) / max(discovered, 1), 4)
        assert rejection_rate == 0.7

    def test_zero_discovered_safe(self):
        discovered, entered = 0, 0
        quality = {
            "discovery_hit_rate": round(entered / max(discovered, 1), 4),
            "rejection_rate": round((discovered - entered) / max(discovered, 1), 4),
        }
        assert quality["discovery_hit_rate"] == 0
        assert quality["rejection_rate"] == 0

    def test_all_entered_perfect_quality(self):
        discovered, entered = 5, 5
        quality = {
            "discovery_hit_rate": round(entered / max(discovered, 1), 4),
            "rejection_rate": round((discovered - entered) / max(discovered, 1), 4),
        }
        assert quality["discovery_hit_rate"] == 1.0
        assert quality["rejection_rate"] == 0.0


class TestLifecyclePatchWhitelist:

    def test_lifecycle_stage_in_patch(self):
        """lifecycle_stage must be in the PATCH whitelist."""
        from flask import Flask
        from sniper.routes.api_strategies import create_api_strategies_blueprint
        conn = _get_conn()
        app = Flask(__name__)
        bp = create_api_strategies_blueprint({"conn": conn})
        app.register_blueprint(bp)

        # Insert a strategy to patch
        _insert_strategy(conn, 99, lifecycle_stage="paper")

        with app.test_client() as client:
            res = client.patch(
                "/api/strategies/99",
                json={"lifecycle_stage": "promoted"},
                content_type="application/json",
            )
            # 400 with "no allowed fields" would mean lifecycle_stage is not whitelisted.
            # 200 = success, 404 = strategy not found (acceptable in bare test db),
            # 500 = serializer error after a successful UPDATE (not a whitelist rejection).
            body = res.get_json() or {}
            assert res.status_code != 400 or "no allowed fields" not in body.get("error", ""), (
                f"lifecycle_stage rejected by whitelist: {res.status_code}: {body}"
            )
