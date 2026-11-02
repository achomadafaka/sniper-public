"""Exact route/result binding; no provider, signing or main-ledger access."""
import json

import pytest

from sniper.execution import evm_dex_quote as v3, evm_v2_exit_quote as v2, evm_rpc_pool
from sniper.execution.evm_rpc_pool import RequestNotSent
from test_memecoin_evm_v2_exit_quote import (
    ROUTER, TAKER, TOKEN, CONNECTOR, WETH, _amounts_result,
    _amount_from_get_amounts, _path_from_get_amounts,
)


@pytest.fixture(autouse=True)
def isolated_capability_state():
    evm_rpc_pool.reset_for_tests()
    yield
    evm_rpc_pool.reset_for_tests()


def quote(call, *, connectors=(CONNECTOR,), blind=None):
    return v2.dex_v2_exit_quote(
        "56", TOKEN, WETH, 1000, TAKER, cfg_router=ROUTER,
        connector_tokens=connectors, slippage_bps=100, tax_buffer_bps=0,
        call_fn=call, now_fn=lambda: 1000, blind_out=blind)


def value(call):
    path = _path_from_get_amounts(call[1])
    amount = _amount_from_get_amounts(call[1])
    return _amounts_result([amount] + [600] * (len(path) - 2) + [700 + len(path)])


def test_existing_routes_in_one_batch_preserve_exact_quote_and_calldata():
    scalar = lambda *call: value(call)
    expected = quote(scalar)
    seen = []
    def forbidden(*_):
        pytest.fail("serial path read")
    def batch(calls):
        seen.extend(calls)
        return [value(call) for call in calls]
    forbidden.batch = batch
    assert quote(forbidden) == expected
    assert len(seen) == 2


def test_single_path_remains_scalar():
    calls = []
    def scalar(*call):
        calls.append(call)
        return value(call)
    scalar.batch = lambda _: pytest.fail("single-path batch")
    assert quote(scalar, connectors=()) is not None
    assert len(calls) == 1


@pytest.mark.parametrize("results,reason", [
    ([v2.V2ReadUnavailable("Too Many Requests")] * 2, "rpc_rate_limit"),
    ([v2.V2ReadUnavailable("rpc_rate_limit: rate limit exceeded")] * 2, "rpc_rate_limit"),
    ([v2.V2ReadUnavailable("timeout")] * 2, "rpc_transport"),
    ([None], "rpc_transport"),
])
def test_unavailable_or_partial_batch_never_becomes_terminal_empty_book(results, reason):
    def forbidden(*_):
        pytest.fail("failed batch must not fan out")
    forbidden.batch = lambda _: results
    blind = {}
    assert quote(forbidden, blind=blind) is None
    assert blind["reason"] == reason


def test_revert_is_a_decided_miss_and_other_path_can_win():
    def forbidden(*_):
        pytest.fail("serial read")
    forbidden.batch = lambda calls: [v2.V2Reverted("no pair"), value(calls[1])]
    blind = {}
    result = quote(forbidden, blind=blind)
    assert result["route"]["path"] == [TOKEN.lower(), CONNECTOR.lower(), WETH.lower()]
    assert blind == {}


def test_reverted_batch_is_not_provider_blindness():
    call = lambda *_: pytest.fail("serial read")
    call.batch = lambda _: [v2.V2Reverted("no pair")] * 2
    blind = {}
    assert quote(call, blind=blind) is None
    assert blind == {}


def test_explicit_unsupported_batch_retains_scalar_compatibility():
    call = lambda *args: value(args)
    call.batch = lambda _: None
    assert quote(call) == quote(lambda *args: value(args))


def test_caller_budget_refusal_does_not_retry_or_convert_to_provider_failure():
    call = lambda *_: pytest.fail("serial read")
    def batch(_):
        raise RequestNotSent("deadline")
    call.batch = batch
    with pytest.raises(RequestNotSent):
        quote(call)


def transport(monkeypatch, response):
    rpc = "https://bsc.invalid"
    monkeypatch.setattr(evm_rpc_pool, "choose", lambda *_a, **_k: rpc)
    seen = []
    def http(method, target, *, body):
        assert target == rpc and method == "POST"
        request = json.loads(body)
        seen.append(request)
        if isinstance(request, list):
            return 200, response(request), None
        return 200, {"jsonrpc": "2.0", "id": 1, "result": value(
            (request["params"][0]["to"], request["params"][0]["data"]))}, None
    return v2._make_call_fn("56", http, rpc=rpc), seen


def test_default_batch_binds_out_of_order_ids_and_preserves_per_item_revert(monkeypatch):
    def response(req):
        return [dict(jsonrpc="2.0", id=2, result=value(
            (req[1]["params"][0]["to"], req[1]["params"][0]["data"]))),
            dict(jsonrpc="2.0", id=1, error={"code": 3, "message": "execution reverted"})]
    call, requests = transport(monkeypatch, response)
    assert quote(call)["buyAmount"] == "703"
    assert len(requests) == 1 and isinstance(requests[0], list)


@pytest.mark.parametrize("response", [
    lambda _: [{"id": 1, "result": "0x01"}],
    lambda _: [{"id": 1, "result": "0x01"}, {"id": 1, "result": "0x02"}],
    lambda _: [{"id": True, "result": "0x01"}, {"id": 2, "result": "0x02"}],
    lambda _: [{"id": 1, "result": "0x01"}, {"id": 99, "result": "0x02"}],
    lambda _: {"error": {"code": -32005, "message": "Too Many Requests"}},
])
def test_default_batch_rejects_missing_duplicate_wrong_or_quota_answers(monkeypatch, response):
    call, requests = transport(monkeypatch, response)
    blind = {}
    assert quote(call, blind=blind) is None
    assert blind["reason"] in {"rpc_transport", "rpc_rate_limit"}
    assert requests and all(isinstance(req, list) for req in requests)


def test_default_explicit_batch_unsupported_falls_back_once_per_existing_path(monkeypatch):
    call, requests = transport(monkeypatch, lambda _: {
        "error": {"code": -32600, "message": "Batch requests are not supported"}})
    assert quote(call)["buyAmount"] == "703"
    assert [isinstance(req, list) for req in requests] == [True, False, False]


def test_known_unsupported_transport_does_not_probe_again_on_the_next_quote(monkeypatch):
    evm_rpc_pool.reset_for_tests()
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda rpc: [rpc])
    call, requests = transport(monkeypatch, lambda _: {
        "error": {"code": -32600, "message": "Batch requests are not supported"}})
    try:
        assert quote(call)["buyAmount"] == "703"
        assert quote(call)["buyAmount"] == "703"
        assert [isinstance(req, list) for req in requests] == [True, False, False, False, False]
        rebuilt, later_requests = transport(monkeypatch, lambda _: pytest.fail("cached capability lost"))
        assert quote(rebuilt)["buyAmount"] == "703"
        assert [isinstance(req, list) for req in later_requests] == [False, False]
    finally:
        evm_rpc_pool.reset_for_tests()


@pytest.mark.parametrize("chain_id", ["0x1", None, "0x38"])
def test_filtered_sole_secondary_requires_chain_proof_before_batch(monkeypatch, chain_id):
    primary, secondary = "https://primary.invalid", "https://secondary.invalid"
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda _: [primary, secondary])
    evm_rpc_pool.note_refusal(primary, "batch_unsupported", "eth_call_batch")
    requests = []
    def http(method, target, *, body):
        assert target == secondary and method == "POST"
        request = json.loads(body)
        requests.append(request)
        if isinstance(request, dict):
            assert request["method"] == "eth_chainId"
            return 200, {"id": 1, "result": chain_id}, None
        assert chain_id == "0x38", "quote read preceded valid chain identity"
        return 200, [{"id": item["id"], "result": value(
            (item["params"][0]["to"], item["params"][0]["data"]))} for item in request], None
    call = v2._make_call_fn("56", http, rpc=primary)
    result = quote(call)
    assert (result is not None) == (chain_id == "0x38")
    assert isinstance(requests[0], dict)
    assert any(isinstance(req, list) for req in requests) == (chain_id == "0x38")


def test_v3_rejects_cross_bound_duplicate_batch_ids(monkeypatch):
    monkeypatch.setattr(evm_rpc_pool, "choose", lambda *_a, **_k: "https://bsc.invalid")
    def http(*_, **_kw):
        return 200, [{"id": 1, "result": "0x01"}, {"id": 1, "result": "0x02"}], None
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://bsc.invalid")
    call = v3._make_call_fn("56", http)
    assert call.batch([(ROUTER, "0x12"), (ROUTER, "0x34")]) == [None, None]
    assert call.read_state["answered"] is False
    assert call.read_state["unavailable"] is not None
