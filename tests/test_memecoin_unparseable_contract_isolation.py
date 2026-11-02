import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as policy
from sniper.services import memecoin_fastlane as fastlane
from sniper.services import memecoin_real_runner as runner


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_contract_parse_fault_has_a_distinct_result(monkeypatch):
    monkeypatch.setattr(
        policy, "frozen_policy_from_position",
        lambda _pos: (_ for _ in ()).throw(ValueError("invalid contract")),
    )
    assert policy.frozen_policy_or_unparseable({}) is policy.CONTRACT_UNPARSEABLE


def test_contract_isolation_does_not_hide_runtime_faults(monkeypatch):
    monkeypatch.setattr(
        policy, "frozen_policy_from_position",
        lambda _pos: (_ for _ in ()).throw(RuntimeError("database failed")),
    )
    with pytest.raises(RuntimeError, match="database failed"):
        policy.frozen_policy_or_unparseable({})


def test_fastlane_holds_an_invalid_contract(monkeypatch):
    monkeypatch.setattr(
        fastlane, "frozen_policy_from_position",
        lambda _pos: (_ for _ in ()).throw(ValueError("invalid contract")),
    )
    assert fastlane._frozen_or_unparseable({}) is policy.CONTRACT_UNPARSEABLE


def test_one_bad_contract_does_not_stop_healthy_position_pricing(monkeypatch):
    conn = _db()
    positions = [
        {"id": 1, "mint": "GOOD", "network_id": "solana", "entry_price": 1.0},
        {"id": 2, "mint": "BAD", "network_id": "solana", "entry_price": 1.0},
    ]
    monkeypatch.setattr(m, "open_positions_by_strategy", lambda *_a, **_k: positions)
    monkeypatch.setattr(
        policy, "frozen_policy_from_position",
        lambda pos: (_ for _ in ()).throw(ValueError("invalid contract"))
        if pos["id"] == 2 else None,
    )
    for name in (
        "recover_unverified_derisks", "recover_unverified_closes", "recover_orphaned_closes"
    ):
        monkeypatch.setattr(runner, name, lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_safe_alert", lambda *_a, **_k: None)
    priced = []

    runner.real_exit_sweep(
        conn, {}, price_fn=lambda mints: priced.extend(mints) or {},
        now_fn=lambda: "2026-08-30T00:01:00Z",
    )

    assert priced == ["GOOD"]


def test_unparseable_contract_writes_one_durable_audit(monkeypatch):
    conn = _db()
    monkeypatch.setattr(runner, "_safe_alert", lambda *_a, **_k: None)
    runner._UNPARSEABLE_PAGED.clear()
    position = {"id": 7, "mint": "BAD"}

    runner._report_unparseable_contracts(
        conn, [position], "2026-08-30T00:00:00Z", "periodic_sweep")
    runner._report_unparseable_contracts(
        conn, [position], "2026-08-30T00:01:00Z", "periodic_sweep")

    count = conn.execute(
        "SELECT COUNT(*) FROM memecoin_exit_trigger_audit "
        "WHERE position_id=7 AND reason='contract_unparseable'"
    ).fetchone()[0]
    assert count == 1


def test_audit_write_failure_does_not_stop_contract_isolation(monkeypatch):
    conn = _db()
    monkeypatch.setattr(runner, "_safe_alert", lambda *_a, **_k: None)
    monkeypatch.setattr(
        m, "record_exit_trigger_audit",
        lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    runner._report_unparseable_contracts(
        conn, [{"id": 8, "mint": "BAD"}], "2026-08-30T00:00:00Z", "periodic_sweep")
