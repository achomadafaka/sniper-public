"""V3 quote read failover; fake HTTP only, with real pool identity and health rules."""
import copy
import json

import pytest

from sniper.execution import evm_dex_quote as Q
from sniper.execution import evm_rpc_pool as pool
from sniper.settings import memecoin as cfg


PRIMARY = "https://primary.invalid"
BACKUP = "https://backup.invalid"
WRONG = "https://wrong-chain.invalid"
QUOTER = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
COIN = "0x" + "33" * 20
BUSY = {"code": -32005, "message": "daily request count exceeded, request rate limited"}
REVERT = {"code": 3, "message": "execution reverted: rate limit token"}


@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, BACKUP)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {})
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda _chain: PRIMARY)
    pool.reset_for_tests()
    yield
    pool.reset_for_tests()


def _error(request_id, error=BUSY):
    return {"jsonrpc": "2.0", "id": request_id, "error": dict(error)}


def _results(request):
    # Reverse the response order: the actual requested IDs must select each output.
    if isinstance(request, list):
        return [{"jsonrpc": "2.0", "id": item["id"], "result": "0x%02x" % item["id"]}
                for item in reversed(request)]
    return {"jsonrpc": "2.0", "id": request["id"], "result": "0x01"}


def _transport(primary_reply, *, backup_reply=None, fail_at=None, wrong_chain=None):
    calls, bodies = [], []
    def http(method, url, body=None):
        assert method == "POST"
        request = json.loads(body)
        identity = isinstance(request, dict) and request["method"] == "eth_chainId"
        calls.append((url, "identity" if identity else "quote"))
        if fail_at == ("identity" if identity else "quote"):
            raise pool.RequestNotSent("local request budget exhausted")
        if identity:
            return 200, {"jsonrpc": "2.0", "id": request["id"],
                         "result": "0x38" if url == wrong_chain else "0x1"}, None
        bodies.append((url, body))
        reply = primary_reply if url == PRIMARY else backup_reply
        payload = reply(request) if callable(reply) else copy.deepcopy(reply)
        return 200, _results(request) if payload is None else payload, None
    return http, calls, bodies


def _assert_same_read(bodies):
    assert [url for url, _ in bodies] == [PRIMARY, BACKUP]
    assert bodies[0][1] == bodies[1][1]  # Every target, parameter, ID and amount is unchanged.


def test_single_busy_reply_uses_verified_backup_without_changing_read():
    http, calls, bodies = _transport(_error(1))
    call = Q._make_call_fn("1", http)
    assert call(QUOTER, "0x1234") == "0x01"
    _assert_same_read(bodies)
    assert calls == [(PRIMARY, "identity"), (PRIMARY, "quote"),
                     (BACKUP, "identity"), (BACKUP, "quote")]
    assert not pool.available(PRIMARY) and pool.identity_state(BACKUP) is True
    assert call.read_state == {"answered": True, "unavailable": None}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("payload", [None, {"error": {
    "code": -33000, "message": "You have reached your daily credit limit"}}])
def test_http_402_daily_quota_reoffers_exact_read_to_verified_backup(batch, payload):
    calls, bodies = [], []

    def http(method, url, body=None):
        request = json.loads(body)
        if isinstance(request, dict) and request["method"] == "eth_chainId":
            calls.append((url, "identity"))
            return 200, {"result": "0x1"}, None
        calls.append((url, "quote"))
        bodies.append((url, body))
        if url == PRIMARY:
            return 402, copy.deepcopy(payload), None
        return 200, _results(request), None

    call = Q._make_call_fn("1", http)
    if batch:
        assert call.batch([(QUOTER, "0x1234"), (QUOTER, "0xabcd")]) == ["0x01", "0x02"]
    else:
        assert call(QUOTER, "0x1234") == "0x01"
    _assert_same_read(bodies)
    assert calls == [(PRIMARY, "identity"), (PRIMARY, "quote"),
                     (BACKUP, "identity"), (BACKUP, "quote")]
    assert not pool.available(PRIMARY) and pool.identity_state(BACKUP) is True


@pytest.mark.parametrize("reply", [
    _error(None),
    [_error(2), _error(1)],
], ids=["top-level-quota", "all-bound-items-busy"])
def test_batch_busy_reply_reoffers_exact_batch_and_binds_reordered_results(reply):
    http, _calls, bodies = _transport(reply)
    call = Q._make_call_fn("1", http)
    assert call.batch([(QUOTER, "0x1234"), (QUOTER, "0xabcd")]) == ["0x01", "0x02"]
    _assert_same_read(bodies)
    assert all(isinstance(json.loads(body), list) for _url, body in bodies)
    assert not pool.available(PRIMARY) and pool.identity_state(BACKUP) is True


@pytest.mark.parametrize("reply", [
    _error(None),
    [_error(1), _error(2)],
    _error(None, {"code": -32005, "message": "Too many requests"}),
    [_error(1, {"code": -32005, "message": "Too many requests"}),
     _error(2, {"code": -32005, "message": "Too many requests"})],
], ids=["top-level-quota", "all-bound-items-busy", "top-level-too-many", "all-too-many"])
def test_exhausted_busy_batch_remains_unknown_without_single_tier_fanout(reply):
    from sniper.execution.evm_real_seams import classify_quote_miss
    http, _calls, bodies = _transport(reply, backup_reply=reply)
    with pytest.raises(Q.V3ReadUnavailable) as exc:
        Q.dex_v3_quote("1", WETH, COIN, 100, ROUTER, http=http,
                       cfg_addr={"quoter": QUOTER, "router": ROUTER},
                       slippage_bps=100, fee_tiers=(500, 3000), connectors=())
    assert exc.value.failure_type == "rpc_rate_limit"
    code, _detail = classify_quote_miss([
        {"rung": "v3", "hit": False, "blind_reason": exc.value.failure_type}])
    assert code == "provider_rpc_rate_limit"  # Existing entry cooldown uses this exact code.
    _assert_same_read(bodies)
    assert all(isinstance(json.loads(body), list) for _url, body in bodies)
    assert not pool.available(PRIMARY) and not pool.available(BACKUP)


@pytest.mark.parametrize("reply,want,answered", [
    ([_error(1), {"jsonrpc": "2.0", "id": 2, "result": "0x02"}], [None, "0x02"], True),
    ([_error(1, REVERT), _error(2)], [None, None], True),
], ids=["partial-priced", "partial-reverted"])
def test_partial_batch_is_authoritative_and_does_not_retry_or_penalize(reply, want, answered):
    http, _calls, bodies = _transport(reply)
    call = Q._make_call_fn("1", http)
    assert call.batch([(QUOTER, "0x1234"), (QUOTER, "0xabcd")]) == want
    assert len(bodies) == 1 and bodies[0][0] == PRIMARY
    assert pool.available(PRIMARY) and pool.identity_state(BACKUP) is None
    assert call.read_state["answered"] is answered


@pytest.mark.parametrize("reply", [
    [_error(1)],
    [_error(1), _error(1)],
    [_error(1), _error(3)],
    [_error(True), _error(2)],
    [_error("1"), _error(2)],
    [_error(1), _error(2), _error(3)],
    [],
], ids=["missing", "duplicate", "foreign", "boolean-id", "text-id", "extra", "empty"])
def test_unbound_busy_batch_never_quarantines_or_reoffers(reply):
    http, _calls, bodies = _transport(reply)
    call = Q._make_call_fn("1", http)
    call.batch([(QUOTER, "0x1234"), (QUOTER, "0xabcd")])
    assert len(bodies) == 1 and bodies[0][0] == PRIMARY
    assert pool.available(PRIMARY) and pool.identity_state(BACKUP) is None


@pytest.mark.parametrize("error", [
    REVERT,
    {"code": -32005, "message": "execution reverted: missing pool"},
    {"code": -32005, "message": "arbitrary application refusal"},
], ids=["revert-with-busy-word", "reverted-32005", "non-quota-32005"])
def test_revert_or_unrecognized_error_does_not_trigger_provider_failover(error):
    http, _calls, bodies = _transport(_error(1, error))
    call = Q._make_call_fn("1", http)
    assert call(QUOTER, "0x1234") is None
    assert len(bodies) == 1 and pool.available(PRIMARY)
    assert pool.identity_state(BACKUP) is None


@pytest.mark.parametrize("phase", ["identity", "quote"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_local_budget_refusal_propagates_without_provider_health_changes(phase, batch):
    http, calls, bodies = _transport(None, fail_at=phase)
    call = Q._make_call_fn("1", http)
    with pytest.raises(pool.RequestNotSent):
        if batch:
            call.batch([(QUOTER, "0x1234")])
        else:
            call(QUOTER, "0x1234")
    assert all(url == PRIMARY for url, _ in calls)
    assert pool.available(PRIMARY) and pool.available(BACKUP)
    assert pool.identity_state(PRIMARY) is (None if phase == "identity" else True)
    assert call.read_state == {"answered": False, "unavailable": None}
    assert not bodies  # Budget refuses before the fake external dispatch records a body.


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_full_quote_budget_refusal_stops_before_any_later_tier_or_backup(batch):
    http, calls, bodies = _transport(None, fail_at="quote")
    call = Q._make_call_fn("1", http)
    if not batch:
        del call.batch  # Exercise the real scalar boundary, with the same budgeted reader.
    with pytest.raises(pool.RequestNotSent):
        Q.dex_v3_quote("1", WETH, COIN, 100, ROUTER, call_fn=call,
                       cfg_addr={"quoter": QUOTER, "router": ROUTER},
                       slippage_bps=100, fee_tiers=(500, 3000), connectors=())
    assert calls == [(PRIMARY, "identity"), (PRIMARY, "quote")]
    assert not bodies and pool.available(PRIMARY) and pool.available(BACKUP)
    assert call.read_state == {"answered": False, "unavailable": None}


@pytest.mark.parametrize("status,response,exception", [
    (429, None, None),
    (503, None, None),
    (403, _error(1), None),
    (None, None, TimeoutError("identity transport timeout")),
    (200, {}, None),
    (200, _error(1), None),
    (200, {"jsonrpc": "2.0", "id": 1, "result": "not-a-chain-id"}, None),
    (200, {"jsonrpc": "2.0", "id": 1, "result": None}, None),
], ids=["429", "503", "403", "transport", "missing", "busy", "malformed", "null"])
def test_unavailable_identity_is_unknown_and_recovers_after_short_quarantine(
        monkeypatch, status, response, exception):
    clock = [100.0]
    monkeypatch.setattr(pool, "_now", lambda: clock[0])
    unknown = [True]
    healthy, calls, bodies = _transport(None)

    def http(method, url, body=None):
        request = json.loads(body)
        if url == PRIMARY and request.get("method") == "eth_chainId" and unknown[0]:
            calls.append((url, "identity"))
            if exception:
                raise exception
            return status, copy.deepcopy(response), None
        return healthy(method, url, body=body)

    call = Q._make_call_fn("1", http)
    assert call(QUOTER, "0x1234") == "0x01"
    assert bodies == [(BACKUP, bodies[0][1])]
    assert pool.identity_state(PRIMARY) is None
    assert not pool.available(PRIMARY)
    assert pool.snapshot([PRIMARY])[0]["last_penalty"] == "identity_probe_failed"

    unknown[0] = False
    clock[0] += 6  # The existing first quarantine is 5 seconds, not a wrong-chain exclusion.
    assert pool.available(PRIMARY)
    assert call(QUOTER, "0x1234") == "0x01"
    assert bodies[-1][0] == PRIMARY and pool.identity_state(PRIMARY) is True


@pytest.mark.parametrize("reply", [
    _error(None, {"code": -32005, "message": "Service unavailable"}),
    [_error(1, {"code": -32005, "message": "Server busy"}),
     _error(2, {"code": -32005, "message": "Server busy"})],
], ids=["top-level-unavailable", "all-server-busy"])
def test_exhausted_generic_busy_is_not_typed_as_a_quota(reply):
    http, _calls, bodies = _transport(reply, backup_reply=reply)
    with pytest.raises(Q.V3ReadUnavailable) as exc:
        Q.dex_v3_quote("1", WETH, COIN, 100, ROUTER, http=http,
                       cfg_addr={"quoter": QUOTER, "router": ROUTER},
                       slippage_bps=100, fee_tiers=(500, 3000), connectors=())
    assert exc.value.failure_type in {"rpc_transport", "unavailable"}
    _assert_same_read(bodies)


@pytest.mark.parametrize("error", [
    BUSY,
    {"code": -32005, "message": "Too many requests"},
], ids=["rate-limit", "too-many"])
def test_pons_default_reader_keeps_exhausted_quota_as_infrastructure_unavailable(error):
    from sniper.execution import evm_pons_v2_quote as P
    reply = _error(1, error)
    http, _calls, bodies = _transport(reply, backup_reply=reply)
    read = P._make_call_fn(PRIMARY, http=http)
    with pytest.raises(P.PonsReadUnavailable) as exc:
        read(QUOTER, "0x1234")
    assert exc.value.failure_type == "rpc_transport"
    assert Q._classify_rpc_error(reply) == "unavailable"  # Contract imported by Pons readers.
    _assert_same_read(bodies)


def test_busy_failover_never_uses_a_backup_with_wrong_chain_identity(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, WRONG, BACKUP)})
    http, calls, bodies = _transport(_error(1), wrong_chain=WRONG)
    call = Q._make_call_fn("1", http)
    assert call(QUOTER, "0x1234") == "0x01"
    _assert_same_read(bodies)
    assert (WRONG, "identity") in calls and (WRONG, "quote") not in calls
    assert pool.identity_state(WRONG) is False and pool.identity_state(BACKUP) is True
