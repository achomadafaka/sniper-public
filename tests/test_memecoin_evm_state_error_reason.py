"""A provider-busy quarantine names the provider's words (2026-10-07, Alpha P0, chain 1).

MEV Blocker sat out 5 s on 'endpoint_state_unavailable' while Infura was 429ing, so state reads
hit NoEligibleReadEndpoint, and nothing said which body tripped it: the penalty kept the class,
the trace tap the requested alias. The reason now carries the error message, bounded at 60 chars,
at all three penalize sites (single member, rotation, hedge), so the next trip names itself.
"""
import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_wallet as W

PRIMARY = "https://primary.example"
SECOND = "https://second.example"
BUSY = {"jsonrpc": "2.0", "id": 1,
        "error": {"code": -32005, "message": "rate limit exceeded,   retry in 1s"}}
GOOD = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
WORDS = "endpoint_state_unavailable: rate limit exceeded, retry in 1s"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pool.reset_for_tests()
    import sniper.settings.memecoin as real
    monkeypatch.setattr(real, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (PRIMARY, SECOND)},
                        raising=False)
    pool.mark_identity(SECOND, True)
    yield
    pool.reset_for_tests()


def _http(table):
    def http(method, url, headers=None, body=None, timeout=None):
        if body and '"eth_chainId"' in body:
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, {}
        return 200, dict(table[url]), {}
    return http


def test_the_rotation_penalty_names_the_providers_words():
    http = _http({PRIMARY: BUSY, SECOND: GOOD})
    assert W._rpc(PRIMARY, "eth_feeHistory", [1, "latest", []], http=http) == GOOD
    assert pool._entry(PRIMARY)["last_penalty"] == WORDS


def test_the_hedged_penalty_names_the_providers_words():
    http = _http({PRIMARY: BUSY, SECOND: GOOD})
    assert W._rpc(PRIMARY, "eth_call", [{"to": "0x" + "00" * 20, "data": "0x"}, "latest"],
                  http=http) == GOOD
    assert pool._entry(PRIMARY)["last_penalty"] == WORDS


def test_the_single_member_penalty_names_the_providers_words():
    lone = "https://lone.example"
    assert W._rpc(lone, "eth_feeHistory", [1, "latest", []], http=_http({lone: BUSY})) == BUSY
    assert pool._entry(lone)["last_penalty"] == WORDS


def test_the_words_are_bounded_and_a_bare_error_keeps_the_class():
    long = {"error": {"code": -32005, "message": "too many requests " + "x" * 200}}
    reason = W._state_error_reason(long)
    assert reason.startswith("endpoint_state_unavailable: too many requests ")
    assert len(reason) == len("endpoint_state_unavailable: ") + 60
    assert W._state_error_reason({"error": {"code": -32005}}) == "endpoint_state_unavailable"
