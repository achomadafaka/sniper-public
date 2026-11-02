"""Tests for Phase 3B: signal timestamp plumbing + copy viability scoring."""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.strategies.insider_copy_v1 import InsiderCopyV1Strategy
from sniper.strategies.base import StrategySignal
from db import init_db
from sniper.persistence.db_hyperliquid import (
    ensure_signal_queue_table,
    ensure_copy_viability_table,
    insert_signal_queue_entry,
    update_signal_queue_status,
)


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_signal_queue_table(conn)
    ensure_copy_viability_table(conn)
    return conn


class TestInsiderCopyEntryMeta:

    def _make_signal(self, analyzed_ts=1700000000):
        from datetime import datetime, timedelta, timezone
        # end_date must be within max_days_to_resolution (default 60)
        near_end = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return StrategySignal(
            wallet="0xtest",
            condition_id="cond_test",
            outcome="YES",
            title="Test Market",
            current_price=0.45,
            position_value=5000.0,
            analyzed_ts=analyzed_ts,
            metadata={
                "position_insider_score": 95.0,
                "position_speculator_score": 10.0,
                "position_classification": "position_insider_candidate",
                "end_date_iso": near_end,
                "win_rate": 0.65,
                "qualifying_roi": 25.0,
                "wallet_classification": "high_winrate_review",
                "source_entry_price": 0.40,
                "wallet_quality_score": 80.0,
                "wallet_quality_eligible": 1,
            },
        )

    def _make_strategy(self):
        return {
            "id": 99,
            "wallet": "0xtest",
            "strategy_type": "insider_copy_v1",
            "parameters": json.dumps({
                "max_concurrent_positions": 15,
                "max_entry_price": 0.70,
                "min_position_value_usdc": 500,
            }),
            "status": "active",
        }

    @pytest.mark.integration
    def test_enter_metadata_has_signal_ts(self):
        """enter() must return metadata with signal_ts and entry_ts when entry is accepted."""
        conn = _get_conn()
        plugin = InsiderCopyV1Strategy()
        signal = self._make_signal(analyzed_ts=1700000000)
        strategy = self._make_strategy()
        decision = plugin.enter(conn, strategy, signal, [])
        # This signal is designed to pass all gates — assert it actually enters
        assert decision.should_enter, f"Expected entry but got rejection: {decision.reason}"
        assert decision.metadata is not None
        assert "signal_ts" in decision.metadata
        assert decision.metadata["signal_ts"] == 1700000000
        assert "entry_ts" in decision.metadata
        assert decision.metadata["entry_ts"] > 0


class TestSignalQueueTimestamp:

    def test_signal_ts_from_analyzed_ts(self):
        analyzed_ts = 1700000000
        now = int(time.time())
        signal_ts = analyzed_ts if analyzed_ts > 0 else now
        assert signal_ts == analyzed_ts
        assert signal_ts != now

    def test_signal_ts_fallback_to_now(self):
        analyzed_ts = 0
        now = int(time.time())
        signal_ts = analyzed_ts if analyzed_ts > 0 else now
        assert signal_ts == now


# -----------------------------------------------------------------------
# Copy viability scoring tests (Worker 2)
# -----------------------------------------------------------------------

from sniper.analysis.copy_viability import measure_wallet_viability, update_all_viability, get_viability_leaderboard


def _seed_signals(conn, wallet, entered=5, rejected=3, avg_latency=500):
    """Seed signal_queue with test data for a wallet."""
    wallet_lower = wallet.strip().lower()
    for i in range(entered):
        sid = insert_signal_queue_entry(
            conn, source="insider_copy_v1", venue="polymarket",
            asset_id=f"cond_{i}", direction="yes", confidence=80.0,
            signal_ts=1700000000 + i,
            metadata_json=json.dumps({"wallet": wallet_lower}),
        )
        update_signal_queue_status(
            conn, sid, status="entered",
            entry_ts=1700000000 + i + (avg_latency // 1000),
            fill_ts=1700000000 + i + (avg_latency // 1000),
            latency_ms=avg_latency,
        )
    for i in range(rejected):
        insert_signal_queue_entry(
            conn, source="insider_copy_v1", venue="polymarket",
            asset_id=f"cond_rej_{i}", direction="yes", confidence=50.0,
            signal_ts=1700000000 + entered + i,
            metadata_json=json.dumps({"wallet": wallet_lower}),
        )


class TestMeasureWalletViability:

    def test_with_data(self):
        conn = _get_conn()
        _seed_signals(conn, "0xABC", entered=5, rejected=3, avg_latency=500)
        result = measure_wallet_viability(conn, "0xABC")
        assert result["signal_count"] == 8
        assert result["fill_rate"] == 5 / 8
        assert result["avg_latency_ms"] == 500
        assert result["score"] > 0

    def test_empty_data(self):
        conn = _get_conn()
        result = measure_wallet_viability(conn, "0xnonexistent")
        assert result["signal_count"] == 0
        assert result["fill_rate"] == 0
        assert result["avg_latency_ms"] == 0
        assert result["score"] == 0

    def test_all_rejected(self):
        conn = _get_conn()
        _seed_signals(conn, "0xbad", entered=0, rejected=5)
        result = measure_wallet_viability(conn, "0xbad")
        assert result["signal_count"] == 5
        assert result["fill_rate"] == 0
        assert result["score"] == 0

    def test_mixed_case_wallet_matches(self):
        """LOWER() in SQL must handle mixed-case stored data, not just query input."""
        conn = _get_conn()
        # Seed with mixed-case wallet directly in metadata (bypassing _seed_signals normalization)
        # This simulates production where strategy_runner stores signal.wallet as-is
        sid = insert_signal_queue_entry(
            conn, source="insider_copy_v1", venue="polymarket",
            asset_id="cond_mc", direction="yes", confidence=80.0,
            signal_ts=1700000000,
            metadata_json=json.dumps({"wallet": "0xAbCdEf123"}),  # mixed-case stored
        )
        update_signal_queue_status(conn, sid, status="entered", entry_ts=1700000001, fill_ts=1700000001, latency_ms=100)
        # Query with different case
        result = measure_wallet_viability(conn, "0xABCDEF123")
        assert result["signal_count"] == 1
        # Also query with lowercase
        result2 = measure_wallet_viability(conn, "0xabcdef123")
        assert result2["signal_count"] == 1


class TestUpdateAllViability:

    def test_populates_table(self):
        conn = _get_conn()
        _seed_signals(conn, "0xabc", entered=5, rejected=2)
        _seed_signals(conn, "0xdef", entered=3, rejected=1)
        count = update_all_viability(conn)
        assert count == 2
        rows = conn.execute("SELECT * FROM copy_viability ORDER BY score DESC").fetchall()
        assert len(rows) == 2


class TestViabilityLeaderboard:

    def test_sorted_by_score(self):
        conn = _get_conn()
        _seed_signals(conn, "0xgood", entered=10, rejected=1, avg_latency=200)
        _seed_signals(conn, "0xbad", entered=2, rejected=8, avg_latency=2000)
        update_all_viability(conn)
        lb = get_viability_leaderboard(conn, limit=10)
        assert len(lb) == 2
        assert lb[0]["score"] > lb[1]["score"]

    def test_limit(self):
        conn = _get_conn()
        _seed_signals(conn, "0xa", entered=5, rejected=0)
        _seed_signals(conn, "0xb", entered=4, rejected=0)
        _seed_signals(conn, "0xc", entered=3, rejected=0)
        update_all_viability(conn)
        lb = get_viability_leaderboard(conn, limit=2)
        assert len(lb) == 2


# -----------------------------------------------------------------------
# Flask endpoint tests (following test_dashboard_legacy_schema.py pattern)
# -----------------------------------------------------------------------

from flask import Flask
from sniper.routes.api_strategies import create_api_strategies_blueprint


class TestCopyViabilityEndpoints:

    def _make_app(self):
        conn = _get_conn()
        _seed_signals(conn, "0xwallet1", entered=5, rejected=2)
        update_all_viability(conn)
        app = Flask(__name__)
        ctx_dict = {"conn": conn}
        bp = create_api_strategies_blueprint(ctx_dict)
        app.register_blueprint(bp)
        return app, conn

    def test_copy_viability_single_wallet(self):
        app, conn = self._make_app()
        res = app.test_client().get("/api/copy-viability?wallet=0xwallet1")
        assert res.status_code == 200
        data = res.get_json()
        assert "signal_count" in data
        assert "score" in data
        assert "fill_rate" in data
        assert data["signal_count"] == 7

    def test_copy_viability_missing_wallet(self):
        app, conn = self._make_app()
        res = app.test_client().get("/api/copy-viability")
        assert res.status_code == 400

    def test_copy_viability_leaderboard(self):
        app, conn = self._make_app()
        res = app.test_client().get("/api/copy-viability/leaderboard")
        assert res.status_code == 200
        data = res.get_json()
        assert "items" in data
        assert "count" in data
        assert isinstance(data["items"], list)

    def test_copy_viability_leaderboard_limit(self):
        app, conn = self._make_app()
        res = app.test_client().get("/api/copy-viability/leaderboard?limit=1")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["items"]) <= 1
