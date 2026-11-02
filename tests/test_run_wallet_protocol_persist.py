"""Smoke test: ``run_wallet_protocol --persist`` writes one analyzer_runs row
plus one live wallet_layer5_decision row, fully atomic. Uses a hand-built
ProtocolReport fixture so the test does not hit live APIs.

Phase 3 Workstream A Task A6.
"""
from __future__ import annotations

import sqlite3
import time
import json

import pytest

from db import init_db
from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    ProtocolReport,
    StationarityResult,
    WalletData,
)


def _fake_protocol_report(wallet: str = "0xfeedface") -> ProtocolReport:
    """Hand-built ProtocolReport that maps to validated_copy_candidate / high."""
    branch = BranchReport(
        branch="held_analyst",
        is_active=True,
        hypotheses=[
            HypothesisResult(
                name="H1",
                verdict="supported",
                n=80,
                ci_lower=0.05,
                ci_upper=0.30,
            )
        ],
        stationarity=StationarityResult(
            cusum_status="calibrated",
            h_used=25.0,
            k=0.5,
            signaled=False,
            signal_index=None,
            pre_regime_n=60,
            post_regime_n=20,
        ),
    )
    report = ProtocolReport(
        wallet=wallet,
        analyzed_at=int(time.time()),
        n_closed_positions=87,
        n_held_to_resolution=64,
        net_pnl=12345.0,
        gate_result="pass",
        trading_mode="held_analyst_only",
        overall_verdict="edge_detected",
        primary_edge_type="entry_timing",
        held_analyst_branch=branch,
    )
    report.capital_ledger = CapitalLedger(
        wallet=wallet,
        n_splits=3,
        n_merges=1,
        n_redemptions=2,
        total_deposits_usdc=10_000.0,
        total_withdrawals_usdc=2_000.0,
        total_redemption_payout_usdc=500.0,
        net_capital_funded_usdc=8_000.0,
    )
    return report


def _no_fetch(*args, **kwargs) -> WalletData:
    """Stand-in for fetch_wallet_data — returns minimal WalletData. The test
    does not exercise fetch; it exercises persist plumbing."""
    return WalletData(
        wallet="0xfeedface",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="directional_discretionary",
        execution_profile=None,
        fetched_at=int(time.time()),
    )


def _open_persisted_db(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_persist_flag_writes_atomic_run_and_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "sandbox.db"
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod

    # Patch BOTH fetch and analyze so the CLI never hits live APIs.
    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)
    monkeypatch.setattr(
        mod, "analyze_wallet", lambda *a, **kw: _fake_protocol_report()
    )

    rc = mod.main(["0xfeedface", "--persist", "--no-tags", "--no-peers", "--no-exec",
                   "--no-history", "--no-archetype"])
    assert rc == 0

    conn = _open_persisted_db(db_path)
    try:
        runs = list(conn.execute("SELECT * FROM analyzer_runs"))
        decisions = list(
            conn.execute(
                "SELECT * FROM wallet_layer5_decision WHERE is_current = 1"
            )
        )
        assert len(runs) == 1
        assert runs[0]["success"] == 1
        assert len(decisions) == 1
        assert decisions[0]["analyzer_run_id"] == runs[0]["run_id"]
        assert decisions[0]["action_decision"] == "validated_copy_candidate"
        assert decisions[0]["confidence_band"] == "high"
        assert decisions[0]["wallet"] == "0xfeedface"
        capital = json.loads(decisions[0]["capital_ledger_summary_json"])
        assert capital["net_capital_funded_usdc"] == 8_000.0
        assert capital["n_splits"] == 3
    finally:
        conn.close()


def test_persist_flag_records_failure_with_no_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "sandbox.db"
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod
    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(mod, "analyze_wallet", _boom)

    rc = mod.main(["0xfeedface", "--persist", "--no-tags", "--no-peers", "--no-exec",
                   "--no-history", "--no-archetype"])
    assert rc == 1

    conn = _open_persisted_db(db_path)
    try:
        runs = list(conn.execute("SELECT * FROM analyzer_runs"))
        decisions = list(conn.execute("SELECT * FROM wallet_layer5_decision"))
        assert len(runs) == 1
        assert runs[0]["success"] == 0
        assert runs[0]["error_class"] == "RuntimeError"
        assert len(decisions) == 0
    finally:
        conn.close()


def test_fetch_failure_recorded_as_failed_run(tmp_path, monkeypatch):
    """Fetch raising pre-analyze must still produce a success=0 audit row.
    Catches the regression where only analyze_wallet was patched."""
    db_path = tmp_path / "sandbox.db"
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod

    def _fetch_boom(*args, **kwargs):
        raise ConnectionError("simulated fetch failure")

    monkeypatch.setattr(mod, "fetch_wallet_data", _fetch_boom)

    rc = mod.main(["0xfeedface", "--persist", "--no-tags", "--no-peers", "--no-exec",
                   "--no-history", "--no-archetype"])
    assert rc == 1

    conn = _open_persisted_db(db_path)
    try:
        runs = list(conn.execute("SELECT * FROM analyzer_runs"))
        assert len(runs) == 1
        assert runs[0]["success"] == 0
        assert runs[0]["error_class"] == "ConnectionError"
    finally:
        conn.close()


def test_persist_flag_off_writes_nothing(tmp_path, monkeypatch):
    """Without --persist, no DB writes occur even on success."""
    db_path = tmp_path / "sandbox.db"
    c = sqlite3.connect(str(db_path))
    init_db(c)
    c.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod
    monkeypatch.setattr(mod, "fetch_wallet_data", _no_fetch)
    monkeypatch.setattr(
        mod, "analyze_wallet", lambda *a, **kw: _fake_protocol_report()
    )

    rc = mod.main(["0xfeedface", "--no-tags", "--no-peers", "--no-exec",
                   "--no-history", "--no-archetype"])
    assert rc == 0

    conn = _open_persisted_db(db_path)
    try:
        runs = list(conn.execute("SELECT * FROM analyzer_runs"))
        decisions = list(conn.execute("SELECT * FROM wallet_layer5_decision"))
        assert len(runs) == 0
        assert len(decisions) == 0
    finally:
        conn.close()
