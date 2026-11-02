"""EVM read hedge: cap the 4663 primary's slow tail without ever doubling a write.

Measured 2026-08-14: the Robinhood 4663 primary is bimodal — ~180ms or 1.0-1.4s, 6/10 probes slow —
while the pooled arrowrpc fallback holds a stable ~190ms. Failure ROTATION already existed; nothing
addressed a primary that answers, slowly. The hedge races the next verified endpoint only after the
primary has been quiet for _HEDGE_AFTER_S, so the fast path costs zero extra requests.

The invariant that must never bend: eth_sendRawTransaction also flows through _rpc, and a hedged
broadcast is a DOUBLE-SEND. Writes are excluded by allowlist, not by convention.
"""
import threading
import time

import pytest

from sniper.execution import evm_rpc_pool
from sniper.execution import evm_wallet as W

CHAIN = "4663"
PRIMARY = "https://primary.example"
BACKUP = "https://backup.example"
CHAIN_HEX = hex(int(CHAIN))


@pytest.fixture(autouse=True)
def _pool(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {CHAIN: (PRIMARY, BACKUP)})
    evm_rpc_pool._state.clear()
    # both endpoints pre-verified so choose() does not re-probe identity mid-test
    for u in (PRIMARY, BACKUP):
        evm_rpc_pool._entry(u).update({"chain_ok": True, "checked": evm_rpc_pool._now()})
    yield
    evm_rpc_pool._state.clear()


def _http_for(behaviour):
    """behaviour: {url: (delay_s, result_or_exc)}; identity probes answer instantly."""
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        if '"eth_chainId"' in (body or "") and url in behaviour:
            return 200, {"jsonrpc": "2.0", "id": 1, "result": CHAIN_HEX}, b""
        calls.append(url)
        delay, result = behaviour[url]
        if delay:
            time.sleep(delay)
        if isinstance(result, Exception):
            raise result
        return 200, {"jsonrpc": "2.0", "id": 1, "result": result}, b""
    http.calls = calls
    return http


def test_fast_primary_never_hedges():
    http = _http_for({PRIMARY: (0.0, "0xaa"), BACKUP: (0.0, "0xbb")})
    j = W._rpc(PRIMARY, "eth_blockNumber", [], http=http)
    assert j["result"] == "0xaa"
    assert http.calls == [PRIMARY], "a fast primary must cost zero extra requests"


def test_slow_primary_is_hedged_and_the_backup_wins():
    http = _http_for({PRIMARY: (1.2, "0xaa"), BACKUP: (0.05, "0xbb")})
    t0 = time.monotonic()
    j = W._rpc(PRIMARY, "eth_blockNumber", [], http=http)
    took = time.monotonic() - t0
    assert j["result"] == "0xbb", "the first answer wins"
    assert took < 1.0, "the caller must not wait out the slow primary (took %.2fs)" % took
    assert BACKUP in http.calls


def test_writes_are_never_hedged():
    """A hedged eth_sendRawTransaction is a double-broadcast. The slow path stays slow instead."""
    http = _http_for({PRIMARY: (0.6, "0xsig"), BACKUP: (0.0, "0xother")})
    j = W._rpc(PRIMARY, "eth_sendRawTransaction", ["0xdead"], http=http)
    assert j["result"] == "0xsig"
    assert http.calls == [PRIMARY], "the backup must never see a write"


def test_send_raw_transaction_is_not_in_the_allowlist():
    assert "eth_sendRawTransaction" not in W._HEDGEABLE_READS


def test_hedged_primary_failure_falls_to_the_backup_answer():
    http = _http_for({PRIMARY: (0.6, OSError("transport")), BACKUP: (0.05, "0xbb")})
    j = W._rpc(PRIMARY, "eth_blockNumber", [], http=http)
    assert j["result"] == "0xbb"


def test_fast_provider_metadata_error_falls_to_the_backup_answer():
    calls = []

    def http(_method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        if url == PRIMARY:
            return 200, {"error": {
                "code": -32000, "message": "metadata is not found, 51044310",
            }}, None
        return 200, {"result": "0xbb"}, None

    result = W._rpc(PRIMARY, "eth_call", [{}, "0x30adfd3"], http=http)

    assert result["result"] == "0xbb"
    assert calls == [PRIMARY, BACKUP]


def test_both_hedge_legs_failing_still_raises():
    http = _http_for({PRIMARY: (0.0, OSError("down")), BACKUP: (0.0, OSError("down"))})
    with pytest.raises(Exception):
        W._rpc(PRIMARY, "eth_blockNumber", [], http=http)


def test_single_endpoint_chain_is_byte_identical_behaviour():
    from sniper.settings import memecoin as cfg
    cfg.MEMECOIN_EVM_RPC_POOL_URLS = {}
    http = _http_for({PRIMARY: (0.0, "0xaa")})
    j = W._rpc(PRIMARY, "eth_blockNumber", [], http=http)
    assert j["result"] == "0xaa"
    assert http.calls == [PRIMARY]


# --- the unbounded wait (2026-09-03) ---------------------------------------------------------
# The hedge bounds only its FIRST wait (_HEDGE_AFTER_S, before racing the backup). The loop that
# collects the answers was `wait(pending, return_when=FIRST_COMPLETED)` with NO timeout, so a
# worker that never completes blocks the CALLING thread forever, silently: nothing raises, nothing
# logs, and the caller has no deadline of its own.
#
# On 2026-09-02 the funded runner wedged for 99s at 19:51:22. The main loop printed `cycles=1` and
# never printed again; the fast lane wrote its last mark at 19:51:21.8. BOTH threads stopped inside
# 0.4s of each other, no error was logged, and only SIGTERM ended it. The watchdog then killed the
# runner for `sweep_stale` — which trips ~30s before `stale` because `bracket_ts` is structurally
# one cycle older than `ts` (90s vs 120s thresholds). Three kills in one hour.
#
# A per-socket timeout does not bound this: it does not cover name resolution, and it applies per
# read rather than to the whole exchange. The wait itself must carry the deadline.

def _blocking_http(gate, answered):
    """Both endpoints hang until `gate` is set. Identity probes still answer instantly."""
    def http(method, url, headers=None, body=None, timeout=10):
        if '"eth_chainId"' in (body or ""):
            return 200, {"jsonrpc": "2.0", "id": 1, "result": CHAIN_HEX}, b""
        gate.wait(30)
        answered.append(url)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0xlate"}, b""
    return http


def test_hedge_gives_up_when_no_endpoint_ever_answers(monkeypatch):
    """The wedge: neither leg completes. The call MUST return on its own budget."""
    monkeypatch.setattr(W, "_HEDGE_TOTAL_S", 0.5)
    gate, answered = threading.Event(), []
    http = _blocking_http(gate, answered)
    try:
        t0 = time.monotonic()
        out = W._rpc_hedged_read(
            PRIMARY, "{}", http, evm_rpc_pool, lambda *a, **k: True, [PRIMARY, BACKUP])
        elapsed = time.monotonic() - t0
    finally:
        gate.set()
    assert elapsed < 5.0, (
        "the hedge blocked %.1fs on workers that never answered; this is the funded wedge" % elapsed)
    assert out is None, "no answer means no answer — fall through to the sequential rotation"
    assert answered == [], "the test's own premise: neither endpoint answered inside the budget"


def test_hedge_timeout_quarantines_the_endpoints_that_never_answered(monkeypatch):
    """A silent endpoint must cost the pool something, or the next tick picks it again."""
    monkeypatch.setattr(W, "_HEDGE_TOTAL_S", 0.5)
    gate, answered = threading.Event(), []
    penalized = []
    monkeypatch.setattr(evm_rpc_pool, "penalize",
                        lambda url, reason="", quarantine_s=None: penalized.append((url, reason)))
    try:
        W._rpc_hedged_read(PRIMARY, "{}", _blocking_http(gate, answered), evm_rpc_pool,
                           lambda *a, **k: True, [PRIMARY, BACKUP])
    finally:
        gate.set()
    assert penalized, "a silent endpoint was never penalized, so the pool keeps choosing it"
    assert {u for u, _ in penalized} == {PRIMARY, BACKUP}
    assert all(r == "hedged_read_timeout" for _, r in penalized), penalized


def test_a_slow_but_answering_primary_is_not_cut_off_by_the_budget():
    """The budget bounds a HANG, not a slow answer. _http already owns the per-call timeout."""
    http = _http_for({PRIMARY: (0.9, "0xaa"), BACKUP: (5.0, "0xbb")})
    t0 = time.monotonic()
    out = W._rpc_hedged_read(
        PRIMARY, "{}", http, evm_rpc_pool, lambda *a, **k: True, [PRIMARY, BACKUP])
    assert out["result"] == "0xaa", "a primary answering inside the budget still wins"
    assert time.monotonic() - t0 < 3.0


def test_the_total_budget_exceeds_the_per_call_socket_timeout():
    """Ordering guard: a budget under the 10s socket timeout would pre-empt every normal timeout
    and turn ordinary provider latency into a pool quarantine."""
    assert W._HEDGE_TOTAL_S > 10.0, W._HEDGE_TOTAL_S
    assert W._HEDGE_TOTAL_S > W._HEDGE_AFTER_S


def test_a_measurably_stale_backup_is_not_raced():
    """The hedge picks a winner by SPEED, and a stale node is fast because it answers from old
    state. Measured on 4663 (2026-09-04): both Alchemy members ran ~750 blocks (~75s) behind while
    the chain owner sat at lag 0. A stale nonce or balance is a WRONG VALUE, not a slow one."""
    from sniper.execution import evm_wallet as w
    from sniper.execution import evm_rpc_pool as pool

    pool.reset_for_tests()
    current, stale = "https://current.invalid", "https://stale.invalid"
    pool.record_heads({current: 1_000_000, stale: 999_250})     # 750 blocks behind
    assert w._too_stale_to_race(pool, stale) is True
    assert w._too_stale_to_race(pool, current) is False

    asked = []

    def http(_m, url, body=None, **_k):
        asked.append(url)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}, ""

    # No eligible backup -> the hedge declines and the sequential rotation (correctly scored) owns
    # the read. The stale member is never raced.
    assert w._rpc_hedged_read(current, "{}", http, pool, lambda _c: True,
                              [current, stale], method="eth_getTransactionCount") is None
    assert stale not in asked
    pool.reset_for_tests()


def test_a_current_backup_still_races():
    # The staleness filter must not disable hedging for a healthy, current pool.
    from sniper.execution import evm_wallet as w
    from sniper.execution import evm_rpc_pool as pool

    pool.reset_for_tests()
    a, b = "https://a.invalid", "https://b.invalid"
    pool.record_heads({a: 1_000_000, b: 999_999})               # 1 block apart
    assert w._too_stale_to_race(pool, b) is False
    answer = w._rpc_hedged_read(
        a, "{}", lambda _m, _u, body=None, **_k: (200, {"jsonrpc": "2.0", "id": 1,
                                                        "result": "0x7"}, ""),
        pool, lambda _c: True, [a, b], method="eth_getBalance")
    assert answer["result"] == "0x7"
    pool.reset_for_tests()
