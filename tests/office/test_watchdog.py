"""Fail-closed Office guard; persist pause before a slice-scoped kill."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("office_watchdog", ROOT / "scripts/office_watchdog.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)












def test_latch_committed_before_kill_and_persists_until_manual_recovery(tmp_path):
    latch = tmp_path / "pause.json"
    observed = []
    def kill(slice_name):
        observed.append((slice_name, json.loads(latch.read_text())["reason"]))
    W.pause(latch, reason="cpu_pressure", now=101., terminate=kill)
    assert observed == [("office.slice", "cpu_pressure")]
    assert W.external_guard(latch) == "cpu_pressure"
    assert W.external_guard(latch) == "cpu_pressure"  # good telemetry cannot auto-clear


def test_failed_latch_write_never_kills(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    calls = []
    with pytest.raises(OSError):
        W.pause(blocked / "pause.json", reason="cpu_pressure", now=101., terminate=calls.append)
    assert calls == []


def test_unknown_latch_is_not_healthy(tmp_path):
    latch = tmp_path / "pause.json"
    assert W.external_guard(latch) == "guard_unknown"
    latch.write_text("broken")
    assert W.external_guard(latch) == "guard_unknown"


def test_explicit_healthy_latch_has_no_pause(tmp_path):
    latch = tmp_path / "pause.json"
    W.initialize(latch, now=100.)
    assert W.external_guard(latch, now=101.) is None


def test_guard_death_expires_ready_latch(tmp_path):
    latch = tmp_path / "pause.json"
    W.initialize(latch, now=100.)
    assert W.external_guard(latch, now=101., max_age_s=5.) is None
    assert W.external_guard(latch, now=106., max_age_s=5.) == "guard_stale"




def test_research_environment_rejects_shared_account_pool_even_with_different_key_names():
    config = {"MEMECOIN_RESEARCH_SOLANA_RPC_URL": "https://research.example"}
    resources = [{"env": "MEMECOIN_RESEARCH_SOLANA_RPC_URL", "provider": "p", "account": "a", "pool": "credits"}]
    funded = [{"provider": "p", "account": "a", "pool": "credits"}]
    with pytest.raises(ValueError, match="shared_resource"):
        W.research_environment(config, resources=resources, funded_resources=funded)
    with pytest.raises(ValueError):
        W.research_environment({"FUNDED_KEY": "canary"}, resources=[], funded_resources=[])
    assert W.research_environment(config, resources=resources, funded_resources=[]) == config


def test_headroom_recommendation_reserves_measured_host_capacity():
    path = ROOT / "scripts/office_measure_headroom.py"
    spec = importlib.util.spec_from_file_location("office_headroom", path)
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    result = H.recommend(memory_available=8 * 1024**3, idle_cores=4.)
    assert result["MemoryMax"] <= 0.10 * 8 * 1024**3
    assert result["CPUQuota"] <= 40.  # at least 90% of observed idle capacity remains outside Office
    assert result["MemoryHigh"] < result["MemoryMax"]
    assert result["margin_rule"]




def test_bounded_trace_tail_distinguishes_empty_from_read_failure(tmp_path):
    import sqlite3
    spec = importlib.util.spec_from_file_location('office_headroom_tail', ROOT / 'scripts/office_measure_headroom.py')
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    db = sqlite3.connect(tmp_path / 'traces.db')
    db.execute('CREATE TABLE memecoin_entry_attempts(attempt_id,network_id,signal_source,terminal_status,completed_at,total_ms,transaction_submitted_at)')
    db.execute('CREATE TABLE memecoin_exit_execution_attempts(id,network_id,tier_variant,status,attempted_at,total_ms,sig)')
    db.commit()
    empty = H.trace_tail(db, now=1000., window_s=60., limit=5)
    assert empty['samples'] == 0 and empty['state'] == 'observed_empty'
    db.execute("INSERT INTO memecoin_entry_attempts VALUES('e1','solana','slot_lane','filled','1970-01-01T00:16:30+00:00',100.,'1970-01-01T00:16:29+00:00')")
    db.commit()
    observed = H.trace_tail(db, now=1000., window_s=60., limit=5)
    assert observed['samples'] == 1 and observed['p90_ms'] == 100.
    assert observed['groups'][0]['network_id'] == 'solana'
    db.execute('DROP TABLE memecoin_exit_execution_attempts')
    assert H.trace_tail(db, now=1000., window_s=60., limit=5) is None
    db.close()




