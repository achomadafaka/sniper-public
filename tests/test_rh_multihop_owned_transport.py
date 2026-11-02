"""Owned entry metadata shares the funded connection pool, never an injected seam."""
import time

import httpx
import pytest

from sniper.execution.fast_http import PooledHttpClient
from sniper.services.memecoin_evm_leg import _funded_evm_read_http


def test_owned_metadata_keeps_headers_deadline_and_client(monkeypatch):
    from sniper.execution import fast_http, submission_backends

    calls = []
    clients = []

    def respond(request):
        calls.append((request.method, request.url.path, request.extensions["timeout"]))
        return httpx.Response(429, json={"error": "limited"},
                              headers={"retry-after": "17", "location": "/next"})

    def factory(**kw):
        client = PooledHttpClient(transport=httpx.MockTransport(respond), **kw)
        clients.append(client)
        return client

    monkeypatch.setattr(fast_http, "PooledHttpClient", factory)
    monkeypatch.setattr(submission_backends, "register_client", lambda client: client)
    request = _funded_evm_read_http(None)
    try:
        with request.safety_deadline(time.monotonic() + 0.25):
            status, payload, headers = request.owned_metadata_headers(
                "GET", "https://provider.example/pools", timeout=12)
        assert status == 429 and payload == {"error": "limited"}
        assert headers.get("Retry-After") == "17"
        assert headers.get("Location") == "/next"
        request("POST", "https://rpc.example/", body="{}", timeout=1)
        assert len(clients) == 1
        assert len(calls) == 2
        assert all(0 < seconds <= 0.25 for seconds in calls[0][2].values())
    finally:
        for client in clients:
            client.close()


def test_owned_metadata_expired_deadline_sends_nothing(monkeypatch):
    from sniper.execution import fast_http, submission_backends
    from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded

    calls = []
    client = PooledHttpClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})))
    monkeypatch.setattr(fast_http, "PooledHttpClient", lambda **kw: client)
    monkeypatch.setattr(submission_backends, "register_client", lambda owned: owned)
    request = _funded_evm_read_http(None)
    try:
        with request.safety_deadline(time.monotonic() - 1):
            with pytest.raises(EvmSafetyDeadlineExceeded):
                request.owned_metadata_headers("GET", "https://provider.example/pools")
        assert calls == []
    finally:
        client.close()


def test_injected_http_never_claims_owned_metadata():
    request = _funded_evm_read_http(lambda *a, **kw: (200, {}, None))
    assert not hasattr(request, "owned_metadata_headers")


@pytest.mark.parametrize("status", [200, 429])
def test_owned_metadata_invalid_json_preserves_provider_breaker(monkeypatch, status):
    from sniper.execution import fast_http, submission_backends, solana_quote as ds
    from sniper.execution import evm_v4_multihop as mh, evm_v4_quote as q

    client = PooledHttpClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="<html>provider unavailable</html>",
                                       headers={"retry-after": "37"})))
    monkeypatch.setattr(fast_http, "PooledHttpClient", lambda **kw: client)
    monkeypatch.setattr(submission_backends, "register_client", lambda owned: owned)
    monkeypatch.setattr(ds, "_ds_funded_cooldown_until", [0.])
    monkeypatch.setattr(ds, "_ds_cooldown_until", [0.])
    request = _funded_evm_read_http(None)
    try:
        metadata = mh._metadata_http(request.owned_metadata_headers,
                                     mh._SearchBudget(1, 100), ds._UA)
        assert q._ds_pools_ranked("4663", "0x" + "ab" * 20,
                                  request_http=metadata) == ([], True)
        assert ds.dexscreener_cooldown_active(funded=True)
    finally:
        client.close()


@pytest.mark.parametrize("owned", [False, True])
def test_executor_quote_wrapper_preserves_owned_capability(monkeypatch, owned):
    from types import SimpleNamespace
    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg

    seen = []

    def http(*args, **kw):
        pytest.fail("executor construction must not read metadata")

    metadata = lambda *a, **kw: (200, {}, httpx.Headers())
    if owned:
        http.owned_metadata_headers = metadata

    def ladder(_primary, **kw):
        seen.append(kw["http"])
        return lambda *a, **kw: None

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_EXECUTION_OBSERVER_ENABLED", False)
    monkeypatch.setattr(seams, "venue_quote_with_v2_fallback", ladder)
    seams.build_real_evm_executor(
        acct=SimpleNamespace(address="0x" + "ab" * 20), chain_id="4663",
        rpc_url="https://rpc.example", http=http, price_fn=lambda: 2000.)
    assert len(seen) == 1
    assert getattr(seen[0], "owned_metadata_headers", None) is (metadata if owned else None)
