"""A benched endpoint's re-admit probe never runs on the caller's thread.

2026-09-27 12:57Z: every funded custody call (entry reads, exit previews, sends, confirms) first ran
_refresh_one, and whenever a benched endpoint's window had expired the CALLER sent the
getGenesisHash probe with its own timeout before serving its request. During the Wi-Fi outage
those endpoints were still dead, so a caller holding the funded exit lock paid a dead probe first.
The probe now runs on its own daemon thread; a re-admitted endpoint serves the NEXT call.
"""
import json
import threading
import time

from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp

H1, H2 = "https://h1/rpc", "https://h2/rpc"


def _body(method="getBalance"):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": ["P"]})


def _pool(http, now):
    return RotatingSolanaRpcHttp(http, (H1, H2), retry_s=3600.0, transient_retry_s=60.0,
                                 clock=lambda: now[0])


def _bench_h1(pool):
    pool._advance(0, reason="http_429")
    assert pool.snapshot()["active_index"] == 1


def test_a_caller_never_waits_on_the_probe_and_is_served_by_the_active_endpoint():
    now, calls = [0.0], []

    def http(_m, url, **kw):
        method = json.loads(kw["body"])["method"]
        calls.append((url, method))
        if method == "getGenesisHash":
            time.sleep(1.0)                                     # a slow, dying endpoint
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": url}, None

    pool = _pool(http, now)
    _bench_h1(pool)
    now[0] = 61.0                                               # the short bench has expired
    t0 = time.monotonic()
    status, payload, _ = pool("POST", H1, body=_body())
    assert time.monotonic() - t0 < 0.2, "the caller waited on the probe"
    assert payload["result"] == H2                              # served by the endpoint active now
    pool._probe_thread.join(3)


def test_the_benched_endpoint_is_still_readmitted_and_serves_the_next_call():
    now = [0.0]

    def http(_m, url, **kw):
        if json.loads(kw["body"])["method"] == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": url}, None

    pool = _pool(http, now)
    _bench_h1(pool)
    now[0] = 61.0
    pool("POST", H1, body=_body())
    pool._probe_thread.join(3)
    assert pool.snapshot()["active_index"] == 0 and pool.snapshot()["blocked_indexes"] == []
    assert pool("POST", H1, body=_body())[1]["result"] == H1


def test_a_failed_background_probe_rebenches_on_its_own_reason():
    now = [0.0]

    def http(_m, url, **kw):
        if url == H1:
            raise OSError("still dead")
        return 200, {"result": url}, None

    pool = _pool(http, now)
    _bench_h1(pool)                                             # benched on http_429: 60 s
    now[0] = 61.0
    pool("POST", H1, body=_body())
    pool._probe_thread.join(3)
    assert pool.snapshot()["blocked_indexes"] == [0]
    assert pool._blocked_until[0] == 61.0 + 60.0


def test_concurrent_callers_start_one_probe():
    now, probes, gate = [0.0], [], threading.Event()

    def http(_m, url, **kw):
        if json.loads(kw["body"])["method"] == "getGenesisHash":
            probes.append(url)
            gate.wait(2)
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": url}, None

    pool = _pool(http, now)
    _bench_h1(pool)
    now[0] = 61.0
    workers = [threading.Thread(target=lambda: pool("POST", H1, body=_body())) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(2)
    gate.set()
    pool._probe_thread.join(3)
    assert probes == [H1]


def test_a_hung_probe_is_abandoned_after_30_s_and_its_late_result_changes_nothing():
    from sniper.execution.solana_rpc_pool import PROBE_CLAIM_STALE_S
    now, release, probes = [0.0], threading.Event(), []

    def http(_m, url, **kw):
        if json.loads(kw["body"])["method"] == "getGenesisHash":
            probes.append(url)
            if len(probes) == 1:
                release.wait(5)                                 # stuck (as in DNS) until released
                return 200, {"result": SOLANA_MAINNET_GENESIS}, None    # ...then a LATE "restored"
            raise OSError("still dead")                         # the newer probe: still benched
        return 200, {"result": url}, None

    pool = _pool(http, now)
    _bench_h1(pool)
    now[0] = 61.0
    pool("POST", H1, body=_body())
    hung = pool._probe_thread
    pool("POST", H1, body=_body())
    assert len(probes) == 1                                     # the slot is held: no second probe
    now[0] = 61.0 + PROBE_CLAIM_STALE_S + 1.0
    pool("POST", H1, body=_body())
    pool._probe_thread.join(3)
    assert len(probes) == 2                                     # the stale claim was abandoned
    rebenched = pool._blocked_until[0]
    assert rebenched == now[0] + 60.0
    release.set()
    hung.join(3)
    assert pool._blocked_until[0] == rebenched                  # the late result was ignored
    assert pool.snapshot()["active_index"] == 1
