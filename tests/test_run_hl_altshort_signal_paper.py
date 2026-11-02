import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sniper.hyperlive import intel_store as IS


HOUR = 3_600_000


def _runner():
    spec = importlib.util.spec_from_file_location(
        "run_hl_altshort_signal_paper", "scripts/run_hl_altshort_signal_paper.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _setup(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    policy = {
        "alt_short_only": True, "cost_bps": 39.0, "slots": 8,
        "runup_threshold": 0.30, "runup_lookback_hours": 72,
        "history_hours": 720, "min_coverage": 0.80,
        "bearish_threshold_bps": -500.0, "bullish_threshold_bps": 500.0,
        "stop_multiple": 1.20, "take_profit_multiple": 0.85, "cap_hours": 8,
    }
    arm = IS.create_altshort_arm(
        conn,
        {"arm_id": "standalone-v1", "policy": policy,
         "targets": ["standalone-exhaustion"], "taxonomy_version": "alt-v1",
         "benchmark_version": "loo-v1", "regime_version": "causal-v1"},
        activated_ts_ms=-HOUR, code_sha="abc", created_ts_ms=-HOUR,
    )
    coins = ["AAA"] + [f"B{i}" for i in range(8)]
    rows = []
    for offset in range(-721, 1):
        for coin in coins:
            if offset == -1:
                px = 120.0 if coin == "AAA" else 100.0
            elif offset == 0:
                px = 131.0 if coin == "AAA" else 80.0
            else:
                px = 100.0
            rows.append((offset * HOUR, coin, px, px, px, px, 0.0))
    for coin in coins:
        px = 129.0 if coin == "AAA" else 79.0
        rows.append((HOUR, coin, px, px, px, px, 0.0))
    for i in range(5):
        rows.append((0, f"NEW{i}", 100, 100, 100, 100, 0.0))
        rows.append((HOUR, f"NEW{i}", 100, 100, 100, 100, 0.0))
    conn.executemany(
        "INSERT INTO hl_altshort_regime_marks("
        "ts_ms,coin,mark,open_mark,high_mark,low_mark,funding_rate) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn, arm


def test_cycle_detects_crossing_then_enters_at_next_hour_open(tmp_path):
    runner = _runner()
    conn, arm = _setup(tmp_path)

    first = runner._cycle(conn, arm=arm, now_ms=HOUR + 1_000)
    assert first == {"decisions": 1, "opened": 0, "closed": 0, "rejected": 0}
    pending = IS.list_altshort_signals(conn, arm_id="standalone-v1", status="pending")
    assert [row["coin"] for row in pending] == ["AAA"]
    assert json.loads(pending[0]["regime_json"])["label"] == "bearish"

    second = runner._cycle(conn, arm=arm, now_ms=HOUR + 2_000)
    assert second["opened"] == 1
    opened = IS.list_altshort_signals(conn, arm_id="standalone-v1", status="open")[0]
    assert opened["entry_ts_ms"] == HOUR
    assert opened["entry_mark"] == 129.0
    assert "AAA" not in json.loads(opened["benchmark_entry_json"])

    third = runner._cycle(conn, arm=arm, now_ms=HOUR + 3_000)
    assert third["decisions"] == 0
    assert len(IS.list_altshort_signals(conn, arm_id="standalone-v1")) == 1


def test_cycle_persists_every_processed_regime_hour_without_a_crossing(tmp_path):
    runner = _runner()
    conn, arm = _setup(tmp_path)
    runner._cycle(conn, arm=arm, now_ms=HOUR + 1_000)
    runner._cycle(conn, arm=arm, now_ms=2 * HOUR + 1_000)

    regimes = IS.list_altshort_signal_regimes(conn, arm_id="standalone-v1")
    assert [row["decision_ts_ms"] for row in regimes] == [0, HOUR]
    decisions = IS.list_altshort_signals(conn, arm_id="standalone-v1")
    assert len(decisions) == 1
    assert json.loads(decisions[0]["regime_json"])["episode_id"] == 1


def test_cycle_closes_time_cap_with_complete_funding_and_benchmark(tmp_path):
    runner = _runner()
    conn, arm = _setup(tmp_path)
    runner._cycle(conn, arm=arm, now_ms=HOUR + 1_000)
    runner._cycle(conn, arm=arm, now_ms=HOUR + 2_000)
    coins = ["AAA"] + [f"B{i}" for i in range(8)]
    for offset in range(2, 10):
        rows = []
        for coin in coins:
            px = 120.0 if coin == "AAA" else 75.0
            rows.append((offset * HOUR, coin, px, px, px, px, 0.0))
        conn.executemany(
            "INSERT INTO hl_altshort_regime_marks("
            "ts_ms,coin,mark,open_mark,high_mark,low_mark,funding_rate) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    conn.commit()

    result = runner._cycle(conn, arm=arm, now_ms=9 * HOUR + 1_000)
    assert result["closed"] == 1
    closed = IS.list_altshort_signals(conn, arm_id="standalone-v1", status="closed")[0]
    assert closed["exit_reason"] == "time_cap"
    assert closed["net_bps"] == pytest.approx((129 - 120) / 129 * 10_000 - 39)
    assert closed["provenance_status"] == "valid"
    assert json.loads(closed["benchmark_exit_json"])["valid"] is True


def test_cycle_admits_eight_simultaneous_pending_signals_by_coin(tmp_path):
    runner = _runner()
    conn, arm = _setup(tmp_path)
    coins = [f"S{i}" for i in range(9)]
    for coin in reversed(coins):
        IS.record_altshort_signal_decision(
            conn, arm_id="standalone-v1", coin=coin, decision_ts_ms=0,
            decision_mark=130, runup=0.30, regime={"label": "bearish"},
            benchmark_entry={}, gated_in=True,
        )
        conn.execute(
            "INSERT INTO hl_altshort_regime_marks("
            "ts_ms,coin,mark,open_mark,high_mark,low_mark,funding_rate) "
            "VALUES (?,?,?,?,?,?,?)",
            (HOUR, coin, 129, 129, 129, 129, 0.0),
        )
    conn.commit()

    result = runner._cycle(conn, arm=arm, now_ms=HOUR + 1_000)
    assert result["opened"] == 8 and result["rejected"] == 1
    assert [row["coin"] for row in IS.list_altshort_signals(
        conn, arm_id="standalone-v1", status="open"
    )] == coins[:8]
    assert IS.list_altshort_signals(
        conn, arm_id="standalone-v1", status="rejected"
    )[0]["coin"] == "S8"


def test_runner_is_db_only():
    source = Path("scripts/run_hl_altshort_signal_paper.py").read_text(encoding="utf-8")
    assert "urllib" not in source
    assert "requests" not in source
    assert "hyperliquid_api" not in source


def test_heartbeat_proves_clean_identity_frontier_and_position_counts(tmp_path):
    runner = _runner()
    conn, arm = _setup(tmp_path)
    counts = runner._cycle(conn, arm=arm, now_ms=HOUR + 1_000)
    path = tmp_path / "heartbeat.json"

    runner._write_heartbeat(path, conn=conn, arm=arm, now_ms=HOUR + 1_000, counts=counts)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dirty"] is False
    assert payload["last_processed_closed_hour_ms"] == 0
    assert payload["status_counts"] == {"pending": 1}


def test_manifest_identity_is_pinned_and_idempotent(tmp_path, monkeypatch):
    runner = _runner()
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    manifest = {
        "arm_id": "standalone-v1", "code_sha": "abc",
        "hypothesis": "standalone_exhaustion", "policy": {"alt_short_only": True},
        "targets": ["standalone-exhaustion"], "taxonomy_version": "alt-v1",
        "benchmark_version": "loo-v1", "regime_version": "causal-v1",
    }
    path = tmp_path / "arm.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runner, "_git_state", lambda **kwargs: ("abc", False))
    first = runner._load_arm(conn, path, now_ms=1000)
    replay = runner._load_arm(conn, path, now_ms=9999)
    assert first["activated_ts_ms"] == replay["activated_ts_ms"] == 1000
    manifest["hypothesis"] = "wallet_copy_alt_short"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="wrong_hypothesis"):
        runner._load_arm(conn, path, now_ms=1000)


def test_runtime_pin_ignores_unrelated_tracked_work_but_rejects_code_edit(tmp_path):
    runner = _runner()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    code = tmp_path / "runner.py"
    unrelated = tmp_path / "notes.md"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    unrelated.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "runner.py", "notes.md"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "-c", "user.name=test", "-c", "user.email=test@example.com",
        "commit", "-qm", "runtime",
    ], cwd=tmp_path, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        text=True, capture_output=True,
    ).stdout.strip()

    unrelated.write_text("dirty unrelated work\n", encoding="utf-8")
    assert runner._git_state(
        root=tmp_path, pinned_code_sha=pinned, code_paths=["runner.py"]
    ) == (pinned, False)
    code.write_text("VALUE = 2\n", encoding="utf-8")
    _, dirty = runner._git_state(
        root=tmp_path, pinned_code_sha=pinned, code_paths=["runner.py"]
    )
    assert dirty is True
