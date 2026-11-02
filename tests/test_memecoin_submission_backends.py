"""Task 1/4 — submission backend construction + lifecycle.

``auto_free`` (default) builds the shared same-bytes transport over the warm custody RPC plus the
chain-owner official endpoints (health-gated); ``pooled_rpc`` builds custody only; explicit ``rpc``
builds none. Official endpoints equal to custody (normalized) are de-duplicated; a startup wrong-chain
identity excludes an official before any send (its gated provider returns NOT_SENT). ``close_all``
closes every pooled client and stops warming. All offline (injected fake pooled client / http seam).
"""
import json

import pytest
from solders.keypair import Keypair

from sniper.execution import submission_backends as B
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, build_request,
)
from sniper.settings import memecoin_submission as C


def _signed_raw(chain_id, nonce=1):
    """A real signed transaction: the EVM-08 intent fence never sends bytes without a chain id."""
    from eth_account import Account
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()


class FakeClient:
    def __init__(self, *a, **k):
        self.closed = False

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        rpc_method = json.loads(body or "{}").get("method")
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        if rpc_method == "getGenesisHash":
            from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        if rpc_method == "eth_chainId":
            return 200, {"result": "0x2105"}, None
        return 200, {}, None

    def close(self):
        self.closed = True


def _evm_req(raw=None, identity="0xhash1"):
    return build_request(chain_id="8453", action="entry", tx_identity=identity,
                         signed_payload=_signed_raw(8453) if raw is None else raw)


@pytest.fixture(autouse=True)
def _clean_registry():
    B.close_all()
    yield
    B.close_all()


class FakeSidecar:
    """A stand-in TpuSidecarProcess: reports readiness and hands out a solana:tpu provider."""

    def __init__(self, *, ready=True):
        self._ready = ready
        self.closed = 0

    def health(self):
        return {"ready": self._ready, "leader_count": 5}

    def provider(self, health=None):
        ready = self._ready

        class _Prov:
            provider_id = "solana:tpu"

            def submit(self, request):
                from sniper.execution.submission_transport import AttemptState as _S, SubmissionAttempt
                if not ready:
                    return SubmissionAttempt(
                        "solana:tpu", _S.NOT_SENT, None, 0.0, None, "sidecar_unavailable")
                return SubmissionAttempt(
                    "solana:tpu", _S.ACCEPTED, request.tx_identity, 1.0, "5", None)
        return _Prov()

    def close(self):
        self.closed += 1


def test_auto_free_builds_custody_plus_public_solana_transport():
    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc", env={},
        client_factory=FakeClient)
    assert isinstance(transport, SubmissionTransport)
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc", "solana:public_rpc"]
    assert transport._providers[0]._max_retries == 3
    assert builder is None


def test_auto_free_adds_healthy_tpu_sidecar():
    sc = FakeSidecar(ready=True)
    transport, _ = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc", env={},
        client_factory=FakeClient, tpu_sidecar=sc)
    labels = [p.provider_id for p in transport._providers]
    assert labels == ["solana:custody_rpc", "solana:public_rpc", "solana:tpu"]
    assert sc in B.submission_sidecars()             # owned -> closed on shutdown


def test_auto_free_keeps_supervised_tpu_provider_while_initially_unhealthy():
    sc = FakeSidecar(ready=False)
    transport, _ = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc", env={},
        client_factory=FakeClient, tpu_sidecar=sc)
    labels = [p.provider_id for p in transport._providers]
    assert labels == ["solana:custody_rpc", "solana:public_rpc", "solana:tpu"]
    attempt = transport._providers[-1].submit(
        build_request(chain_id="solana", action="entry", tx_identity="sig",
                      signed_payload="c2lnbmVk"))
    assert attempt.state is AttemptState.NOT_SENT
    assert attempt.reason_code == "sidecar_unavailable"
    assert sc in B.submission_sidecars()


def test_pooled_rpc_builds_custody_only_solana_transport():
    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc"}, client_factory=FakeClient)
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc"]
    assert builder is None


def test_default_solana_client_outlives_the_configured_warm_interval(monkeypatch):
    from sniper.execution import fast_http
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(fast_http, "PooledHttpClient", client)
    monkeypatch.setattr(B, "_start_warming", lambda *_a: None)
    B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc", "MEMECOIN_SOLANA_WARM_INTERVAL_S": "60"})
    assert options["keepalive_expiry_s"] >= 120.0


@pytest.mark.parametrize("interval,timeout_ms", [(5, 60_000), (60, 1_500)])
def test_idle_expiry_covers_serial_warm_calls_as_well_as_sleep(monkeypatch, interval, timeout_ms):
    from sniper.execution import fast_http
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(fast_http, "PooledHttpClient", client)
    monkeypatch.setattr(B, "_start_warming", lambda *_a: None)
    B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc",
             "MEMECOIN_SOLANA_WARM_INTERVAL_S": str(interval),
             "MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS": str(timeout_ms)})
    # Custody + Sender + Nozomi, each with pool/connect/write/read timeout phases.
    assert options["keepalive_expiry_s"] >= 2 * interval + 3 * 4 * max(3, timeout_ms / 1000)


def test_custom_solana_factory_keeps_its_existing_keyword_contract(monkeypatch):
    seen = []

    def factory(*, read_timeout_s):
        seen.append(read_timeout_s)
        return FakeClient()

    monkeypatch.setattr(B, "_start_warming", lambda *_a: None)
    B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc", "MEMECOIN_SOLANA_WARM_INTERVAL_S": "60"},
        client_factory=factory)
    assert seen == [1.5]


def test_legacy_rpc_mode_builds_no_solana_backend():
    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "rpc"}, client_factory=FakeClient)
    assert transport is None and builder is None


def test_injected_http_validates_custody_without_constructing_a_real_client():
    calls = []

    def http(method, url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        calls.append(rpc_method)
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
        return 200, {"result": SOLANA_MAINNET_GENESIS}, None

    def forbidden_factory(**kwargs):
        raise AssertionError("client factory must not run with injected HTTP")

    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc"},
        client_factory=forbidden_factory, http=http)
    assert isinstance(transport, SubmissionTransport) and builder is None
    assert calls == ["getHealth", "getGenesisHash"]


def test_managed_external_http_keeps_custody_connection_warming(monkeypatch):
    started = []
    client = FakeClient()
    monkeypatch.setattr(
        B, "_start_warming",
        lambda specs, interval_s: started.append((tuple(label for label, _fn in specs), interval_s)),
    )

    B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc"},
        http=client.request, warm_external_http=True,
    )

    assert started == [(('solana:custody_rpc',), 5.0)]


def test_registered_external_client_is_closed_with_backend_lifecycle():
    client = FakeClient()

    B.register_client(client)
    B.close_all()

    assert client.closed is True


def test_solana_custody_identity_failure_blocks_backend_construction():
    def http(method, url, **kwargs):
        rpc_method = json.loads(kwargs["body"])["method"]
        if rpc_method == "getHealth":
            return 200, {"result": "ok"}, None
        return 200, {"result": "wrong-genesis"}, None

    with pytest.raises(C.SubmissionConfigError, match="custody Solana identity"):
        B.build_solana_backend(
            keypair=Keypair(), custody_rpc_url="https://custody/rpc", env={}, http=http)


def test_evm_custody_identity_failure_blocks_backend_construction():
    def http(method, url, **kwargs):
        return 200, {"result": "0xdead"}, None

    with pytest.raises(C.SubmissionConfigError, match="custody EVM identity"):
        B.build_evm_backend("8453", custody_rpc_url="https://custody/rpc", env={}, http=http)


def test_auto_free_base_builds_custody_plus_standard_and_flashblocks():
    transport = B.build_evm_backend(
        "8453", custody_rpc_url="https://custody/rpc", env={}, client_factory=FakeClient)
    assert isinstance(transport, SubmissionTransport)
    assert [p.provider_id for p in transport._providers] == [
        "8453:custody_rpc", "8453:base_standard", "8453:base_flashblocks"]


def test_auto_free_robinhood_builds_custody_public_and_sequencer():
    def http(method, url, *, headers=None, body=None, timeout=None):
        if json.loads(body).get("method") == "eth_chainId":
            return 200, {"result": "0x1237"}, None
        return 200, {}, None

    transport = B.build_evm_backend(
        "4663", custody_rpc_url="https://custody/rpc", env={}, http=http)
    assert [p.provider_id for p in transport._providers] == [
        "4663:custody_rpc", "4663:public_rpc", "4663:sequencer"]


def test_robinhood_warms_submission_only_sequencer_on_its_sending_client(monkeypatch):
    started = []
    clients = []
    calls = []

    class Client(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            clients.append(self)

        def request(self, method, url, *, headers=None, body=None, timeout=None):
            rpc = json.loads(body)
            calls.append((self, method, url, rpc))
            if rpc["method"] == "eth_chainId":
                if "sequencer" in url:
                    return 200, {"error": {"code": -32601, "message": "Method not found"}}, None
                return 200, {"result": "0x1237"}, None
            assert rpc["method"] == "eth_sendRawTransaction"
            return 200, {"result": "0xhash1"}, None

    monkeypatch.setattr(B, "_start_health_checks", lambda *_a: None)
    monkeypatch.setattr(B, "_start_warming", lambda specs, interval: started.append((specs, interval)))
    transport = B.build_evm_backend(
        "4663", custody_rpc_url="https://custody/rpc", env={}, client_factory=Client)

    assert len(clients) == 1
    assert len(started) == 1
    warm_specs, interval = started[0]
    assert interval == 5.0
    assert [label for label, _fn in warm_specs] == ["4663:custody_rpc", "4663:sequencer"]
    from sniper.execution.fast_http import warm_once
    assert warm_once(warm_specs)["4663:sequencer"] is True
    assert "4663:sequencer" not in B.submission_health_snapshots()

    sequencer = transport._providers[-1]
    result = sequencer.submit(build_request(
        chain_id="4663", action="entry", tx_identity="0xhash1", signed_payload=_signed_raw(4663)))
    assert result.state is AttemptState.ACCEPTED
    seq_calls = [(client, method, rpc) for client, method, url, rpc in calls if "sequencer" in url]
    assert [(method, rpc["method"]) for _client, method, rpc in seq_calls] == [
        ("POST", "eth_chainId"), ("POST", "eth_sendRawTransaction")]
    assert all(client is clients[0] for client, _method, _rpc in seq_calls)


@pytest.mark.parametrize("chain,mode", [("4663", "pooled_rpc"), ("8453", "auto_free"),
                                        ("56", "auto_free")])
def test_other_evm_policies_keep_only_existing_custody_warming(monkeypatch, chain, mode):
    started = []

    class Client(FakeClient):
        def request(self, method, url, *, headers=None, body=None, timeout=None):
            return 200, {"result": hex(int(chain))}, None

    monkeypatch.setattr(B, "_start_health_checks", lambda *_a: None)
    monkeypatch.setattr(B, "_start_warming", lambda specs, interval: started.append((specs, interval)))
    transport = B.build_evm_backend(
        chain, custody_rpc_url="https://custody/rpc", env={"MEMECOIN_EVM_SUBMIT_MODE": mode},
        client_factory=Client)

    assert [(tuple(label for label, _fn in specs), interval) for specs, interval in started] == [
        ((chain + ":custody_rpc",), 5.0)]
    if mode == "pooled_rpc":
        assert [provider.provider_id for provider in transport._providers] == [chain + ":custody_rpc"]


def test_injected_robinhood_http_does_not_start_connection_warming(monkeypatch):
    def http(method, url, *, headers=None, body=None, timeout=None):
        return 200, {"result": "0x1237"}, None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("injected HTTP must not start warming or construct a client")

    monkeypatch.setattr(B, "_start_health_checks", lambda *_a: None)
    monkeypatch.setattr(B, "_start_warming", forbidden)
    transport = B.build_evm_backend(
        "4663", custody_rpc_url="https://custody/rpc", env={}, http=http, client_factory=forbidden)
    assert [provider.provider_id for provider in transport._providers] == [
        "4663:custody_rpc", "4663:public_rpc", "4663:sequencer"]


def test_robinhood_submission_only_sequencer_is_not_blocked_as_unverified():
    sends = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        rpc = json.loads(body)
        if rpc.get("method") == "eth_chainId":
            return 200, {"result": "0x1237"}, None
        sends.append(url)
        if "sequencer" in url:
            return 200, {"result": "0xhash1"}, None
        return 200, {"error": {"message": "invalid sender"}}, None

    transport = B.build_evm_backend(
        "4663", custody_rpc_url="https://custody/rpc", env={}, http=http)
    request = build_request(
        chain_id="4663", action="entry", tx_identity="0xhash1", signed_payload=_signed_raw(4663))
    result = transport.submit(request)
    assert result.state is AttemptState.ACCEPTED
    assert any("sequencer.mainnet.chain.robinhood.com" in url for url in sends)


def test_pooled_rpc_evm_builds_custody_only():
    transport = B.build_evm_backend(
        "8453", custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_EVM_SUBMIT_MODE": "pooled_rpc"}, client_factory=FakeClient)
    assert [p.provider_id for p in transport._providers] == ["8453:custody_rpc"]


def test_official_endpoint_equal_to_custody_is_deduped():
    # custody IS the base standard endpoint (trailing slash + case) -> base_standard dropped
    transport = B.build_evm_backend(
        "8453", custody_rpc_url="https://Mainnet.Base.org/", env={}, client_factory=FakeClient)
    labels = [p.provider_id for p in transport._providers]
    assert labels == ["8453:custody_rpc", "8453:base_flashblocks"]


def test_startup_wrong_chain_official_is_excluded_and_never_sent(monkeypatch):
    # base_standard reports the wrong chain id at startup -> hard-excluded; its gated provider then
    # returns NOT_SENT (zero eth_sendRawTransaction to it) while custody + flashblocks still send.
    sends = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        meth = json.loads(body).get("method")
        if meth == "eth_chainId":
            wrong = ("mainnet.base.org" in url and "preconf" not in url)   # only base_standard
            return (200, {"result": "0xbad"}, None) if wrong else (200, {"result": "0x2105"}, None)
        if meth == "eth_sendRawTransaction":
            sends.append(url)
            return 200, {"result": "0xhash1"}, None
        return 200, {"result": None}, None

    transport = B.build_evm_backend("8453", custody_rpc_url="https://custody/rpc", env={}, http=http)
    res = transport.submit(_evm_req())
    # base_standard was excluded before send -> its attempt is NOT_SENT and no send hit its URL
    states = {a.provider_id: a.state for a in res.attempts}
    assert states["8453:base_standard"] is AttemptState.NOT_SENT
    assert not any("mainnet.base.org" in u and "preconf" not in u for u in sends)
    snaps = B.submission_health_snapshots()
    assert snaps["8453:base_standard"]["state"] == "excluded"
    assert snaps["8453:base_flashblocks"]["state"] == "active"


def test_health_gated_provider_returns_not_sent_when_excluded():
    from sniper.execution.submission_health import HealthGatedProvider, HealthRegistry

    class Inner:
        provider_id = "8453:base_standard"

        def __init__(self):
            self.calls = 0

        def submit(self, request):
            self.calls += 1
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                     request.tx_identity, 1.0, "200", None)

    health = HealthRegistry(["8453:base_standard"])
    health.record_success("8453:base_standard", "0x2105")
    inner = Inner()
    gated = HealthGatedProvider(inner, health)
    assert gated.submit(_evm_req()).state is AttemptState.ACCEPTED and inner.calls == 1
    health.record_wrong_chain("8453:base_standard", "0xbad")
    gated_attempt = gated.submit(_evm_req())
    assert gated_attempt.state is AttemptState.NOT_SENT
    assert inner.calls == 1                               # excluded -> inner never called again


def test_blocked_official_cannot_delay_a_fast_custody_accept():
    import threading
    import time as _t
    block = threading.Event()

    def http(method, url, *, headers=None, body=None, timeout=None):
        meth = json.loads(body).get("method")
        if meth == "eth_chainId":
            return 200, {"result": "0x2105"}, None
        if meth == "eth_sendRawTransaction":
            if "custody" in url:
                return 200, {"result": "0xhash1"}, None      # custody accepts immediately
            block.wait(2.0)                                  # an official hangs
            return 200, {"result": "0xhash1"}, None
        return 200, {"result": None}, None

    transport = B.build_evm_backend(
        "8453", custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_EVM_SUBMIT_DEADLINE_MS": "800"}, http=http)
    try:
        started = _t.monotonic()
        res = transport.submit(_evm_req())
        elapsed = _t.monotonic() - started
        assert res.state is AttemptState.ACCEPTED         # custody carried it
        assert elapsed < 0.8                              # never waited for the hung official
    finally:
        block.set()
        transport.close()


def test_legacy_evm_rpc_mode_builds_no_transport():
    assert B.build_evm_backend(
        "8453", custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_EVM_SUBMIT_MODE": "rpc"}, client_factory=FakeClient) is None


def test_evm_factory_revalidates_mode_defensively():
    with pytest.raises(C.SubmissionConfigError, match="MEMECOIN_EVM_SUBMIT_MODE"):
        B.build_evm_backend(
            "8453", custody_rpc_url="https://custody/rpc",
            env={"MEMECOIN_EVM_SUBMIT_MODE": "bogus"}, client_factory=FakeClient)


def test_evm_requires_custody_url():
    with pytest.raises(C.SubmissionConfigError, match="custody"):
        B.build_evm_backend("8453", custody_rpc_url=None, env={}, client_factory=FakeClient)


def test_close_all_closes_clients():
    clients = []

    def factory(*a, **k):
        c = FakeClient()
        clients.append(c)
        return c
    B.build_evm_backend("8453", custody_rpc_url="https://custody/rpc", env={},
                        client_factory=factory)
    assert clients and not clients[0].closed
    B.close_all()
    assert clients[0].closed


def test_close_all_closes_registered_sidecar():
    class FakeSidecar:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    sc = FakeSidecar()
    B._register_sidecar(sc)
    assert sc in B.submission_sidecars()
    B.close_all()
    assert sc.closed == 1                             # owned sidecar closed exactly once on shutdown
    assert B.submission_sidecars() == []             # registry cleared


def test_slow_but_correct_custody_endpoint_still_starts_the_backend():
    """A SLOW custody RPC is not a WRONG one. Chain 4663 answers eth_chainId correctly in
    1.9-3.7 s (8/8 samples over 1 s, 2026-08-25). The startup probe borrowed the 1000 ms submit
    budget, failed 3/3 on transport, and the funded runner refused to start."""
    def http(method, url, *, headers=None, body=None, timeout=None):
        if json.loads(body).get("method") == "eth_chainId":
            if (timeout or 0) < 2.0:                  # the endpoint needs ~2.6 s to answer
                raise TimeoutError("read timeout")
            return 200, {"result": "0x1237"}, None
        return 200, {}, None

    transport = B.build_evm_backend(
        "4663", custody_rpc_url="https://custody/rpc",
        env={"MEMECOIN_EVM_SUBMIT_TIMEOUT_MS": "1000"}, http=http)

    assert "4663:custody_rpc" in [p.provider_id for p in transport._providers]
