"""Pattern B write-path guards for legacy db_discovery functions."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from sniper.legacy_guards import LegacyPipelineDisabledError


def test_init_discovery_schema_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import init_discovery_schema

    conn = sqlite3.connect(":memory:")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        init_discovery_schema(conn)
    assert "init_discovery_schema" in str(excinfo.value)


def test_init_discovery_schema_works_when_enabled(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import init_discovery_schema

    conn = sqlite3.connect(":memory:")
    init_discovery_schema(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%discovery%'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    assert any("discovery" in table.lower() for table in tables), tables
    conn.close()


def test_record_discovery_event_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    from db_discovery import record_discovery_event

    conn = sqlite3.connect(":memory:")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        record_discovery_event(conn, "0xabc", "low_buy", 1)
    assert "record_discovery_event" in str(excinfo.value)


_REMAINING_GUARDED_CALLS = (
    ("update_wallet_activity_hourly", ("0xabc", 1, 1.0)),
    ("score_pending_events", ()),
    ("enqueue_high_value_wallets", ()),
    ("release_expired_leases", ()),
    ("claim_wallets_for_analysis", ("worker", 1)),
    ("get_next_wallet_for_analysis", ("worker",)),
    ("complete_wallet_analysis", ("0xabc", 1)),
    ("fail_wallet_analysis", ("0xabc", 1, "err")),
    ("mark_wallet_analyzed", ("0xabc", 1)),
    ("migrate_low_buy_trades", ()),
    ("enqueue_wallets_for_analysis", (["0xabc"],)),
)


@pytest.mark.parametrize(("func_name", "args"), _REMAINING_GUARDED_CALLS)
def test_remaining_db_discovery_write_paths_raise_when_disabled(
    monkeypatch,
    func_name,
    args,
):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    module = importlib.import_module("db_discovery")
    func = getattr(module, func_name)

    conn = sqlite3.connect(":memory:")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        func(conn, *args)
    assert func_name in str(excinfo.value)
