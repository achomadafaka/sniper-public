"""Progressive batch runner tests.

Locks the Phase F direction change: one wallet finishes -> DB status and
human summary are visible immediately, while the batch continues.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
import types

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


def _init_db(db_path) -> None:
    conn = _connect(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()


def _persist_decision(
    db_path,
    wallet: str,
    *,
    action_decision: str = "validated_copy_candidate",
    confidence_band: str = "high",
    overall_verdict: str = "edge_detected",
    primary_edge_type: str = "entry_timing",
) -> str:
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
                dw_roi_point=0.21,
                ew_roi_point=0.09,
                gate_result="pass",
                trading_mode="held_analyst_only",
                overall_verdict=overall_verdict,
                primary_edge_type=primary_edge_type,
                activity_cap_hit=True,
                archetype_layer1="directional_discretionary",
            ),
            decision=Layer5Decision(
                action_decision,
                ["edge_detected", "primary_edge_type:entry_timing"],
                confidence_band,
            ),
            started_ts=now,
            finished_ts=now,
            duration_ms=100,
            invocation_source="test",
            args_json="{}",
            fetch_quality_flags_json="[]",
            activity_rows_fetched=120,
            activity_cap_hit=True,
            subgraph_fallback_used=False,
            report_markdown="# report\n\nstored decision report",
        )
    finally:
        conn.close()


def _items(db_path):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM wallet_analysis_batch_item ORDER BY wallet"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _intake_rows(db_path):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM wallet_analyzer_intake_queue ORDER BY wallet"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_action_bucket_mapping_locked():
    import scripts.run_wallet_protocol_batch as mod

    assert mod.action_bucket_for_decision("validated_copy_candidate") == "actionable_now"
    assert mod.action_bucket_for_decision("reverse_engineer_candidate") == "review"
    assert mod.action_bucket_for_decision("study") == "low_confidence"
    assert mod.action_bucket_for_decision("monitor") == "low_confidence"
    assert mod.action_bucket_for_decision("exclude_for_copy") == "no_action"
    assert mod.action_bucket_for_decision("anti_pattern") == "no_action"


def test_manual_wallets_record_source_metadata_and_print_summary(
    tmp_path, monkeypatch, capsys,
):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    calls: list[str] = []

    def _fake_run(wallet, *, db_path, single_wallet_args):
        calls.append(wallet)
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--batch-id", "batch-success",
        "--wallet", WALLET_A.upper(),
        "--wallet", WALLET_B.upper(),
        "--source", "manual",
        "--source-ref", "operator-drop",
        "--metadata-json", '{"ticket":"q7d2m5"}',
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_A, WALLET_B]
    items = _items(db_path)
    assert [r["status"] for r in items] == ["completed", "completed"]
    assert [r["action_bucket"] for r in items] == [
        "actionable_now", "actionable_now",
    ]
    assert all(r["analyzer_run_id"] for r in items)
    assert {r["source"] for r in items} == {"manual"}
    assert {r["source_ref"] for r in items} == {"operator-drop"}
    assert all('"ticket": "q7d2m5"' in r["metadata_json"] for r in items)
    assert all("bucket: actionable_now" in r["summary_markdown"] for r in items)

    out = capsys.readouterr().out
    assert f"WALLET {WALLET_A}" in out
    assert "bucket: actionable_now" in out
    assert "aggregate by status" in out


def test_wallet_file_ignores_comments_dedupes_and_preserves_order(
    tmp_path, monkeypatch,
):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    wallet_file = tmp_path / "wallets.txt"
    wallet_file.write_text(
        f"\n# comment\n{WALLET_B.upper()}\n{WALLET_A}\n{WALLET_B}\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def _fake_run(wallet, *, db_path, single_wallet_args):
        calls.append(wallet)
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--batch-id", "batch-file",
        "--wallet-file", str(wallet_file),
        "--source", "import_file",
        "--source-ref", str(wallet_file),
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_B, WALLET_A]
    assert {r["source"] for r in _items(db_path)} == {"import_file"}


def test_run_single_wallet_protocol_uses_subprocess_timeout(monkeypatch, tmp_path, capsys):
    import scripts.run_wallet_protocol_batch as mod

    calls = {}

    def fake_run(cmd, *, cwd, env, capture_output, text, timeout):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        calls["capture_output"] = capture_output
        calls["text"] = text
        calls["timeout"] = timeout
        return types.SimpleNamespace(
            returncode=0,
            stdout="child stdout\n",
            stderr="child stderr\n",
        )

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_single_wallet_protocol(
        WALLET_A,
        db_path=str(tmp_path / "batch.db"),
        single_wallet_args=["--no-tags"],
        timeout_seconds=12.5,
    )

    assert rc == 0
    assert calls["timeout"] == 12.5
    assert calls["env"]["SNIPER_DB_PATH"] == str(tmp_path / "batch.db")
    assert calls["cmd"][-1] == "--no-tags"
    assert "run_wallet_protocol.py" in calls["cmd"][1]
    out = capsys.readouterr()
    assert "child stdout" in out.out
    assert "child stderr" in out.err


def test_run_single_wallet_protocol_timeout_raises_timeout_error(monkeypatch, tmp_path):
    import scripts.run_wallet_protocol_batch as mod

    def fake_run(cmd, *, cwd, env, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    try:
        mod._run_single_wallet_protocol(
            WALLET_A,
            db_path=str(tmp_path / "batch.db"),
            single_wallet_args=[],
            timeout_seconds=0.01,
        )
    except TimeoutError as exc:
        assert "timed out after 0.01s" in str(exc)
    else:
        raise AssertionError("expected TimeoutError")


def test_per_wallet_timeout_marks_wallet_failed_and_continues(
    tmp_path, monkeypatch, capsys,
):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    calls: list[tuple[str, float | None]] = []

    def _fake_run(wallet, *, db_path, single_wallet_args, timeout_seconds):
        calls.append((wallet, timeout_seconds))
        if wallet == WALLET_B:
            raise TimeoutError("wallet analysis timed out after 0.01s")
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--batch-id", "batch-timeout",
        "--wallet", WALLET_A,
        "--wallet", WALLET_B,
        "--wallet", WALLET_C,
        "--per-wallet-timeout-s", "0.01",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 1
    assert calls == [(WALLET_A, 0.01), (WALLET_B, 0.01), (WALLET_C, 0.01)]
    rows = {r["wallet"]: r for r in _items(db_path)}
    assert rows[WALLET_A]["status"] == "completed"
    assert rows[WALLET_B]["status"] == "failed"
    assert rows[WALLET_B]["action_bucket"] == "failed"
    assert rows[WALLET_B]["error_class"] == "TimeoutError"
    assert rows[WALLET_B]["error_message"] == "wallet analysis timed out after 0.01s"
    assert rows[WALLET_C]["status"] == "completed"
    out = capsys.readouterr().out
    assert f"FAILED {WALLET_B} TimeoutError: wallet analysis timed out after 0.01s" in out


def test_invalid_wallet_input_fails_before_analysis(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_run_single_wallet_protocol",
        lambda wallet, *, db_path, single_wallet_args: calls.append(wallet) or 0,
    )

    rc = mod.main([
        "--db", str(db_path),
        "--wallet", "0xnothex",
    ])

    assert rc == 2
    assert calls == []
    assert _items(db_path) == []


def test_no_implicit_fallback_to_followed_or_watchlist(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS followed_wallets "
            "(wallet TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS watchlist_wallets "
            "(wallet TEXT, added_ts INTEGER)"
        )
        conn.execute(
            "INSERT INTO followed_wallets (wallet, status) VALUES (?, 'active')",
            (WALLET_A,),
        )
        conn.execute(
            "INSERT INTO watchlist_wallets (wallet, added_ts) VALUES (?, 1)",
            (WALLET_B,),
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []
    monkeypatch.setattr(
        mod,
        "_run_single_wallet_protocol",
        lambda wallet, *, db_path, single_wallet_args: calls.append(wallet) or 0,
    )

    rc = mod.main(["--db", str(db_path), "--batch-id", "no-fallback"])

    assert rc == 2
    assert calls == []
    assert _items(db_path) == []


def test_from_queue_claims_new_analyzer_queue_only_by_priority(
    tmp_path, monkeypatch,
):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        mod.enqueue_intake_wallet(
            conn,
            wallet=WALLET_A,
            source="manual",
            source_ref="low-priority",
            priority=10,
            metadata_json='{"rank":3}',
            notes="keep",
        )
        mod.enqueue_intake_wallet(
            conn,
            wallet=WALLET_B,
            source="discovery_agent",
            source_ref="agent-1",
            priority=50,
            metadata_json='{"rank":1}',
        )
        mod.enqueue_intake_wallet(
            conn,
            wallet=WALLET_C,
            source="api",
            source_ref="api-1",
            priority=20,
            metadata_json='{"rank":2}',
        )
        conn.execute(
            """
            CREATE TABLE wallet_analysis_queue (
                wallet TEXT PRIMARY KEY,
                priority_score INTEGER NOT NULL,
                first_event_ts INTEGER NOT NULL,
                last_event_ts INTEGER NOT NULL,
                state TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO wallet_analysis_queue VALUES (?, 9999, 1, 1, 'queued')",
            ("0x" + "d" * 40,),
        )
        conn.execute(
            """
            CREATE TABLE discovery_events (
                wallet TEXT,
                analysis_status TEXT,
                quick_score INTEGER,
                detected_at INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO discovery_events VALUES (?, 'scored', 9999, 1)",
            ("0x" + "e" * 40,),
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS candidate_users "
            "(wallet TEXT PRIMARY KEY, first_seen_ts INTEGER)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO candidate_users "
            "(wallet, first_seen_ts, last_seen_ts) VALUES (?, 1, 1)",
            ("0x" + "f" * 40,),
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS low_buy_trades "
            "(transaction_hash TEXT PRIMARY KEY, proxy_wallet TEXT NOT NULL, "
            "condition_id TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, "
            "timestamp INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO low_buy_trades "
            "(transaction_hash, proxy_wallet, condition_id, side, price, timestamp) "
            "VALUES ('tx_legacy_batch_guard', ?, 'cond', 'BUY', 0.1, 1)",
            ("0x" + "1" * 40,),
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
        "--from-queue",
        "--limit", "2",
        "--batch-id", "queue-batch",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_B, WALLET_C]
    rows = {r["wallet"]: r for r in _intake_rows(db_path)}
    assert rows[WALLET_A]["status"] == "pending"
    assert rows[WALLET_B]["status"] == "completed"
    assert rows[WALLET_C]["status"] == "completed"

    items = {r["wallet"]: r for r in _items(db_path)}
    assert items[WALLET_B]["source"] == "discovery_agent"
    assert items[WALLET_B]["source_ref"] == "agent-1"
    assert items[WALLET_B]["intake_queue_id"] == rows[WALLET_B]["id"]
    assert items[WALLET_C]["source"] == "api"

    conn = _connect(db_path)
    try:
        legacy = conn.execute("SELECT * FROM wallet_analysis_queue").fetchone()
        assert legacy["state"] == "queued"
        discovery = conn.execute("SELECT * FROM discovery_events").fetchone()
        assert discovery["analysis_status"] == "scored"
        candidate = conn.execute("SELECT * FROM candidate_users").fetchone()
        assert candidate["wallet"] == "0x" + "f" * 40
        low_buy = conn.execute("SELECT * FROM low_buy_trades").fetchone()
        assert low_buy["proxy_wallet"] == "0x" + "1" * 40
    finally:
        conn.close()


def test_from_queue_failure_mirrors_to_intake_queue(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        mod.enqueue_intake_wallet(
            conn,
            wallet=WALLET_B,
            source="manual",
            source_ref="operator",
            priority=1,
            metadata_json="{}",
        )
    finally:
        conn.close()

    def _fail(wallet, *, db_path, single_wallet_args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fail)

    rc = mod.main([
        "--db", str(db_path),
        "--from-queue",
        "--limit", "1",
        "--batch-id", "queue-fail",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 1
    queue_row = _intake_rows(db_path)[0]
    assert queue_row["status"] == "failed"
    assert queue_row["attempt_count"] == 1
    assert queue_row["error_class"] == "RuntimeError"
    assert queue_row["error_message"] == "synthetic failure"
    item = _items(db_path)[0]
    assert item["status"] == "failed"
    assert item["intake_queue_id"] == queue_row["id"]


def test_failed_wallet_does_not_stop_next_wallet(tmp_path, monkeypatch, capsys):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    calls: list[str] = []

    def _fake_run(wallet, *, db_path, single_wallet_args):
        calls.append(wallet)
        if wallet == WALLET_B:
            raise RuntimeError("synthetic failure")
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--batch-id", "batch-fail",
        "--wallet", WALLET_A,
        "--wallet", WALLET_B,
        "--wallet", WALLET_C,
        "--heartbeat-seconds", "60",
    ])

    assert rc == 1
    assert calls == [WALLET_A, WALLET_B, WALLET_C]
    rows = {r["wallet"]: r for r in _items(db_path)}
    assert rows[WALLET_A]["status"] == "completed"
    assert rows[WALLET_B]["status"] == "failed"
    assert rows[WALLET_B]["action_bucket"] == "failed"
    assert rows[WALLET_B]["error_class"] == "RuntimeError"
    assert rows[WALLET_C]["status"] == "completed"

    out = capsys.readouterr().out
    assert f"FAILED {WALLET_B} RuntimeError: synthetic failure" in out
    assert "completed: 2" in out
    assert "failed: 1" in out


def test_retry_failed_reprocesses_only_failed_and_increments_attempt(
    tmp_path, monkeypatch,
):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        mod.create_batch(conn, batch_id="batch-retry", source="test", total_wallets=2)
        mod.queue_batch_items(
            conn,
            "batch-retry",
            [
                mod.BatchWallet(WALLET_A, "manual", "", "{}", None),
                mod.BatchWallet(WALLET_B, "manual", "", "{}", None),
            ],
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item "
            "SET status='completed' WHERE batch_id='batch-retry' AND wallet=?",
            (WALLET_A,),
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item "
            "SET status='failed', action_bucket='failed', attempt_count=1, "
            "error_class='RuntimeError', error_message='old failure' "
            "WHERE batch_id='batch-retry' AND wallet=?",
            (WALLET_B,),
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []

    def _fake_run(wallet, *, db_path, single_wallet_args):
        calls.append(wallet)
        _persist_decision(db_path, wallet, action_decision="reverse_engineer_candidate")
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fake_run)

    rc = mod.main([
        "--db", str(db_path),
        "--retry-failed",
        "--batch-id", "batch-retry",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_B]
    rows = {r["wallet"]: r for r in _items(db_path)}
    assert rows[WALLET_A]["status"] == "completed"
    assert rows[WALLET_B]["status"] == "completed"
    assert rows[WALLET_B]["attempt_count"] == 2
    assert rows[WALLET_B]["action_bucket"] == "review"
    assert rows[WALLET_B]["error_class"] is None
    assert rows[WALLET_B]["error_message"] is None


def test_running_wallet_heartbeat_updates(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)

    def _slow_success(wallet, *, db_path, single_wallet_args):
        time.sleep(0.05)
        _persist_decision(db_path, wallet)
        return 0

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _slow_success)

    rc = mod.main([
        "--db", str(db_path),
        "--batch-id", "batch-heartbeat",
        "--wallet", WALLET_A,
        "--heartbeat-seconds", "0.01",
    ])

    assert rc == 0
    row = _items(db_path)[0]
    assert row["status"] == "completed"
    assert row["heartbeat_ts"] is not None
    assert row["heartbeat_ts"] >= row["started_ts"]


def test_pause_batch_resets_unfinished_queue_items_to_pending(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        intake_a = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_A, source="manual", priority=1
        )
        intake_b = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_B, source="manual", priority=1
        )
        intake_c = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_C, source="manual", priority=1
        )
        mod.create_batch(conn, batch_id="batch-pause", source="test", total_wallets=3)
        mod.queue_batch_items(
            conn,
            "batch-pause",
            [
                mod.BatchWallet(WALLET_A, "manual", "", "{}", intake_a),
                mod.BatchWallet(WALLET_B, "manual", "", "{}", intake_b),
                mod.BatchWallet(WALLET_C, "manual", "", "{}", intake_c),
            ],
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='completed' "
            "WHERE batch_id='batch-pause' AND wallet=?",
            (WALLET_A,),
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='running' "
            "WHERE batch_id='batch-pause' AND wallet=?",
            (WALLET_B,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='completed' WHERE id=?",
            (intake_a,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='running' WHERE id=?",
            (intake_b,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='claimed' WHERE id=?",
            (intake_c,),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        mod,
        "_run_single_wallet_protocol",
        lambda wallet, *, db_path, single_wallet_args: (_ for _ in ()).throw(
            AssertionError("pause must not analyze wallets")
        ),
    )

    rc = mod.main([
        "--db", str(db_path),
        "--pause-batch",
        "--batch-id", "batch-pause",
    ])

    assert rc == 0
    items = {r["wallet"]: r for r in _items(db_path)}
    assert items[WALLET_A]["status"] == "completed"
    assert items[WALLET_B]["status"] == "skipped"
    assert items[WALLET_B]["error_class"] == "BatchPaused"
    assert items[WALLET_C]["status"] == "skipped"
    assert items[WALLET_C]["error_class"] == "BatchPaused"
    intake = {r["wallet"]: r for r in _intake_rows(db_path)}
    assert intake[WALLET_A]["status"] == "completed"
    assert intake[WALLET_B]["status"] == "pending"
    assert intake[WALLET_C]["status"] == "pending"


def test_resume_batch_reprocesses_only_paused_items(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        intake_a = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_A, source="manual", priority=1
        )
        intake_b = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_B, source="manual", priority=1
        )
        mod.create_batch(conn, batch_id="batch-resume", source="test", total_wallets=2)
        mod.queue_batch_items(
            conn,
            "batch-resume",
            [
                mod.BatchWallet(WALLET_A, "manual", "", "{}", intake_a),
                mod.BatchWallet(WALLET_B, "manual", "", "{}", intake_b),
            ],
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='completed' "
            "WHERE batch_id='batch-resume' AND wallet=?",
            (WALLET_A,),
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='skipped', "
            "error_class='BatchPaused', error_message='paused' "
            "WHERE batch_id='batch-resume' AND wallet=?",
            (WALLET_B,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='completed' WHERE id=?",
            (intake_a,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='pending' WHERE id=?",
            (intake_b,),
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
        "--batch-id", "batch-resume",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 0
    assert calls == [WALLET_B]
    items = {r["wallet"]: r for r in _items(db_path)}
    assert items[WALLET_A]["status"] == "completed"
    assert items[WALLET_B]["status"] == "completed"
    intake = {r["wallet"]: r for r in _intake_rows(db_path)}
    assert intake[WALLET_A]["status"] == "completed"
    assert intake[WALLET_B]["status"] == "completed"


def test_cancel_batch_marks_unfinished_queue_items_skipped(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        intake_a = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_A, source="manual", priority=1
        )
        intake_b = mod.enqueue_intake_wallet(
            conn, wallet=WALLET_B, source="manual", priority=1
        )
        mod.create_batch(conn, batch_id="batch-cancel", source="test", total_wallets=2)
        mod.queue_batch_items(
            conn,
            "batch-cancel",
            [
                mod.BatchWallet(WALLET_A, "manual", "", "{}", intake_a),
                mod.BatchWallet(WALLET_B, "manual", "", "{}", intake_b),
            ],
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='completed' "
            "WHERE batch_id='batch-cancel' AND wallet=?",
            (WALLET_A,),
        )
        conn.execute(
            "UPDATE wallet_analysis_batch_item SET status='running' "
            "WHERE batch_id='batch-cancel' AND wallet=?",
            (WALLET_B,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='completed' WHERE id=?",
            (intake_a,),
        )
        conn.execute(
            "UPDATE wallet_analyzer_intake_queue SET status='running' WHERE id=?",
            (intake_b,),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        mod,
        "_run_single_wallet_protocol",
        lambda wallet, *, db_path, single_wallet_args: (_ for _ in ()).throw(
            AssertionError("cancel must not analyze wallets")
        ),
    )

    rc = mod.main([
        "--db", str(db_path),
        "--cancel-batch",
        "--batch-id", "batch-cancel",
    ])

    assert rc == 0
    items = {r["wallet"]: r for r in _items(db_path)}
    assert items[WALLET_A]["status"] == "completed"
    assert items[WALLET_B]["status"] == "skipped"
    assert items[WALLET_B]["error_class"] == "BatchCancelled"
    intake = {r["wallet"]: r for r in _intake_rows(db_path)}
    assert intake[WALLET_A]["status"] == "completed"
    assert intake[WALLET_B]["status"] == "skipped"
