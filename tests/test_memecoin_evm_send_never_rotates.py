"""A send never rotates on a busy or unavailable body (Gamma on 014b49edb, Alpha 2026-10-07).

Member A may have broadcast before it answered 'rate limit' or '-32603 Internal error'. Asking B then
gets B's 'nonce too low', which eth_send_raw reads as a positive rejection (False) and the caller
releases a live transaction. The body A gave is returned as it came: the send stays ambiguous (None)."""
import json

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_real_seams as S
from sniper.execution import evm_wallet as W
from sniper.settings import memecoin as cfg

A, B = "https://a.example", "https://b.example"
RAW = "0x02f8" + "00" * 40


@pytest.fixture(autouse=True)
def two(monkeypatch):
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (A, B)})
    for url in (A, B):
        pool.mark_identity(url, True)
    yield
    pool.reset_for_tests()


def _http(first_body, seen):
    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        seen.append((url, req["method"]))
        if url == A:
            return 200, {"jsonrpc": "2.0", "id": 1, "error": first_body}, {}
        if req["method"] == "eth_sendRawTransaction":
            return 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(7)}, {}
    return http


BUSY = [{"code": -32005, "message": "rate limit exceeded"},
        {"code": -32603, "message": "service temporarily unavailable"},
        {"code": -32603, "message": "Internal error"},
        {"code": -32000, "message": "too many requests, try again later"},
        {"code": -32000, "message": "metadata is not found"}]


@pytest.mark.parametrize("body", BUSY)
def test_a_busy_send_answer_is_never_retried_on_another_member(body):
    seen = []
    assert S._eth_send_raw(RAW, A, _http(body, seen)) is None, "ambiguous, never False"
    assert seen == [(A, "eth_sendRawTransaction")], "B is never asked"


@pytest.mark.parametrize("body", BUSY[:2] + BUSY[3:])
def test_a_busy_read_still_walks_on(body):
    seen = []
    assert W._rpc(A, "eth_getTransactionCount", ["0x" + "12" * 20, "pending"],
                  http=_http(body, seen)) == {"jsonrpc": "2.0", "id": 1, "result": hex(7)}
    assert [u for u, _m in seen] == [A, B]


@pytest.mark.parametrize("method, send", [
    ("eth_sendRawTransaction", True), ("eth_sendRawTransactionConditional", True),
    ("eth_sendRawTransactionSync", True), ("eth_sendTransaction", True),
    ("eth_sendPrivateTransaction", True), ("eth_sendBundle", True), ("mev_sendBundle", True),
    ("eth_cancelPrivateTransaction", True), ("eth_call", False), ("eth_getBalance", False),
    ("eth_estimateGas", False), ("eth_getTransactionCount", False)])
def test_every_send_shaped_method_is_classed_as_a_send(method, send):
    assert W._is_send_method(method) is send


@pytest.mark.parametrize("failure", [500, 502, 504, "timeout"])
def test_a_dispatched_send_failure_is_never_retried_on_another_member(failure):
    """Gamma on 6c0a5e0b2: a gateway error or a timeout can follow an accepted broadcast."""
    seen = []

    def http(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        if url == A:
            if failure == "timeout":
                raise TimeoutError("read timed out")
            return failure, {"error": {"code": -32000, "message": "gateway"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}}, {}
    assert S._eth_send_raw(RAW, A, http) is None
    assert seen == [A]



def test_a_429_send_still_rotates_because_it_was_not_forwarded():
    seen = []

    def http(method, url, headers=None, body=None, timeout=10):
        seen.append(url)
        if url == A:
            return 429, {"error": {"code": -32005, "message": "Too Many Requests"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x" + "ab" * 32}, {}
    assert S._eth_send_raw(RAW, A, http) == "0x" + "ab" * 32
    assert seen == [A, B]
