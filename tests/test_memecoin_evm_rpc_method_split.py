"""4663 RPC pool: which endpoint may serve which request SHAPE.

Regression pin for 2026-08-30. publicnode is faster than the chain's official endpoint on every
funded method, but it REFUSES an unfiltered `eth_getLogs` with -32701 in 58 ms while the official
endpoint spends 448 ms actually serving it. Our tickstream scans the whole chain by design, and
`eth_getLogs` is hedged first-answer-wins, so adding publicnode without a split lets a fast
refusal beat a slow correct answer and thins the tape silently.
"""
import time

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_wallet as W

PRIMARY = "https://rpc.mainnet.chain.robinhood.com"
SECONDARY = "https://robinhood-rpc.publicnode.com"
REFUSAL = {"jsonrpc": "2.0", "id": 1,
           "error": {"code": -32701, "message": "Please specify an address in your request"}}


@pytest.fixture(autouse=True)
def _pooled(monkeypatch):
    pool.reset_for_tests()
    monkeypatch.setattr(pool, "pool_for", lambda url, cfg=None: [PRIMARY, SECONDARY])
    monkeypatch.setattr(pool, "chain_of", lambda url, cfg=None: "4663")
    pool.mark_identity(PRIMARY, True)      # skip the chainId probe; identity is not under test
    pool.mark_identity(SECONDARY, True)
    yield
    pool.reset_for_tests()


# ------------------------------------------------------------------ the shape rule itself

@pytest.mark.parametrize("params,eligible", [
    ([{"fromBlock": "0x1", "toBlock": "0x2"}], False),                 # the tickstream's shape
    ([{"fromBlock": "0x1", "toBlock": "0x2", "address": []}], False),  # empty list is unfiltered
    ([{"fromBlock": "0x1", "address": "  "}], False),                  # blank is unfiltered
    ([{"fromBlock": "0x1", "address": None}], False),
    ([], False),                                                       # no filter at all
    (["not-a-dict"], False),                                           # unparseable -> pin
    ([{"address": "0xabc"}], True),                                    # named address
    ([{"address": ["0xabc", "0xdef"]}], True),                         # named list
])
def test_getlogs_pool_eligibility_is_decided_by_the_filter(params, eligible):
    assert W._pool_eligible("eth_getLogs", params) is eligible


def test_every_other_method_may_use_the_pool():
    for method in ("eth_call", "eth_getBalance", "eth_gasPrice",
                   "eth_getTransactionCount", "eth_blockNumber", "eth_chainId"):
        assert W._pool_eligible(method, [{}]) is True


# ------------------------------------------------------------------ REQUIRED TEST 1

def test_an_unfiltered_getlogs_NEVER_reaches_the_secondary(monkeypatch):
    """The tape's guarantee is gaps, never inventions. A silently thinned tape is worse than a
    slow one, and every downstream study trusts it.

    THE PRIMARY MUST BE SLOW HERE. With a fast primary the hedge never fires, the secondary is
    never contacted, and the test passes whether or not the pin exists — it would be asserting
    the timing, not the rule. Caught by the mutation check: removing the pin left this green.

    Mutation check: delete the `_pool_eligible` pin in `_rpc` and this goes red.
    """
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.01)
    seen = []

    def http(_method, url, body=None, **_kw):
        seen.append(url)
        if url == SECONDARY:
            return 200, REFUSAL, None
        time.sleep(0.25)                   # slow primary: without the pin the hedge WOULD fire
        return 200, {"jsonrpc": "2.0", "id": 1, "result": [{"log": 1}]}, None

    out = W._rpc(PRIMARY, "eth_getLogs",
                 [{"fromBlock": "0x1", "toBlock": "0x2"}], http=http)

    assert SECONDARY not in seen, (
        f"an unfiltered eth_getLogs reached {SECONDARY}, which refuses that shape in 58ms "
        f"and would win the hedge against a 448ms correct answer")
    assert out.get("result") == [{"log": 1}]


def test_an_address_filtered_getlogs_MAY_use_the_secondary(monkeypatch):
    """The positive control. Pinning everything would throw away the whole gain."""
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.01)
    seen = []

    def http(_method, url, body=None, **_kw):
        seen.append(url)
        if url == PRIMARY:
            time.sleep(0.20)               # primary in its slow mode -> the hedge fires
        return 200, {"jsonrpc": "2.0", "id": 1, "result": [{"from": url}]}, None

    W._rpc(PRIMARY, "eth_getLogs", [{"address": "0xabc"}], http=http)
    assert SECONDARY in seen, "the pool is unused for a shape both endpoints serve"


# ------------------------------------------------------------------ REQUIRED TEST 2

def test_a_fast_refusal_cannot_win_a_hedged_race(monkeypatch):
    """Second layer, behind the shape pin. Uses an ADDRESS-FILTERED request on purpose: an
    unfiltered one is pinned to the primary and would pass this test without exercising the hedge
    at all — passing for the wrong reason.

    "First answer wins" was written for endpoints that agree on what a request MEANS. Where they
    disagree, the one that declines is the quicker.

    Mutation check: make the hedge `return fut.result()` on the first completion and this goes red.
    """
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.01)

    def http(_method, url, body=None, **_kw):
        if url == SECONDARY:
            return 200, REFUSAL, None      # fast and wrong
        time.sleep(0.25)                   # slow and right
        return 200, {"jsonrpc": "2.0", "id": 1, "result": [{"log": "real"}]}, None

    out = W._rpc(PRIMARY, "eth_getLogs", [{"address": "0xabc"}], http=http)

    assert "error" not in out, f"a -32701 refusal won the hedged race: {out}"
    assert out.get("result") == [{"log": "real"}]


def test_a_refusal_is_still_returned_when_it_is_all_there_is(monkeypatch):
    """A valid JSON-RPC error is the endpoint working correctly. If BOTH decline, that IS the
    answer — swallowing it would turn a real bad-request into a phantom transport failure."""
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.01)

    def http(_method, url, body=None, **_kw):
        if url == PRIMARY:
            time.sleep(0.05)
        return 200, REFUSAL, None

    out = W._rpc(PRIMARY, "eth_getLogs", [{"address": "0xabc"}], http=http)
    assert out.get("error", {}).get("code") == -32701


def test_a_refusal_does_not_quarantine_the_endpoint(monkeypatch):
    """penalize() is for provider failure. An endpoint that declines a request shape is healthy,
    and quarantining it would cost us its speed on every method it DOES serve."""
    monkeypatch.setattr(W, "_HEDGE_AFTER_S", 0.01)

    def http(_method, url, body=None, **_kw):
        if url == SECONDARY:
            return 200, REFUSAL, None
        time.sleep(0.05)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": []}, None

    W._rpc(PRIMARY, "eth_getLogs", [{"address": "0xabc"}], http=http)
    assert pool.available(SECONDARY), "a JSON-RPC refusal quarantined a healthy endpoint"


@pytest.mark.parametrize('method', ['eth_call', 'eth_getCode'])
def test_safe_read_structural_refusal_asks_the_next_member_within_the_same_request(method):
    seen = []

    def http(_method, url, **kwargs):
        seen.append(url)
        if url == PRIMARY:
            return 403, {'error': {'code': -32602, 'message': 'plan unavailable'}}, None
        return 200, {'result': '0x123'}, None

    http.safety_deadline_active = lambda: True
    answer = W._rpc(PRIMARY, method, [{'to': '0x' + 'a' * 40}, 'latest'], http=http)
    assert answer == {'result': '0x123'}
    assert seen == [PRIMARY, SECONDARY]
    assert pool.available(PRIMARY)


def test_structural_write_refusal_does_not_gain_new_cross_member_retry():
    seen = []

    def http(_method, url, **kwargs):
        seen.append(url)
        return 403, {'error': {'code': -32602, 'message': 'plan unavailable'}}, None

    answer = W._rpc(PRIMARY, 'eth_sendRawTransaction', ['0xsigned'], http=http)
    assert answer['error']['code'] == -32602
    assert SECONDARY not in seen


@pytest.mark.parametrize('identity', [None, False])
def test_safe_read_refusal_never_uses_an_unproved_or_wrong_chain_backup(identity):
    pool.mark_identity(SECONDARY, identity)
    seen = []

    def http(_method, url, body=None, **kwargs):
        import json
        method = json.loads(body)['method']
        seen.append((url, method))
        if method == 'eth_chainId':
            return 200, {'result': '0x1'}, None
        if url == PRIMARY:
            return 403, {'error': {'code': -32602, 'message': 'plan unavailable'}}, None
        return 200, {'result': '0xwrong_chain'}, None

    http.safety_deadline_active = lambda: True
    answer = W._rpc(PRIMARY, 'eth_call', [{'to': '0x' + 'a' * 40}, 'latest'], http=http)
    assert answer['error']['code'] == -32602
    assert (SECONDARY, 'eth_call') not in seen
