from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from sniper.persistence import db_memecoin


ROOT = Path(__file__).resolve().parents[1]


def _require_cluster_feature() -> None:
    from sniper.services.memecoin_cluster_service import ClusterGateClient

    assert ClusterGateClient is not None


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_memecoin.ensure_memecoin_tables(conn)
    return conn


def _event(mint: str = "MINT", network_id: str = "solana"):
    return SimpleNamespace(
        mint=mint,
        network_id=network_id,
        source_user_id="wallet-1",
        source_handle="alice",
        side="buy",
        signal_source="global_feed",
        created_at="2026-08-22T00:00:00Z",
    )


def _decision(reason: str, allowed: bool = False):
    from sniper.services.memecoin_cluster_admission import ClusterDecision

    return ClusterDecision(
        decision_id="decision-1",
        allowed=allowed,
        reason=reason,
        policy_id="solana-funded-cluster-admission-v3",
        snapshot_at="2026-08-22T00:00:00Z",
        completed_at="2026-08-22T00:00:01Z",
        metrics={"attempted_supply": 100},
    )


def test_cluster_block_creates_no_reservation_signature_submission_or_position(monkeypatch) -> None:
    _require_cluster_feature()
    from sniper.services import memecoin_real_leg

    conn = _conn()
    monkeypatch.setattr(memecoin_real_leg, "real_entries_allowed", lambda _path=None: True)
    calls: list[str] = []
    result = memecoin_real_leg.real_entry(
        conn,
        _event(),
        day="2026-08-22",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-08-22T00:00:00Z",
        trade_size=10.0,
        token_safety_fn=lambda _mint: calls.append("safety"),
        execute_fn=lambda *_args, **_kwargs: calls.append("execute"),
        resolved_identity=SimpleNamespace(network_id="solana"),
        cluster_decide_fn=lambda *_args, **_kwargs: _decision("cluster_share_high"),
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "cluster_share_high"
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_cluster_quota_block_creates_no_reservation(monkeypatch) -> None:
    _require_cluster_feature()
    from sniper.services import memecoin_real_leg

    conn = _conn()
    monkeypatch.setattr(memecoin_real_leg, "real_entries_allowed", lambda _path=None: True)
    result = memecoin_real_leg.real_entry(
        conn,
        _event(),
        day="2026-08-22",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-08-22T00:00:00Z",
        trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"),
        execute_fn=lambda *_args, **_kwargs: {"status": "filled"},
        resolved_identity=SimpleNamespace(network_id="solana"),
        cluster_decide_fn=lambda *_args, **_kwargs: _decision("cluster_unknown_quota"),
    )
    assert result["reason"] == "cluster_unknown_quota"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_evm_entry_does_not_call_solana_cluster(monkeypatch) -> None:
    _require_cluster_feature()
    from sniper.services import memecoin_real_leg

    calls: list[str] = []
    monkeypatch.setattr(memecoin_real_leg, "real_entries_allowed", lambda _path=None: True)
    result = memecoin_real_leg.real_entry(
        None,
        _event("0xTOKEN", "4663"),
        day="2026-08-22",
        usdc_balance=0.0,
        sol_balance=0.0,
        now_iso="2026-08-22T00:00:00Z",
        trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"),
        execute_fn=lambda *_args, **_kwargs: {},
        evm_allowlist={"4663"},
        evm_entry_fn=lambda *_args, **_kwargs: {"status": "filled"},
        resolved_identity=SimpleNamespace(network_id="4663"),
        cluster_decide_fn=lambda *_args, **_kwargs: calls.append("cluster"),
    )
    assert result["status"] == "filled"
    assert calls == []


def test_cheap_base_asset_rejection_makes_no_cluster_request(monkeypatch) -> None:
    _require_cluster_feature()
    from sniper.services import memecoin_real_leg

    calls: list[str] = []
    monkeypatch.setattr(memecoin_real_leg, "real_entries_allowed", lambda _path=None: True)
    result = memecoin_real_leg.real_entry(
        _conn(),
        _event("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "solana"),
        day="2026-08-22",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-08-22T00:00:00Z",
        trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"),
        execute_fn=lambda *_args, **_kwargs: {"status": "filled"},
        resolved_identity=SimpleNamespace(network_id="solana"),
        cluster_decide_fn=lambda *_args, **_kwargs: calls.append("cluster"),
    )
    assert result["status"] == "dropped_base_asset"
    assert calls == []


def test_common_funded_solana_path_places_cluster_before_reservation_and_quote() -> None:
    _require_cluster_feature()
    from sniper.services.memecoin_real_leg import real_entry

    source = inspect.getsource(real_entry)
    cluster = source.index("cluster_decide_fn")
    reservation = source.index("reserve_entry(")
    quote_or_execute = source.index("execute_fn(")
    assert cluster < reservation < quote_or_execute


def test_common_gate_has_no_wallet_or_operator_bypass_parameter() -> None:
    _require_cluster_feature()
    from sniper.services.memecoin_real_leg import real_entry

    parameters = inspect.signature(real_entry).parameters
    assert "cluster_decide_fn" in parameters
    assert not {"cluster_bypass", "operator_bypass", "wallet_bypass"} & set(parameters)


def test_all_funded_solana_constructors_use_the_common_entry_path() -> None:
    _require_cluster_feature()
    from sniper.services import memecoin_real_runner

    constructors = {
        "global_feed_follow_and_onchain": memecoin_real_runner.make_real_hook,
        "manual": memecoin_real_runner.make_real_buy_fn,
        "retry": memecoin_real_runner.run_entry_retry_lane,
    }
    expected_calls = {
        "global_feed_follow_and_onchain": "real_entry_hook",
        "manual": "real_entry_hook",
        "retry": "hook",
    }
    for name, constructor in constructors.items():
        assert expected_calls[name] in inspect.getsource(constructor), name


def test_retry_path_does_not_cache_a_cluster_decision() -> None:
    _require_cluster_feature()
    from sniper.services.memecoin_real_runner import run_entry_retry_lane

    source = inspect.getsource(run_entry_retry_lane)
    assert "cluster_decision_id" not in source
    assert "attempt(conn, ev)" in source


def test_cluster_gate_is_default_off(monkeypatch) -> None:
    _require_cluster_feature()
    monkeypatch.delenv("MEMECOIN_REAL_CLUSTER_GATE_ENABLED", raising=False)
    from sniper.settings import memecoin

    assert memecoin.MEMECOIN_REAL_CLUSTER_GATE_ENABLED is False


def test_cluster_outage_is_scoped_to_new_buys_not_exit_code() -> None:
    _require_cluster_feature()
    runner = (ROOT / "sniper" / "services" / "memecoin_real_runner.py").read_text()
    cluster_names = {
        name for name in ("ClusterGateClient", "cluster_decide_fn", "MEMECOIN_REAL_CLUSTER_GATE_ENABLED")
        if name in runner
    }
    assert cluster_names
    for exit_name in ("real_exit_sweep", "make_venue_exit_fn", "make_venue_partial_exit_fn"):
        function = getattr(__import__(
            "sniper.services.memecoin_real_runner", fromlist=[exit_name]
        ), exit_name)
        exit_source = inspect.getsource(function)
        assert "ClusterGateClient" not in exit_source
        assert "cluster_decide_fn" not in exit_source


def test_blocked_gate_reason_cannot_reach_entry_alert_as_a_fill() -> None:
    _require_cluster_feature()
    from sniper.services.memecoin_real_leg import real_entry

    source = inspect.getsource(real_entry)
    assert '"cluster_decision_id"' in source
    assert '"status": "rejected"' in source
    assert source.index('"status": "rejected"') < source.index("execute_fn(")
