"""W4 observability tests for per-run logs and memo gates."""

from __future__ import annotations

import json
import sqlite3
import time

from db import init_db
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


WALLET = "0x" + "a" * 40


def _data() -> WalletData:
    return WalletData(
        wallet=WALLET,
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=int(time.time()),
    )


def _report() -> ProtocolReport:
    return ProtocolReport(
        wallet=WALLET,
        analyzed_at=int(time.time()),
        gate_result="reject_insufficient_sample",
        overall_verdict="data_insufficient",
    )


def test_jsonl_emitter_writes_one_object_per_line(tmp_path):
    from sniper.analysis.wallet_protocol.run_events import JsonlEmitter

    path = tmp_path / "run.jsonl"
    emitter = JsonlEmitter(path, run_id="run-1", wallet=WALLET)
    emitter.emit("run_started", {"seed": 42})
    emitter.emit("run_completed", {"rc": 0})

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["run_started", "run_completed"]
    assert all(r["run_id"] == "run-1" for r in rows)
    assert all(r["wallet"] == WALLET for r in rows)
    assert all(isinstance(r["ts"], int) for r in rows)
    assert rows[0]["payload"] == {"seed": 42}


def test_run_wallet_protocol_creates_run_log(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol as mod

    monkeypatch.setattr(mod, "fetch_wallet_data", lambda *a, **kw: _data())
    monkeypatch.setattr(mod, "analyze_wallet", lambda *a, **kw: _report())

    run_log_dir = tmp_path / "runs"
    rc = mod.main([
        WALLET,
        "--run-log-dir",
        str(run_log_dir),
        "--no-tags",
        "--no-peers",
        "--no-exec",
        "--no-history",
        "--no-archetype",
    ])

    assert rc == 0
    logs = list((run_log_dir / WALLET).glob("*.jsonl"))
    assert len(logs) == 1
    events = [json.loads(line)["event"] for line in logs[0].read_text().splitlines()]
    assert events[:3] == ["run_started", "fetch_completed", "analyze_completed"]
    assert events[-1] == "run_completed"


def test_memo_emitter_hard_gates(monkeypatch):
    from sniper.analysis.wallet_protocol.memo_emitter import maybe_emit_memo

    report = _report()
    assert maybe_emit_memo(report, persist_memo=False).status == "skipped_disabled"
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert maybe_emit_memo(report, persist_memo=True).status == "skipped_missing_env"
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert maybe_emit_memo(report, persist_memo=True).status == "skipped_pytest"


def test_persist_memo_missing_env_exits_zero(tmp_path, monkeypatch, capsys):
    import scripts.run_wallet_protocol as mod

    monkeypatch.setattr(mod, "fetch_wallet_data", lambda *a, **kw: _data())
    monkeypatch.setattr(mod, "analyze_wallet", lambda *a, **kw: _report())
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    rc = mod.main([
        WALLET,
        "--persist-memo",
        "--no-tags",
        "--no-peers",
        "--no-exec",
        "--no-history",
        "--no-archetype",
    ])

    assert rc == 0
    assert "memo skipped: missing Supabase env" in capsys.readouterr().err
