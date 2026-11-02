"""Task 8 — sanitized submission status projection.

Merges per-provider health (active/excluded/quarantined + last-health age) with recent-attempt stats
(accepted/ambiguous/rate-limit counts, p50/p95 latency, last state/latency) and sidecar leader/restart
status. No network call, no execution authority: an audit read failure yields empty stats. The output
exposes labels/states/counts/latencies only — never a URL, payload, or raw error. The snapshot file is
written atomically at mode 0600 and read by a separate process when in-memory state is absent.
"""
import json
import sqlite3

import pytest

from sniper.execution import submission_backends as B
from sniper.execution.submission_health import HealthRegistry
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_submission_status as S


@pytest.fixture(autouse=True)
def _clean():
    B.close_all()          # reset the in-memory health/sidecar registries between tests
    yield
    B.close_all()


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _record(c, provider, state, *, reason=None, latency=10.0):
    ident = "0xh" if state is AttemptState.ACCEPTED else None
    m.record_submission_attempts(
        c, chain_id="8453", action="entry", owner_kind="reservation", owner_id="1",
        tx_identity="0xh", signed_payload_sha256="hash",
        attempts=[SubmissionAttempt(provider, state, ident, latency, "200", reason)],
        created_at="2026-07-26T00:00:00Z")


def test_attempt_stats_counts_percentiles_and_last(tmp_path):
    c = _conn(tmp_path)
    for lat in (10, 20, 30, 40, 100):
        _record(c, "8453:base_standard", AttemptState.ACCEPTED, latency=lat)
    _record(c, "8453:base_standard", AttemptState.AMBIGUOUS, reason="http_429", latency=5)  # newest
    st = S.submission_status(c)["providers"]["8453:base_standard"]
    assert st["accepted"] == 5 and st["ambiguous"] == 1 and st["count"] == 6
    assert st["rate_limit"] == 1
    assert st["last_state"] == "ambiguous" and st["last_latency_ms"] == 5.0
    assert st["p50_ms"] is not None and st["p95_ms"] >= st["p50_ms"]


def test_not_sent_and_explicit_counts(tmp_path):
    c = _conn(tmp_path)
    _record(c, "solana:tpu", AttemptState.NOT_SENT)
    _record(c, "solana:tpu", AttemptState.EXPLICIT_REJECTED)
    st = S.submission_status(c)["providers"]["solana:tpu"]
    assert st["not_sent"] == 1 and st["explicit_rejected"] == 1


def test_health_states_merge_with_attempt_stats(tmp_path):
    c = _conn(tmp_path)
    reg = HealthRegistry(["8453:base_standard", "8453:base_flashblocks", "4663:sequencer"])
    reg.record_wrong_chain("8453:base_standard", "0xbad")                 # excluded
    for _ in range(3):
        reg.record_failure("8453:base_flashblocks", "x")                 # quarantined
    reg.record_success("4663:sequencer", "0x1237")                       # active
    B._register_health("test", reg)
    _record(c, "8453:base_standard", AttemptState.NOT_SENT)
    providers = S.submission_status(c)["providers"]
    assert providers["8453:base_standard"]["state"] == "excluded"
    assert providers["8453:base_standard"]["not_sent"] == 1              # health + stats merged
    assert providers["8453:base_flashblocks"]["state"] == "quarantined"
    assert providers["4663:sequencer"]["state"] == "active"
    assert "last_health_age_s" in providers["4663:sequencer"]


def test_sidecar_leader_and_restart_counts(tmp_path):
    c = _conn(tmp_path)

    class FakeSidecar:
        def sanitized_status(self):
            return {"provider": "solana:tpu", "running": True, "restart_count": 2,
                    "leader_count": 5, "last_health_age_s": 1.0}

    B._register_sidecar(FakeSidecar())
    sidecars = S.submission_status(c)["sidecars"]
    assert sidecars == [{"provider": "solana:tpu", "running": True, "restart_count": 2,
                         "leader_count": 5, "last_health_age_s": 1.0}]


def test_status_is_url_payload_and_error_free(tmp_path):
    c = _conn(tmp_path)
    reg = HealthRegistry(["8453:base_standard"])
    reg.record_failure("8453:base_standard", "https://secret-host/key 500 raw-boom")  # raw error in
    B._register_health("t", reg)
    _record(c, "8453:base_standard", AttemptState.AMBIGUOUS, reason="https://leak/err")
    blob = json.dumps(S.submission_status(c))
    for secret in ("https", "secret-host", "raw-boom", "leak"):
        assert secret not in blob


def test_audit_read_failure_yields_empty_stats_not_crash():
    class BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert S.submission_status(BrokenConn()) == {"providers": {}, "sidecars": []}


def test_status_needs_no_http_seam(tmp_path):
    # a pure DB + in-memory projection: it takes no http/RPC seam and never opens a socket.
    c = _conn(tmp_path)
    _record(c, "8453:custody_rpc", AttemptState.ACCEPTED)
    out = S.submission_status(c)
    assert "8453:custody_rpc" in out["providers"]


def test_write_health_snapshot_is_atomic_0600(tmp_path):
    reg = HealthRegistry(["8453:base_standard"])
    reg.record_success("8453:base_standard", "0x2105")
    B._register_health("t", reg)
    assert S.write_submission_health(str(tmp_path)) is True
    path = tmp_path / "data" / "memecoin_submission_health.json"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    data = json.loads(path.read_text())
    assert data["health"]["8453:base_standard"]["state"] == "active"


def test_reader_process_reads_snapshot_file_when_in_memory_empty(tmp_path):
    # simulate the web process: no in-memory health, but the runner's snapshot file exists.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_submission_health.json").write_text(json.dumps({
        "health": {"solana:public_rpc": {"state": "active", "last_health_age_s": 3.0}},
        "sidecars": [{"provider": "solana:tpu", "running": True, "restart_count": 0,
                      "leader_count": 4, "last_health_age_s": 2.0}]}))
    c = _conn(tmp_path)
    _record(c, "solana:public_rpc", AttemptState.ACCEPTED)
    out = S.submission_status(c, str(tmp_path))
    assert out["providers"]["solana:public_rpc"]["state"] == "active"    # from the file
    assert out["providers"]["solana:public_rpc"]["accepted"] == 1        # live from the DB
    assert out["sidecars"][0]["leader_count"] == 4


def test_reader_process_fills_missing_sidecar_from_snapshot(tmp_path):
    """Partial web-process health must not hide the runner-owned TPU status."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_submission_health.json").write_text(json.dumps({
        "health": {
            "solana:public_rpc": {"state": "active", "ok": 9},
            "8453:base_flashblocks": {"state": "active", "ok": 7},
        },
        "sidecars": [{"provider": "solana:tpu", "running": True, "ready": True,
                      "restart_count": 2, "leader_count": 4, "last_health_age_s": 0.5}]}))
    local = HealthRegistry(["solana:public_rpc"])
    local.record_success("solana:public_rpc", "mainnet")
    B._register_health("web-partial", local)

    out = S.submission_status(_conn(tmp_path), str(tmp_path))

    assert out["sidecars"][0]["leader_count"] == 4
    assert out["providers"]["8453:base_flashblocks"]["ok"] == 7
    assert out["providers"]["solana:public_rpc"]["ok"] == 1  # live state wins
