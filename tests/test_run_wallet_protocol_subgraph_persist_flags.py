"""C2 acceptance: subgraph fallback truth reaches analyzer_runs."""

from __future__ import annotations

import sqlite3
import time

from db import init_db
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _data(*, subgraph_fallback_used: bool) -> WalletData:
    return WalletData(
        wallet="0xfeedface",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
        subgraph_fallback_used=subgraph_fallback_used,
    )


def _report() -> ProtocolReport:
    return ProtocolReport(
        wallet="0xfeedface",
        analyzed_at=int(time.time()),
        gate_result="reject_insufficient_sample",
        overall_verdict="data_insufficient",
    )


def _run(tmp_path, monkeypatch, *, subgraph_fallback_used: bool) -> int:
    db_path = tmp_path / "sandbox.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.close()
    monkeypatch.setenv("SNIPER_DB_PATH", str(db_path))

    import scripts.run_wallet_protocol as mod

    monkeypatch.setattr(
        mod,
        "fetch_wallet_data",
        lambda *a, **kw: _data(subgraph_fallback_used=subgraph_fallback_used),
    )
    monkeypatch.setattr(mod, "analyze_wallet", lambda *a, **kw: _report())

    rc = mod.main([
        "0xfeedface",
        "--persist",
        "--no-tags",
        "--no-peers",
        "--no-exec",
        "--no-history",
        "--no-archetype",
    ])
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(
            "SELECT subgraph_fallback_used FROM analyzer_runs"
        ).fetchone()[0])
    finally:
        conn.close()


def test_subgraph_fallback_persisted_true(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, subgraph_fallback_used=True) == 1


def test_subgraph_fallback_persisted_false(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, subgraph_fallback_used=False) == 0
