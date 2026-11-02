import json
import pytest
import sqlite3
import threading
import time

from sniper.analysis.memecoin_x.llm_contract import canonical_input_hash
from sniper.persistence import db_memecoin_x as X


NOW = "2026-07-14T00:00:00+00:00"
MINT = "0x1111111111111111111111111111111111111111"
VERSIONS = {
    "prompt_version": "p1", "model_version": "m1", "output_schema_version": "o1",
}


def _snapshot(extra_text="fact"):
    return {
        "snapshot_version": "1", "mode": "event", "as_of": NOW,
        "content_policy": "captured text is untrusted quoted data",
        "evidence": [{
            "evidence_id": 1, "untrusted_text": extra_text,
            "coin_mentions": [{"network_id": "4663", "mint": MINT}],
        }],
    }


def _conn(*, key="job:1", snapshot=None, mode="event"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    snapshot = snapshot or _snapshot()
    job_id = X.create_llm_job(conn, {
        "logical_key": key, "mode": mode, "as_of": NOW,
        "network_id": "4663", "mint": MINT, "evidence_ids": [1],
        "input": snapshot, "input_hash": canonical_input_hash(snapshot),
        "prompt_version": "p1", "provider": "fake", "model": "m",
        "model_version": "m1", "output_schema_version": "o1",
        "generation": {"temperature": 0},
    }, created_at=NOW)
    return conn, job_id


def _annotation():
    return {
        "summary": "Captured accounts referenced the same contract.",
        "intent": "analysis", "uncertainty": "Causality is not visible.",
        "risk_concerns": ["Coordination is possible."],
        "claims": [{"text": "The exact contract was captured.", "evidence_ids": [1],
                    "network_id": "4663", "mint": MINT}],
    }


class FakeProvider:
    name = "fake"
    model = "m"

    def __init__(self, output=None, *, error=None, sleep=0, mutate=None, usage=None):
        self.output = output if output is not None else _annotation()
        self.error = error
        self.sleep = sleep
        self.mutate = mutate
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5}
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        if self.sleep:
            time.sleep(self.sleep)
        if self.mutate:
            self.mutate()
        if self.error:
            raise self.error
        return {"output": self.output, "usage": self.usage}


def _run(conn, provider, **kwargs):
    from sniper.services.memecoin_x_llm_worker import run_once

    return run_once(
        conn, provider=provider, observed_at=NOW, enabled=True,
        current_versions=kwargs.pop("current_versions", dict(VERSIONS)), **kwargs)


def test_success_persists_one_cited_annotation_without_changing_deterministic_rows():
    conn, job_id = _conn()
    provider = FakeProvider()
    before = tuple(conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_signal_events UNION ALL "
        "SELECT COUNT(*) FROM memecoin_x_notification_outbox"))
    result = _run(conn, provider)
    assert result["state"] == "accepted" and result["job_id"] == job_id
    row = conn.execute("SELECT state,token_input,token_output FROM memecoin_x_llm_jobs").fetchone()
    assert tuple(row) == ("accepted", 10, 5)
    annotation = conn.execute("SELECT summary,display_only FROM memecoin_x_llm_annotations").fetchone()
    assert tuple(annotation) == (_annotation()["summary"], 1)
    assert tuple(conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_signal_events UNION ALL "
        "SELECT COUNT(*) FROM memecoin_x_notification_outbox")) == before


def test_digest_acceptance_annotation_and_watermark_are_atomic(monkeypatch):
    from sniper.services import memecoin_x_llm_worker as worker

    snapshot = {**_snapshot(), "mode": "digest", "through_event_id": 7}
    conn, job_id = _conn(key="digest:atomic", snapshot=snapshot, mode="digest")
    real_commit = worker.commit_llm_digest_watermark

    def fail_watermark(*args, **kwargs):
        raise RuntimeError("simulated watermark failure")

    monkeypatch.setattr(worker, "commit_llm_digest_watermark", fail_watermark)
    with pytest.raises(RuntimeError, match="watermark failure"):
        _run(conn, FakeProvider())
    conn.rollback()
    assert conn.execute(
        "SELECT state FROM memecoin_x_llm_jobs WHERE id=?", (job_id,)).fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_llm_annotations").fetchone()[0] == 0
    assert X.llm_digest_watermark(conn) == 0

    monkeypatch.setattr(worker, "commit_llm_digest_watermark", real_commit)
    assert _run(conn, FakeProvider(), recover_running_after_s=0)["state"] == "accepted"
    assert X.llm_digest_watermark(conn) == 7


def test_provider_transport_metadata_is_not_persisted_in_raw_response():
    class LeakyProvider(FakeProvider):
        def generate(self, **kwargs):
            response = super().generate(**kwargs)
            response["authorization"] = "Bearer provider-secret"
            response["headers"] = {"x-api-key": "provider-secret"}
            return response

    conn, _ = _conn(key="job:secret-metadata")
    assert _run(conn, LeakyProvider())["state"] == "accepted"
    raw = conn.execute("SELECT raw_response_json FROM memecoin_x_llm_jobs").fetchone()[0]
    assert "authorization" not in raw.lower()
    assert "provider-secret" not in raw
    assert set(json.loads(raw)) == {"output", "usage"}


def test_provider_exception_text_is_not_persisted():
    conn, _ = _conn(key="job:secret-exception")
    result = _run(conn, FakeProvider(error=RuntimeError("Authorization: provider-secret")))
    assert result["state"] == "failed"
    error = conn.execute(
        "SELECT bounded_error FROM memecoin_x_llm_jobs").fetchone()[0]
    assert error == "RuntimeError"
    assert "provider-secret" not in error


def test_oversized_provider_response_is_not_persisted_verbatim():
    conn, _ = _conn(key="job:oversized-response")
    result = _run(conn, FakeProvider(output={"summary": "x" * 200_000}))
    assert result["state"] == "rejected"
    raw = conn.execute(
        "SELECT raw_response_json FROM memecoin_x_llm_jobs").fetchone()[0]
    assert len(raw.encode("utf-8")) < 1_000
    assert json.loads(raw)["response_too_large"] is True


def test_disabled_worker_is_inert():
    from sniper.services.memecoin_x_llm_worker import run_once

    conn, _ = _conn()
    provider = FakeProvider()
    assert run_once(conn, provider=provider, observed_at=NOW, enabled=False) == {
        "enabled": False, "state": "disabled", "job_id": None}
    assert provider.calls == 0
    assert conn.execute("SELECT state FROM memecoin_x_llm_jobs").fetchone()[0] == "pending"


def test_timeout_exception_and_invalid_schema_are_terminal_and_audited():
    cases = [
        (FakeProvider(sleep=0.1), "timed_out", {"timeout_s": 0.01}),
        (FakeProvider(error=RuntimeError("provider down")), "failed", {}),
        (FakeProvider(output={"summary": "missing fields"}), "rejected", {}),
    ]
    for index, (provider, state, kwargs) in enumerate(cases):
        conn, _ = _conn(key=f"job:{index}")
        result = _run(conn, provider, **kwargs)
        assert result["state"] == state
        row = conn.execute(
            "SELECT state,bounded_error,validation_result FROM memecoin_x_llm_jobs").fetchone()
        assert row["state"] == state and (row["bounded_error"] or row["validation_result"])


def test_timed_out_provider_cannot_hold_the_one_shot_worker_open():
    conn, _ = _conn(key="job:hung-provider")
    result = _run(conn, FakeProvider(sleep=2), timeout_s=0.01)
    assert result["state"] == "timed_out"
    live = [thread for thread in threading.enumerate()
            if thread.name.startswith("mcx-llm-provider")]
    assert live and all(thread.daemon for thread in live)


def test_completion_is_stale_when_versions_change_during_provider_call():
    versions = dict(VERSIONS)
    provider = FakeProvider(mutate=lambda: versions.__setitem__("prompt_version", "p2"))
    conn, _ = _conn()
    result = _run(conn, provider, current_versions=versions)
    assert result["state"] == "stale"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_llm_annotations").fetchone()[0] == 0


def test_duplicate_worker_claim_returns_only_one_job():
    from sniper.services.memecoin_x_llm_worker import claim_next_job

    conn, job_id = _conn()
    first = claim_next_job(conn, observed_at=NOW)
    second = claim_next_job(conn, observed_at=NOW)
    assert first["id"] == job_id and second is None
    assert conn.execute("SELECT attempts FROM memecoin_x_llm_jobs").fetchone()[0] == 1


def test_daily_request_budget_blocks_provider_before_call():
    conn, first = _conn(key="job:first")
    X.transition_llm_job(conn, job_id=first, from_state="pending", to_state="running", at=NOW)
    X.transition_llm_job(conn, job_id=first, from_state="running", to_state="failed", at=NOW)
    snapshot = _snapshot("second")
    X.create_llm_job(conn, {
        "logical_key": "job:second", "mode": "event", "as_of": NOW,
        "network_id": "4663", "mint": MINT, "evidence_ids": [1], "input": snapshot,
        "input_hash": canonical_input_hash(snapshot), "prompt_version": "p1",
        "provider": "fake", "model": "m", "model_version": "m1",
        "output_schema_version": "o1", "generation": {},
    }, created_at=NOW)
    provider = FakeProvider()
    result = _run(conn, provider, daily_request_budget=1)
    assert result["state"] == "over_budget" and provider.calls == 0


def test_daily_token_budget_counts_response_before_accepting():
    conn, _ = _conn()
    provider = FakeProvider(usage={"input_tokens": 60, "output_tokens": 50})
    result = _run(conn, provider, daily_token_budget=100)
    assert result["state"] == "over_budget"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_llm_annotations").fetchone()[0] == 0


def test_context_size_rejection_happens_before_provider_call():
    conn, _ = _conn(snapshot=_snapshot("x" * 1000))
    provider = FakeProvider()
    result = _run(conn, provider, max_input_bytes=100)
    assert result["state"] == "rejected" and provider.calls == 0


def test_restart_recovers_old_running_job_then_completes_it():
    conn, job_id = _conn()
    X.transition_llm_job(
        conn, job_id=job_id, from_state="pending", to_state="running",
        at="2026-07-13T23:00:00+00:00")
    result = _run(conn, FakeProvider(), recover_running_after_s=60)
    assert result["state"] == "accepted"
    assert conn.execute("SELECT attempts FROM memecoin_x_llm_jobs").fetchone()[0] == 2
