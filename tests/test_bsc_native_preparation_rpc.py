"""Speculative RPC replies stay exact; local deferral never poisons provider health."""
import collections
import json
from types import SimpleNamespace

import pytest

from sniper.execution.evm_rpc_pool import RequestNotSent
from sniper.services.memecoin_bsc_fast_lane import PreparationReads
from sniper.services.memecoin_rh_fast_lane import PacedHttp, ResearchPaused


URL = "https://bsc.invalid"
CALL = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": []}
REVERT = {"code": 3, "message": "execution reverted", "data": "0x"}


def reads_for(response=None, *, status=200, failure=None):
    sends = []
    def base(*args, **kwargs):
        sends.append((args, kwargs))
        if failure is not None:
            raise failure
        return status, response, {}
    cache = SimpleNamespace(admission_fn=lambda: True, deferral_fn=lambda: False, ttl_s=60.0,
                            counts=collections.Counter())
    paced = PacedHttp(base, min_gap_s=0)
    return PreparationReads(cache, paced, URL), sends


@pytest.mark.parametrize("answers", [
    [{"id": 2, "result": "0x02"}, {"id": 1, "result": "0x01"}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "error": REVERT}],
    [{"id": 1, "error": REVERT}, {"id": 2, "error": REVERT}],
    [{"id": 1, "result": 0}, {"id": 2, "result": False}],
])
def test_valid_exact_batch_retains_each_result_or_explicit_call_revert(answers):
    reads, sends = reads_for(answers)
    body = json.dumps([CALL, dict(CALL, id=2)])
    assert reads("POST", URL, body=body) == (200, answers, {})
    assert len(sends) == 1 and not reads.cancelled.is_set()


@pytest.mark.parametrize("answers", [
    [], None, {}, {"id": 1, "result": "0x01"},
    [{"id": 1, "result": "0x01"}],
    [{"id": 1, "result": "0x01"}, {"id": 1, "result": "0x02"}],
    [{"id": 1, "result": "0x01"}, {"id": 3, "result": "0x02"}],
    [{"id": True, "result": "0x01"}, {"id": 2, "result": "0x02"}],
    [{"id": "1", "result": "0x01"}, {"id": 2, "result": "0x02"}],
    [{"result": "0x01"}, {"id": 2, "result": "0x02"}],
    [{"id": 1, "result": "0x01"}, None],
    [{"id": 1, "result": "0x01"}, {"id": 2}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "result": None}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "error": "execution reverted"}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "error": {"message": "rate limited"}}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "error": {"message": "Call failed", "data": "0x0"}}],
    [{"id": 1, "result": "0x01"}, {"id": 2, "result": None, "error": REVERT}],
])
def test_incomplete_or_unmatched_batch_cancels_publication(answers):
    reads, sends = reads_for(answers)
    with pytest.raises(ResearchPaused):
        reads("POST", URL, body=json.dumps([CALL, dict(CALL, id=2)]))
    assert len(sends) == 1 and reads.cancelled.is_set()


def test_batch_revert_on_non_call_read_is_unavailable():
    reads, _ = reads_for([{"id": 1, "result": "0x01"}, {"id": 2, "error": REVERT}])
    with pytest.raises(ResearchPaused):
        reads("POST", URL, body=json.dumps([CALL, dict(CALL, id=2, method="eth_getCode")]))
    assert reads.cancelled.is_set()


def test_reordered_mixed_method_batch_matches_revert_to_its_exact_request():
    answers = [{"id": 2, "result": "0x6001"}, {"id": 1, "error": REVERT}]
    reads, sends = reads_for(answers)
    body = json.dumps([CALL, dict(CALL, id=2, method="eth_getCode")])
    assert reads("POST", URL, body=body)[1] == answers
    assert len(sends) == 1 and not reads.cancelled.is_set()


@pytest.mark.parametrize("body", [None, "invalid json", "[]", "null", "[null]",
    json.dumps([CALL, dict(CALL, id=1)]), json.dumps([dict(CALL, id=True)]),
    json.dumps([dict(CALL, id="1")]), json.dumps([{"id": 1}])])
def test_invalid_local_request_does_not_touch_provider(body):
    reads, sends = reads_for()
    with pytest.raises(RequestNotSent):
        reads("POST", URL, body=body)
    assert not sends and reads.cache.counts["preparation_request_attempts"] == 0


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500, None])
def test_http_failure_cannot_become_a_successful_batch(status):
    answers = [{"id": 1, "result": "0x01"}, {"id": 2, "result": "0x02"}]
    reads, _ = reads_for(answers, status=status)
    with pytest.raises(ResearchPaused):
        reads("POST", URL, body=json.dumps([CALL, dict(CALL, id=2)]))
    assert reads.cancelled.is_set()


@pytest.mark.parametrize("refusal", ["target", "admission", "closed", "cancelled",
                                    "background_lock", "account_lock", "paused", "dead"])
def test_local_pre_dispatch_refusal_is_not_sent(refusal):
    reads, sends = reads_for({"result": "0x01"})
    target, lock = URL, None
    if refusal == "target":
        target = URL + "/wrong"
    elif refusal == "admission":
        reads.cache.admission_fn = lambda: False
    elif refusal in ("closed", "cancelled"):
        getattr(reads, refusal).set()
    elif refusal.endswith("lock"):
        lock = reads.paced._background_lock if refusal == "background_lock" else reads.paced._lock
        lock.acquire()
    else:
        setattr(reads.paced, "_paused_until" if refusal == "paused" else "_dead_until", float("inf"))
    try:
        with pytest.raises(RequestNotSent):
            reads("POST", target, body=json.dumps(CALL))
    finally:
        if lock is not None:
            lock.release()
    assert not sends and reads.cancelled.is_set() and reads.paced.counts["calls"] == 0


def test_last_admission_check_refuses_before_physical_dispatch():
    reads, sends = reads_for({"result": "0x01"})
    checks = []
    def admission():
        checks.append(True)
        return len(checks) < 3
    reads.cache.admission_fn = admission
    with pytest.raises(RequestNotSent):
        reads("POST", URL, body=json.dumps(CALL))
    assert len(checks) == 3 and not sends and reads.paced.counts["calls"] == 0


@pytest.mark.parametrize("failure", [TimeoutError("provider timeout"), OSError("transport"),
                                     ResearchPaused("transport answered failure")])
def test_failure_after_dispatch_is_never_retyped_as_not_sent(failure):
    reads, sends = reads_for(failure=failure)
    with pytest.raises(type(failure)) as error:
        reads("POST", URL, body=json.dumps(CALL))
    assert error.value is failure and not isinstance(error.value, RequestNotSent)
    assert len(sends) == 1 and reads.cancelled.is_set()


def test_rpc_pool_never_penalizes_local_cancel(monkeypatch):
    from sniper.execution import evm_rpc_pool as pool, evm_wallet
    records = []
    reads, sends = reads_for()
    reads.cache.admission_fn = lambda: False
    monkeypatch.setattr(pool, "pool_for", lambda url: [url])
    monkeypatch.setattr(pool, "chain_of", lambda url: "56")
    monkeypatch.setattr(pool, "record", lambda *args: records.append(("record", args)))
    monkeypatch.setattr(pool, "penalize", lambda *args: records.append(("penalize", args)))
    with pytest.raises(RequestNotSent):
        evm_wallet._rpc(URL, "eth_getCode", ["0x" + "01" * 20, "latest"], http=reads)
    assert not records and not sends


def test_actual_v2_batch_caller_accepts_exact_replies_without_scalar_retry(monkeypatch):
    from sniper.execution import evm_rpc_pool as pool
    from sniper.execution.evm_v2_exit_quote import _make_call_fn, V2Reverted
    reads, sends = reads_for([{"id": 2, "error": REVERT}, {"id": 1, "result": "0x01"}])
    monkeypatch.setattr(pool, "pool_for", lambda url: [url])
    monkeypatch.setattr(pool, "chain_of", lambda url: "56")
    monkeypatch.setattr(pool, "choose", lambda url, **kwargs: url)
    monkeypatch.setattr(pool, "refuses", lambda *args: False)
    results = _make_call_fn("56", reads, rpc=URL).batch([("0x01", "0x01"), ("0x02", "0x02")])
    assert results[0] == "0x01" and isinstance(results[1], V2Reverted)
    assert len(sends) == 1 and not reads.cancelled.is_set()
