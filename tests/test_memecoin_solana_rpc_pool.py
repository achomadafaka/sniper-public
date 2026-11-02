"""Funded Solana RPC credit rotation.

These tests are offline. They exercise the real selector against an injected transport and prove
that only Helius' explicit credit-exhaustion code can move a funded request to another endpoint.
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
from sniper.settings.memecoin import solana_funded_rpc_urls


def test_configured_pool_requires_three_https_urls_and_appends_legacy_fallback():
    env = {
        "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1/rpc, https://h2/rpc,https://h3/rpc",
        "MEMECOIN_SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com",
    }

    assert solana_funded_rpc_urls(env) == (
        "https://h1/rpc",
        "https://h2/rpc",
        "https://h3/rpc",
        "https://api.mainnet-beta.solana.com",
    )


def test_absent_pool_preserves_legacy_single_url_and_disarmed_empty_url():
    assert solana_funded_rpc_urls({"MEMECOIN_SOLANA_RPC_URL": "https://legacy"}) == (
        "https://legacy",
    )
    assert solana_funded_rpc_urls({}) == ()


@pytest.mark.parametrize("env", [
    {"MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,https://h2",
     "MEMECOIN_SOLANA_RPC_URL": "https://public"},
    {"MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,https://h2,https://h2",
     "MEMECOIN_SOLANA_RPC_URL": "https://public"},
    {"MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,http://h2,https://h3",
     "MEMECOIN_SOLANA_RPC_URL": "https://public"},
    {"MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,https://h2,https://h3"},
    {"MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,https://h2,https://h3",
     "MEMECOIN_SOLANA_RPC_URL": "https://h3"},
])
def test_malformed_or_incomplete_pool_fails_closed_without_echoing_secrets(env):
    with pytest.raises(ValueError) as exc:
        solana_funded_rpc_urls(env)
    assert "h1" not in str(exc.value)
    assert "h2" not in str(exc.value)
    assert "h3" not in str(exc.value)


def test_credit_exhaustion_retries_the_identical_request_on_next_endpoint():
    calls = []

    def http(method, url, **kwargs):
        calls.append((method, url, kwargs["body"], kwargs.get("timeout")))
        if url == "https://h1/rpc":
            return 200, {"error": {"code": -32429, "message": "max usage reached"}}, None
        return 200, {"result": {"value": 7}}, None

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": ["P"]})

    result = rpc("POST", "https://h1/rpc", body=body, timeout=3.0)

    assert result == (200, {"result": {"value": 7}}, None)
    assert calls == [
        ("POST", "https://h1/rpc", body, 3.0),
        ("POST", "https://h2/rpc", body, 3.0),
    ]
    snap = rpc.snapshot()
    assert {k: snap[k] for k in
            ("active_index", "endpoint_count", "exhausted_count", "fallback_active")} == {
        "active_index": 1,
        "endpoint_count": 2,
        "exhausted_count": 1,
        "fallback_active": True,
    }
    # The operator asked to KNOW when we are not on the fastest path, so the snapshot has to say
    # so rather than leaving it to be inferred from active_index.
    assert snap["degraded"] is True
    assert snap["last_reason"] == "credit_exhausted"
    assert snap["blocked_indexes"] == [0]


def test_non_rpc_urls_pass_through_without_pool_substitution():
    calls = []

    def http(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return 200, {"outAmount": "1"}, None

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://public/rpc"))

    assert rpc("GET", "https://quote-api.jup.ag/v6/quote", timeout=10) == (
        200, {"outAmount": "1"}, None)
    assert calls[0][1] == "https://quote-api.jup.ag/v6/quote"
    assert rpc.snapshot()["active_index"] == 0


@pytest.mark.parametrize("response", [
    (429, {"error": "rate"}, None),
    (503, {"error": "down"}, None),
    (200, {"error": {"code": -32000, "message": "node error"}}, None),
    (200, {"error": {"code": "-32429", "message": "wrong type"}}, None),
    (200, {"garbage": True}, None),
])
def test_transient_ambiguous_and_malformed_responses_do_not_rotate(response):
    calls = []
    rpc = RotatingSolanaRpcHttp(
        lambda _method, url, **_kwargs: calls.append(url) or response,
        ("https://h1/rpc", "https://h2/rpc"),
    )

    assert rpc("POST", "https://h1/rpc", body="{}") == response
    assert calls == ["https://h1/rpc"]
    assert rpc.snapshot()["active_index"] == 0


def test_transport_exception_does_not_rotate():
    calls = []

    def http(_method, url, **_kwargs):
        calls.append(url)
        raise TimeoutError("unknown after transport")

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))

    with pytest.raises(TimeoutError):
        rpc("POST", "https://h1/rpc", body="{}")
    assert calls == ["https://h1/rpc"]
    assert rpc.snapshot()["active_index"] == 0


def test_two_concurrent_exhaustion_responses_advance_only_one_endpoint():
    barrier = threading.Barrier(2)

    def http(_method, url, **_kwargs):
        if url == "https://h1/rpc":
            barrier.wait(timeout=2)
            return 200, {"error": {"code": -32429}}, None
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(
        http, ("https://h1/rpc", "https://h2/rpc", "https://h3/rpc"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _unused: rpc("POST", "https://h1/rpc", body="{}"), range(2)))

    assert all(result == (200, {"result": "ok"}, None) for result in results)
    assert rpc.snapshot()["active_index"] == 1
    assert rpc.snapshot()["exhausted_count"] == 1


def test_read_only_cooldown_probe_restores_a_refilled_endpoint():
    now = [0.0]
    refilled = [False]
    calls = []

    def http(_method, url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        calls.append((url, rpc_method))
        if url == "https://h1/rpc" and not refilled[0]:
            return 200, {"error": {"code": -32429}}, None
        if rpc_method == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(
        http, ("https://h1/rpc", "https://h2/rpc"),
        retry_s=3600, clock=lambda: now[0],
    )
    balance_body = json.dumps({"method": "getBalance"})
    rpc("POST", "https://h1/rpc", body=balance_body)
    assert rpc.snapshot()["active_index"] == 1

    refilled[0] = True
    now[0] = 3601.0
    rpc("POST", "https://h1/rpc", body=balance_body)
    # The probe runs on its own thread since 2026-09-27: this call is served by whichever endpoint
    # is active when it reads the pool (the stub probe may already have re-admitted h1), and the
    # re-admitted endpoint serves the next call either way.
    rpc._probe_thread.join(3)
    assert ("https://h1/rpc", "getGenesisHash") in calls[-2:]
    rpc("POST", "https://h1/rpc", body=balance_body)
    assert calls[-1] == ("https://h1/rpc", "getBalance")
    assert rpc.snapshot()["active_index"] == 0
    assert rpc.snapshot()["exhausted_count"] == 0


def test_rotation_status_and_logs_never_contain_urls_or_keys(caplog):
    secret_url = "https://mainnet.helius-rpc.com/?api-key=SECRET"

    def http(_method, url, **_kwargs):
        if url == secret_url:
            return 200, {"error": {"code": -32429}}, None
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(http, (secret_url, "https://public/rpc"))
    with caplog.at_level(logging.WARNING):
        rpc("POST", secret_url, body="{}")

    rendered = repr(rpc.snapshot()) + caplog.text
    assert "SECRET" not in rendered
    assert "helius-rpc" not in rendered
    assert "https://" not in rendered


# --- HTTP 429 rotation, reads only (2026-08-25) ----------------------------------------
# The pool rotated ONLY on the -32429 body, which needs an HTTP 200. Real throttling arrives as
# an HTTP 429 that urllib RAISES, so it never reached the body check: the funded runner logged
# 1,812 HTTP 429s with active_index=0 exhausted_count=0. Rotation was unreachable, not mis-tuned.
#
# It must stay unreachable for WRITES. A timeout or 429 on sendTransaction is AMBIGUOUS — the
# bytes may be in flight — so re-sending to another endpoint can double-submit a funded trade.

def _read_body(method="getBalance"):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": ["P"]})


def _write_body():
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": ["AQ=="]})


class _Boom(Exception):
    def __init__(self, code):
        super().__init__("http %s" % code)
        self.code = code


def test_a_read_rotates_on_a_raised_429():
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        if url == "https://h1/rpc":
            raise _Boom(429)
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    assert rpc("POST", "https://h1/rpc", body=_read_body()) == (200, {"result": "ok"}, None)
    assert calls == ["https://h1/rpc", "https://h2/rpc"]
    assert rpc.snapshot()["last_reason"] == "http_429"


def test_a_read_rotates_on_a_returned_429_tuple():
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        return (429, {"error": "rate"}, None) if url == "https://h1/rpc" else (200, {"result": 1}, None)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    assert rpc("POST", "https://h1/rpc", body=_read_body()) == (200, {"result": 1}, None)
    assert rpc.snapshot()["degraded"] is True


def test_a_WRITE_never_rotates_on_429_even_though_a_read_would():
    """THE SAFETY PROPERTY. Re-sending a submit that may already be in flight can double-spend."""
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        raise _Boom(429)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    with pytest.raises(_Boom):
        rpc("POST", "https://h1/rpc", body=_write_body())
    assert calls == ["https://h1/rpc"], "a submit must never be replayed on another endpoint"
    assert rpc.snapshot()["active_index"] == 0


def test_a_WRITE_still_rotates_on_32429():
    """-32429 is safe for a write: the provider is stating it did not serve the request."""
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        if url == "https://h1/rpc":
            return 200, {"error": {"code": -32429}}, None
        return 200, {"result": "sig"}, None

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    assert rpc("POST", "https://h1/rpc", body=_write_body()) == (200, {"result": "sig"}, None)
    assert calls == ["https://h1/rpc", "https://h2/rpc"]


def test_an_unparseable_body_is_treated_as_a_write():
    """Fail SAFE: if we cannot prove it is an idempotent read, do not replay it."""
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        raise _Boom(429)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    with pytest.raises(_Boom):
        rpc("POST", "https://h1/rpc", body="not json")
    assert calls == ["https://h1/rpc"]


def test_a_batch_rotates_only_if_every_member_is_a_read():
    mixed = json.dumps([{"method": "getBalance"}, {"method": "sendTransaction"}])
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        raise _Boom(429)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    with pytest.raises(_Boom):
        rpc("POST", "https://h1/rpc", body=mixed)
    assert calls == ["https://h1/rpc"]


def test_a_read_does_not_rotate_on_a_client_error():
    """400 says the REQUEST is wrong. Rotating would repeat a bad request across every endpoint."""
    calls = []

    def http(_m, url, **_k):
        calls.append(url)
        raise _Boom(400)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    with pytest.raises(_Boom):
        rpc("POST", "https://h1/rpc", body=_read_body())
    assert calls == ["https://h1/rpc"]


def test_all_endpoints_down_raises_rather_than_returning_None():
    """A None return would read as a valid empty answer. The caller must see the failure."""
    def http(_m, _url, **_k):
        raise _Boom(429)

    rpc = RotatingSolanaRpcHttp(http, ("https://h1/rpc", "https://h2/rpc"))
    with pytest.raises(_Boom):
        rpc("POST", "https://h1/rpc", body=_read_body())


def test_snapshot_reports_healthy_when_on_the_primary():
    rpc = RotatingSolanaRpcHttp(lambda *_a, **_k: (200, {"result": 1}, None),
                                ("https://h1/rpc", "https://h2/rpc"))
    snap = rpc.snapshot()
    assert snap["degraded"] is False and snap["last_reason"] is None


def test_http_429_benches_briefly_but_credit_exhaustion_benches_for_an_hour():
    """A per-second rate limit is not a spent credit cap.

    Widening rotation to HTTP statuses (2026-08-25) reused the credit-exhaustion window, so one
    burst benched a PAID endpoint for a full hour. Live that evening: indexes 0 and 1 both out on
    http_429 with three endpoints left and no early recovery, because _refresh_one only probes
    after the window elapses.
    """
    now = [0.0]
    calls = []

    def http(_m, url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        calls.append((url, rpc_method))
        if url == "https://h1/rpc":
            raise _Boom(429)
        if rpc_method == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(
        http, ("https://h1/rpc", "https://h2/rpc"),
        retry_s=3600.0, transient_retry_s=60.0, clock=lambda: now[0])
    rpc("POST", "https://h1/rpc", body=_read_body())
    assert rpc.snapshot()["last_reason"] == "http_429"
    assert rpc.snapshot()["blocked_indexes"] == [0]

    now[0] = 61.0                      # short bench elapsed -> the probe reconsiders it
    calls.clear()
    rpc("POST", "https://h1/rpc", body=_read_body())
    rpc._probe_thread.join(3)          # the probe runs on its own thread since 2026-09-27
    assert any(m == "getGenesisHash" and u == "https://h1/rpc" for u, m in calls), \
        "a 429 must not bench a paid endpoint for an hour"


def test_credit_exhaustion_keeps_the_long_bench():
    now = [0.0]

    def http(_m, url, **kwargs):
        if url == "https://h1/rpc":
            return 200, {"error": {"code": -32429}}, None
        return 200, {"result": "ok"}, None

    rpc = RotatingSolanaRpcHttp(
        http, ("https://h1/rpc", "https://h2/rpc"),
        retry_s=3600.0, transient_retry_s=60.0, clock=lambda: now[0])
    rpc("POST", "https://h1/rpc", body=_read_body())
    assert rpc.snapshot()["last_reason"] == "credit_exhausted"

    now[0] = 61.0                      # the SHORT window must not free a credit-capped key
    rpc("POST", "https://h1/rpc", body=_read_body())
    assert rpc.snapshot()["blocked_indexes"] == [0]


def test_a_failed_probe_rebenches_on_THIS_endpoints_reason_not_the_pools_last():
    """`_last_reason` is the pool's most recent rotation, not this endpoint's cause.

    Bench index 0 on credit_exhausted (an hour) and index 1 on http_429 (a minute). A failed probe
    of index 0 must still get the hour: reading the pool-global `_last_reason` would hand it the
    minute that belongs to index 1, and an out-of-credit account would then be probed 60x more often
    than intended.
    """
    clock = {"t": 0.0}
    calls = []

    def http(method, url, **kw):
        calls.append(url)
        return 429, {"jsonrpc": "2.0", "error": {"code": 429, "message": "slow down"}, "id": 1}, None

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/", "https://c/"],
                                 retry_s=3600.0, transient_retry_s=60.0,
                                 clock=lambda: clock["t"])
    pool._advance(0, reason="credit_exhausted")
    pool._advance(1, reason="http_429")
    assert pool._blocked_until[0] == 3600.0
    assert pool._blocked_until[1] == 60.0

    clock["t"] = 3601.0                      # index 0's window elapsed; its probe will fail
    pool._refresh_one()
    assert pool._blocked_until[0] == 3601.0 + 3600.0, (
        "a failed probe of a credit-exhausted endpoint must keep the long bench")


def test_a_failed_probe_of_a_transient_endpoint_keeps_the_short_bench():
    clock = {"t": 0.0}

    def http(method, url, **kw):
        return 429, {"jsonrpc": "2.0", "error": {"code": 429, "message": "slow down"}, "id": 1}, None

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/", "https://c/"],
                                 retry_s=3600.0, transient_retry_s=60.0,
                                 clock=lambda: clock["t"])
    pool._advance(0, reason="http_429")
    clock["t"] = 61.0
    pool._refresh_one()
    assert pool._blocked_until[0] == 61.0 + 60.0


def test_a_restored_endpoint_forgets_its_bench_reason():
    clock = {"t": 0.0}
    state = {"ok": False}

    def http(method, url, **kw):
        if state["ok"]:
            return 200, {"jsonrpc": "2.0", "result": SOLANA_MAINNET_GENESIS, "id": 1}, None
        return 429, {"jsonrpc": "2.0", "error": {"code": 429, "message": "slow down"}, "id": 1}, None

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/", "https://c/"],
                                 retry_s=3600.0, transient_retry_s=60.0,
                                 clock=lambda: clock["t"])
    pool._advance(0, reason="credit_exhausted")
    state["ok"] = True
    clock["t"] = 3601.0
    pool._refresh_one()
    assert pool._blocked_until[0] is None
    assert pool._blocked_reason[0] is None


class _Spent(Exception):
    """urllib-shaped HTTPError: status on .code, body behind a one-shot .read()."""
    code = 429

    def __init__(self, body=b"max usage reached"):
        super().__init__("HTTP Error 429: Too Many Requests")
        self._body, self._read = body, False

    def read(self):
        if self._read:
            raise ValueError("body already consumed")
        self._read = True
        return self._body


def test_a_spent_plan_arrives_as_an_exception_and_takes_the_LONG_bench():
    """Measured 2026-08-25: Helius answers a spent plan with HTTP 429 and body 'max usage reached',
    to a SINGLE getHealth, repeatedly over 40s. urllib RAISES on 4xx, so the body only exists on the
    exception. Misread as a per-second limit it takes the 60s bench and gets probed ~1,440x/day."""
    clock = {"t": 0.0}

    def http(method, url, **kw):
        raise _Spent()

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/", "https://c/"],
                                 retry_s=3600.0, transient_retry_s=60.0, clock=lambda: clock["t"])
    try:
        pool("POST", "https://a/", body='{"method":"getHealth"}')
    except Exception:
        pass
    assert pool._blocked_reason[0] == "credit_exhausted", pool._blocked_reason
    assert pool._blocked_until[0] == 3600.0


def test_a_plain_rate_limit_still_takes_the_SHORT_bench():
    """A 429 whose body does NOT claim exhaustion keeps the transient window."""
    clock = {"t": 0.0}

    def http(method, url, **kw):
        raise _Spent(b"slow down, too many requests per second")

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/", "https://c/"],
                                 retry_s=3600.0, transient_retry_s=60.0, clock=lambda: clock["t"])
    try:
        pool("POST", "https://a/", body='{"method":"getHealth"}')
    except Exception:
        pass
    assert pool._blocked_reason[0] == "http_429", pool._blocked_reason
    assert pool._blocked_until[0] == 60.0


def test_the_body_is_read_once_and_survives_a_reraise():
    """The pool re-raises last_exc when every endpoint is out; a caller must still find the body."""
    exc = _Spent()

    def http(method, url, **kw):
        raise exc

    pool = RotatingSolanaRpcHttp(http, ["https://a/", "https://b/"],
                                 retry_s=3600.0, transient_retry_s=60.0, clock=lambda: 0.0)
    try:
        pool("POST", "https://a/", body='{"method":"getHealth"}')
    except Exception as raised:
        assert getattr(raised, "_pool_body_peek", None) == "max usage reached"
    else:
        raise AssertionError("expected the exhausted-pool re-raise")


def test_shyft_funded_pool_accepts_one_explicit_url_without_legacy_fallback():
    url = "https://rpc.shyft.to/?api_key=funded"
    assert solana_funded_rpc_urls({"MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft",
                                   "MEMECOIN_SOLANA_RPC_POOL_URLS": url}) == (url,)
