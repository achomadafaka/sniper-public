"""Cold namespaced layouts finish through verified background-only storage hints."""
import json
import time

import pytest

from sniper.execution import evm_token_safety as safety
from sniper.execution.evm_rpc_pool import RequestNotSent
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services.memecoin_rh_fast_lane import ResearchPaused
from test_bsc_native_preparation_rpc import CALL, URL, reads_for
from test_bsc_preparation_scheduling import setup_job, full_safety_network
from test_memecoin_bsc_fast_lane_live import live56


BALANCE_KEY = "0x" + "c1" * 32
ALLOWANCE_KEY = "0x" + "d2" * 32
WRONG_KEY = "0x" + "e3" * 32


def hint_network(live, mint, clock, calls, *, mode="valid"):
    base = full_safety_network(live, clock, calls)
    def network(method, url, *, body, **kwargs):
        request = json.loads(body)
        if isinstance(request, dict) and request["method"] == "eth_createAccessList":
            assert not live.snapshot.refreshing.is_set()
            # The fake clock owns snapshot age; wall time between two lines is host load.
            assert clock.age < 9
            calls.append((clock(), request, kwargs.get("timeout")))
            if mode == "transport":
                raise TimeoutError("hint transport failed")
            if mode in ("unsupported", "provider_error"):
                code = -32601 if mode == "unsupported" else -32005
                return 200, {"jsonrpc": "2.0", "id": request["id"],
                             "error": {"code": code, "message": "unavailable"}}, {}
            if mode == "malformed":
                result = {"accessList": [{"address": mint, "storageKeys": ["0x1"]}]}
            else:
                key = BALANCE_KEY if request["params"][0]["data"].startswith("0x70a08231") else ALLOWANCE_KEY
                key = WRONG_KEY if mode == "wrong" else key
                result = {"accessList": [{"address": "0x" + "ab" * 20,
                                         "storageKeys": [WRONG_KEY]},
                                        {"address": mint, "storageKeys": [key, key]}],
                          "gasUsed": "0x5000"}
            if mode == "expired":
                clock.now += 60
            return 200, {"jsonrpc": "2.0", "id": request["id"], "result": result}, {}
        response = base(method, url, body=body, **kwargs)
        rows = request if isinstance(request, list) else [request]
        answers = response[1] if isinstance(request, list) else [response[1]]
        for row, answer in zip(rows, answers):
            if row["method"] != "eth_call":
                continue
            call = row["params"][0]
            if str(call["to"]).lower() != mint or not call["data"].startswith(("0x70a08231", "0xdd62ed3e")):
                continue
            expected = BALANCE_KEY if call["data"].startswith("0x70a08231") else ALLOWANCE_KEY
            overrides = row["params"][2][mint]["stateDiff"]
            answer["result"] = safety._BIG_HEX if expected in overrides else "0x0"
        return response
    return network


def rpc_rows(calls):
    return [row for _, request, _ in calls
            for row in (request if isinstance(request, list) else [request])]


def test_cold_namespaced_native_preparation_completes_inside_original_job_bound(live56, monkeypatch, record_property):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.base = hint_network(live, mint, clock, calls)
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None and result[0], (result, dict(live.cache.counts), calls)
    assert clock() - 10 < 60 and clock.refreshes >= 1
    consumed, evidence = time.time(), {}
    assert live.cache.reason(mint, now=consumed, evidence=evidence) is None
    assert evidence["prepared_inputs_complete"] is True
    assert 0 <= consumed - evidence["verdict_at"] <= live.cache.ttl_s
    assert 0 <= consumed - live.cache._facts_at[mint] <= live.cache.supply_ttl_s
    assert 0 <= consumed - live.cache._stats[mint][1] <= live.cache.ttl_s
    assert live.cache.preparation_counts(now=consumed, generation=live.generation_fn())["candidates_ready"] == 1
    hints = [r for r in rpc_rows(calls) if r["method"] == "eth_createAccessList"]
    assert len(hints) == 2
    assert all(r["params"][0]["to"] == mint for r in hints)
    assert live.cache.counts["verdicts_ok"] == 1
    assert live.cache.counts["preparation_cancelled"] == 0
    record_property("cold_physical_requests", len(calls))
    record_property("cold_synthetic_duration_s", round(clock() - 10, 3))


@pytest.mark.parametrize("mode", ["transport", "provider_error", "malformed", "expired"])
def test_invalid_or_expired_hint_cancels_without_publishing_safety(live56, monkeypatch, mode):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.base = hint_network(live, mint, clock, calls, mode=mode)
    assert live.cache.run_one(mint, now=time.time()) is None
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert live.cache.counts["verdicts_ok"] == live.cache.counts["verdicts_unsafe"] == 0
    assert any(r["method"] == "eth_createAccessList" for r in rpc_rows(calls))


def test_wrong_hint_cannot_replace_the_override_readback_proof(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.base = hint_network(live, mint, clock, calls, mode="wrong")
    assert live.cache.run_one(mint, now=time.time()) is None
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    hints = [r for r in rpc_rows(calls) if r["method"] == "eth_createAccessList"]
    assert len(hints) == 2  # no duplicate late access-list calls after the hint failed readback


def test_unsupported_hint_uses_original_scan_without_duplicate_late_lookup(monkeypatch):
    from sniper.execution import evm_rpc_pool
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: [url])
    answer = {"jsonrpc": "2.0", "id": 1,
              "error": {"code": -32601, "message": "method not found"}}
    reads, sends = reads_for(answer)
    mint = "0x" + "77" * 20
    seen, late = [], []
    def readback(key):
        seen.append(key)
        return safety._BIG_HEX if key == "0x" + "00" * 31 + "02" else "0x0"
    result = safety._find_slot(
        readback, lambda i: "0x%064x" % i,
        early_slots=lambda: safety._access_list_slots(
            mint, "0x70a08231" + "00" * 32, URL, http=reads, strict=True),
        late_slots=lambda: late.append(True) or ())
    assert result == "0x" + "00" * 31 + "02"
    assert seen[:2] == ["0x" + "00" * 32, "0x" + "00" * 31 + "01"]
    assert len(sends) == 1 and not late and not reads.cancelled.is_set()


def test_common_slot_preparation_never_pays_for_access_list_hints(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.base = full_safety_network(live, clock, calls)
    result = live.cache.run_one(mint, now=time.time())
    assert result is not None and result[0]
    assert not any(r["method"] == "eth_createAccessList" for r in rpc_rows(calls))
    probes = [r for r in rpc_rows(calls) if r["method"] == "eth_call" and r["params"][0]["data"].startswith(("0x70a08231", "0xdd62ed3e"))]
    assert len(probes) == 2  # one balance and one allowance proof; native funding needs no slots


def test_default_inline_checker_keeps_common_prefix_cost_and_has_no_early_hint_read(live56, monkeypatch):
    live, mint, clock = setup_job(live56, monkeypatch)
    calls = []
    live.paced.min_gap_s = 0  # inline HTTP does not use the speculative snapshot-wait fixture
    live.paced.base = full_safety_network(live, clock, calls)
    checked = live.cache.safety_fn.for_quote(mint, bsc.WBNB)
    result = checked(mint, usd=live.cache.usd)
    assert result[0], result
    rows = rpc_rows(calls)
    assert not any(r["method"] == "eth_createAccessList" for r in rows)
    assert len([r for r in rows if r["method"] == "eth_call" and r["params"][0]["data"].startswith(("0x70a08231", "0xdd62ed3e"))]) == 2


def test_explicit_exact_unsupported_access_list_does_not_cancel_job():
    answer = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}}
    reads, sends = reads_for(answer)
    assert reads("POST", URL, body=json.dumps(dict(CALL, method="eth_createAccessList")))[1] == answer
    assert len(sends) == 1 and not reads.cancelled.is_set()


@pytest.mark.parametrize("answer", [
    {"id": 2, "error": {"code": -32601}},
    {"id": 1, "error": {"code": "-32601"}},
    {"id": 1, "error": {"code": -32005}},
    {"id": 1, "result": None, "error": {"code": -32601}},
])
def test_other_access_list_errors_remain_unavailable(answer):
    reads, _ = reads_for(answer)
    with pytest.raises(ResearchPaused):
        reads("POST", URL, body=json.dumps(dict(CALL, method="eth_createAccessList")))
    assert reads.cancelled.is_set()


@pytest.mark.parametrize("result", [
    None, {}, [], {"accessList": None},
    {"accessList": [{"address": "0x" + "77" * 20, "storageKeys": ["0x1"]}]},
    {"accessList": [{"address": "0x" + "77" * 20, "storageKeys": ["0x" + "g1" * 32]}]},
    {"accessList": [{"address": "wrong", "storageKeys": []}]},
    {"accessList": [{"address": "0x" + "77" * 20, "storageKeys": {}}]},
    {"accessList": [{"address": "0x" + "77" * 20,
                     "storageKeys": ["0x%064x" % i for i in range(17)]}]},
    {"accessList": [{"address": "0x" + "77" * 20, "storageKeys": []}] * 65},
])
def test_malformed_or_excessive_access_list_reply_cancels_its_job(result):
    reads, _ = reads_for({"jsonrpc": "2.0", "id": 1, "result": result})
    call = dict(CALL, method="eth_createAccessList",
                params=[{"to": "0x" + "77" * 20}, "latest"])
    with pytest.raises(ResearchPaused):
        reads("POST", URL, body=json.dumps(call))
    assert reads.cancelled.is_set()


def test_exact_empty_access_list_is_a_valid_no_hint_answer():
    reads, _ = reads_for({"jsonrpc": "2.0", "id": 1, "result": {"accessList": []}})
    call = dict(CALL, method="eth_createAccessList",
                params=[{"to": "0x" + "77" * 20}, "latest"])
    assert reads("POST", URL, body=json.dumps(call))[1]["result"] == {"accessList": []}
    assert not reads.cancelled.is_set()


@pytest.mark.parametrize("failure", [TimeoutError("transport"), RequestNotSent("closed job"),
                                     safety.EvmSafetyDeadlineExceeded()])
def test_strict_hint_helper_never_converts_failure_to_an_empty_hint(monkeypatch, failure):
    from sniper.execution import evm_wallet
    def failed(*args, **kwargs):
        raise failure
    monkeypatch.setattr(evm_wallet, "_rpc", failed)
    with pytest.raises(type(failure)) as error:
        safety._access_list_slots("0x" + "77" * 20, "0x70a08231", URL, strict=True)
    assert error.value is failure


def test_missing_rpc_is_unavailable_for_strict_hints_and_keeps_default_behavior():
    with pytest.raises(ValueError, match="RPC unavailable"):
        safety._access_list_slots("0x" + "77" * 20, "0x70a08231", None, strict=True)
    assert safety._access_list_slots("0x" + "77" * 20, "0x70a08231", None) == ()


def test_hint_dedup_and_prefix_skip_do_not_repeat_readback_or_late_discovery():
    seen, hints, late = [], [], []
    def readback(key):
        seen.append(key)
        return "0x0"
    def early():
        hints.append(True)
        return ("0x%064x" % 0, WRONG_KEY, WRONG_KEY)
    assert safety._find_slot(readback, lambda i: "0x%064x" % i,
                             early_slots=early, late_slots=lambda: late.append(True) or ()) is None
    assert hints == [True] and not late
    assert seen.count("0x%064x" % 0) == seen.count(WRONG_KEY) == 1

