"""W4 resume idempotency: completed wallets are not re-run."""

from __future__ import annotations

import sqlite3
import time

from db import init_db
from sniper.analysis.wallet_protocol.persistence import (
    persist_successful_run_with_decision,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import Layer5Decision

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _items(db_path):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM wallet_analysis_batch_item ORDER BY wallet"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _persist_decision(db_path, wallet: str) -> str:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        return persist_successful_run_with_decision(
            conn,
            pr=ProtocolReport(
                wallet=wallet,
                analyzed_at=now,
                n_closed_positions=87,
                n_held_to_resolution=64,
                net_pnl=12345.0,
                gate_result="pass",
                trading_mode="held_analyst_only",
                overall_verdict="edge_detected",
                primary_edge_type="entry_timing",
            ),
            decision=Layer5Decision(
                "validated_copy_candidate",
                ["edge_detected"],
                "high",
            ),
            started_ts=now,
            finished_ts=now,
            duration_ms=100,
            invocation_source="test",
            args_json="{}",
            fetch_quality_flags_json="[]",
            activity_rows_fetched=120,
            activity_cap_hit=False,
            subgraph_fallback_used=False,
            report_markdown="# report",
        )
    finally:
        conn.close()


def test_resume_does_not_duplicate_completed_analyzer_runs(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    conn = _connect(db_path)
    try:
        init_db(conn)
        mod.create_batch(conn, batch_id="resume-idempotent", source="test", total_wallets=3)
        mod.queue_batch_items(
            conn,
            "resume-idempotent",
            [
                mod.BatchWallet(WALLET_A, "manual", "", "{}", None),
                mod.BatchWallet(WALLET_B, "manual", "", "{}", None),
                mod.BatchWallet(WALLET_C, "manual", "", "{}", None),
            ],
        )
        run_id = _persist_decision(db_path, WALLET_A)
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='completed', analyzer_run_id=? "
            "WHERE batch_id='resume-idempotent' AND wallet=?",
            (run_id, WALLET_A),
        )
        for wallet in (WALLET_B, WALLET_C):
            conn.execute(
                "UPDATE wallet_analysis_batch_item SET status='skipped', "
                "error_class='BatchPaused', error_message='paused' "
                "WHERE batch_id='resume-idempotent' AND wallet=?",
                (wallet,),
            )
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []

    def _fake_run(wallet, *, db_path, single_wallet_args):
        calls.append(wallet)
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--resume-batch",
        "--batch-id", "resume-idempotent",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_B, WALLET_C]
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM analyzer_runs WHERE wallet=?",
            (WALLET_A,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert {r["wallet"]: r["status"] for r in _items(db_path)} == {
        WALLET_A: "completed",
        WALLET_B: "completed",
        WALLET_C: "completed",
    }
