"""Shyft startup topology and same-bytes WRITE ambiguity, with no live network."""
import json
import threading
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from sniper.execution import submission_backends as B
from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, build_request,
)
from sniper.settings import memecoin_submission as C


AMS = "https://rpc.ams.shyft.to/?api_key=funded-test"
FRA = "https://rpc.fra.shyft.to/?api_key=alternate-test"
ENV = {"MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft",
       "MEMECOIN_SOLANA_RPC_POOL_URLS": AMS}


class Sidecar:
    def __init__(self):
        self.payloads = []
        self.dispatched = threading.Event()

    def provider(self, health=None):
        sidecar = self

        class Provider:
            provider_id = "solana:tpu"

            def submit(self, request):
                sidecar.payloads.append(request.signed_payload)
                sidecar.dispatched.set()
                return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                         request.tx_identity, 0.0, None, None)

        return Provider()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_backends():
    B.close_all()
    yield
    B.close_all()


def test_shyft_auto_free_never_constructs_probes_or_warms_public_providers(monkeypatch):
    from sniper.execution import free_endpoint_registry
    calls, warm = [], []
    sidecar = Sidecar()

    def http(_method, url, **kwargs):
        body = json.loads(kwargs["body"])
        calls.append((url, body))
        if body["method"] == "sendTransaction":
            assert sidecar.dispatched.wait(1.0)
        return 200, {"result": {"getHealth": "ok", "getGenesisHash": SOLANA_MAINNET_GENESIS,
                               "sendTransaction": "our-sig"}[body["method"]]}, None

    monkeypatch.setattr(free_endpoint_registry, "free_endpoints",
                        lambda *_a: pytest.fail("Shyft constructed public providers"))
    monkeypatch.setattr(B, "_start_warming", lambda specs, _interval: warm.extend(specs))
    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url=AMS, env=ENV, http=http,
        tpu_sidecar=sidecar, warm_external_http=True)
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc", "solana:tpu"]
    assert builder is None
    for _label, keepalive in warm:
        keepalive()
    request = build_request(chain_id="solana", action="entry", tx_identity="our-sig",
                            signed_payload="c2lnbmVk")
    assert transport.submit(request).state is AttemptState.ACCEPTED
    assert {url for url, _body in calls} == {AMS}
    assert [body["params"][0] for _, body in calls if body["method"] == "sendTransaction"] == ["c2lnbmVk"]
    assert sidecar.dispatched.wait(1.0)
    assert sidecar.payloads == ["c2lnbmVk"]
    assert [label for label, _fn in warm] == ["solana:custody_rpc"]


@pytest.mark.parametrize("mode", ["auto_free", "pooled_rpc", "rpc"])
@pytest.mark.parametrize("custody", ["https://api.mainnet-beta.solana.com",
                                      "https://mainnet.helius-rpc.com/?api-key=secret",
                                      FRA])
def test_shyft_refuses_unconfigured_custody_before_constructing_client(mode, custody):
    def factory(**_kwargs):
        pytest.fail("invalid custody constructed a client")

    with pytest.raises(C.SubmissionConfigError, match="custody") as error:
        B.build_solana_backend(keypair=Keypair(), custody_rpc_url=custody,
                               env={**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": mode},
                               client_factory=factory)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("flag,value", [
    ("MEMECOIN_SLOT_LANE_SENDER_AB", "1"),
    ("MEMECOIN_SLOT_LANE_SENDER_ALL", "ab"),
    ("MEMECOIN_SLOT_LANE_SENDER_ALL", "all"),
])
def test_shyft_refuses_active_sender_flags_at_startup(flag, value):
    # Only with MEMECOIN_SOLANA_SENDER_WITH_SHYFT off (2026-10-15: Sender may join the Shyft graph).
    with pytest.raises(C.SubmissionConfigError, match=flag):
        C.validate_submission_config({**ENV, flag: value, "MEMECOIN_SOLANA_SENDER_WITH_SHYFT": "0"})


def test_shyft_disabled_auxiliary_flags_and_pooled_rollback_use_only_custody():
    env = {**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc",
           "MEMECOIN_SLOT_LANE_SENDER_AB": "0", "MEMECOIN_SLOT_LANE_NOZOMI_AB": "off",
           "MEMECOIN_SLOT_LANE_SENDER_ALL": "off", "MEMECOIN_NOZOMI_ALL_LANES": "0",
           "MEMECOIN_NOZOMI_ALL_B": "false"}

    def http(_method, _url, **kwargs):
        method = json.loads(kwargs["body"])["method"]
        return 200, {"result": "ok" if method == "getHealth" else SOLANA_MAINNET_GENESIS}, None

    C.validate_submission_config(env)
    transport, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS, env=env,
                                         http=http, tpu_sidecar=Sidecar())
    assert [p.provider_id for p in transport._providers] == ["solana:custody_rpc"]


@pytest.mark.parametrize("mode", ["auto_free", "pooled_rpc"])
def test_shyft_explicit_frankfurt_nozomi_parallel_same_bytes_and_warming(monkeypatch, caplog, mode):
    from sniper.execution import free_endpoint_registry, solana_sender_ab as S
    from test_memecoin_slot_lane_sender_ab import _request, _signed

    key = "frankfurt-test-secret"
    env = {**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": mode,
           "MEMECOIN_SLOT_LANE_NOZOMI_AB": "true", "MEMECOIN_NOZOMI_API_KEY": key,
           "MEMECOIN_NOZOMI_ALL_LANES": "on", "MEMECOIN_NOZOMI_ALL_B": "yes"}
    nozomi_started, custody_started, release_nozomi = (threading.Event() for _ in range(3))
    calls, warm = [], []
    payer = Keypair()
    request = _request(_signed(payer, S.tip_instructions(
        payer.pubkey(), (S.NOZOMI_TIP_ACCOUNTS[0], S.NOZOMI_TIP_LAMPORTS))))

    def http(method, url, **kwargs):
        if method == "GET":
            calls.append((url, None))
            return 200, "ok", None
        body = json.loads(kwargs["body"])
        calls.append((url, body))
        if body["method"] == "sendTransaction":
            if url == S.NOZOMI_URL + key:
                nozomi_started.set()
                assert custody_started.wait(1.0)
                assert release_nozomi.wait(1.0)
            else:
                custody_started.set()
                assert nozomi_started.wait(1.0)
            return 200, {"result": request.tx_identity}, None
        return 200, {"result": "ok" if body["method"] == "getHealth"
                     else SOLANA_MAINNET_GENESIS}, None

    monkeypatch.setattr(free_endpoint_registry, "free_endpoints",
                        lambda *_a: pytest.fail("Shyft constructed public providers"))
    monkeypatch.setattr(S, "SenderProvider", lambda *_a, **_kw: pytest.fail("constructed Sender"))
    monkeypatch.setattr(B, "_start_warming", lambda specs, _interval: warm.extend(specs))
    sidecar = Sidecar()
    transport, builder = B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url=AMS, env=env, http=http,
        tpu_sidecar=sidecar, warm_external_http=True)
    labels = ["solana:custody_rpc"] + (["solana:tpu"] if mode == "auto_free" else [])
    assert [p.provider_id for p in transport._providers] == labels + ["solana:nozomi"]
    assert builder is None and transport.provider("solana:nozomi").send_after_accept is True
    try:
        result = transport.submit(request)
        assert result.state is AttemptState.ACCEPTED
        assert nozomi_started.wait(1.0)
    finally:
        release_nozomi.set()
    for future in result.late_results:
        future.result(timeout=1.0)
    posts = [(url, body["params"][0]) for url, body in calls
             if body and body["method"] == "sendTransaction"]
    assert set(posts) == {(AMS, request.signed_payload), (S.NOZOMI_URL + key, request.signed_payload)}
    assert sidecar.payloads == ([request.signed_payload] if mode == "auto_free" else [])
    assert [label for label, _fn in warm] == ["solana:custody_rpc", "solana:nozomi"]
    for _label, keepalive in warm:
        keepalive()
    assert (S.NOZOMI_PING_URL, None) in calls
    assert key not in caplog.text and key not in repr(transport.provider("solana:nozomi"))


@pytest.mark.parametrize("key", [None, "", "  "])
def test_shyft_nozomi_missing_key_fails_before_client_construction(key):
    env = {**ENV, "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1", "MEMECOIN_NOZOMI_API_KEY": key}
    with pytest.raises(C.SubmissionConfigError, match="MEMECOIN_NOZOMI_API_KEY"):
        B.build_solana_backend(
            keypair=Keypair(), custody_rpc_url=AMS, env=env,
            client_factory=lambda **_kw: pytest.fail("missing key constructed client"))


def test_shyft_nozomi_rpc_rollback_requires_disabling_opt_in():
    env = {**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": "rpc",
           "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1", "MEMECOIN_NOZOMI_API_KEY": "test-key"}
    with pytest.raises(C.SubmissionConfigError, match="shared transport"):
        C.validate_submission_config(env)
    C.validate_submission_config({**env, "MEMECOIN_SLOT_LANE_NOZOMI_AB": "0"})


@pytest.mark.parametrize("action", ["entry", "exit"])
def test_shyft_nozomi_untipped_sends_are_neutral(monkeypatch, action):
    from sniper.execution import solana_sender_ab as S
    from test_memecoin_slot_lane_sender_ab import _request, _signed
    from dataclasses import replace

    calls = []
    request = replace(_request(_signed(Keypair(), [])), action=action)

    def http(_method, url, **kwargs):
        method = json.loads(kwargs["body"])["method"]
        calls.append(url)
        return 200, {"result": {"getHealth": "ok", "getGenesisHash": SOLANA_MAINNET_GENESIS,
                               "sendTransaction": request.tx_identity}[method]}, None

    env = {**ENV, "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1", "MEMECOIN_NOZOMI_API_KEY": "test-key"}
    transport, _ = B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS, env=env, http=http)
    attempt = transport.provider(S.NOZOMI_PROVIDER_ID).submit(request)
    assert attempt.state is AttemptState.NOT_SENT and attempt.reason_code == "untipped"
    result = transport.submit(request)
    for future in result.late_results:
        future.result(timeout=1.0)
    assert result.state is AttemptState.ACCEPTED and set(calls) == {AMS}


def test_shyft_nozomi_warm_budget_covers_custody_and_nozomi(monkeypatch):
    from sniper.execution import fast_http
    from test_memecoin_submission_backends import FakeClient

    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(fast_http, "PooledHttpClient", client)
    monkeypatch.setattr(B, "_start_warming", lambda *_a: None)
    B.build_solana_backend(keypair=Keypair(), custody_rpc_url=AMS,
                          env={**ENV, "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1",
                               "MEMECOIN_NOZOMI_API_KEY": "test-key",
                               "MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS": "60000"})
    assert options["keepalive_expiry_s"] >= 2 * 5 + 2 * 4 * 60


def test_shyft_profile_does_not_change_evm_public_topology():
    def http(_method, _url, **_kwargs):
        return 200, {"result": "0x2105"}, None

    transport = B.build_evm_backend("8453", custody_rpc_url="https://evm-custody", env=ENV, http=http)
    assert [p.provider_id for p in transport._providers] == [
        "8453:custody_rpc", "8453:base_standard", "8453:base_flashblocks"]


@pytest.mark.parametrize("response", [
    (429, "max usage reached", None),
    (429, {"error": "rate limit"}, None),
    (200, {"error": {"code": -32005, "message": "credit limit"}}, None),
    (200, {}, None),
    (200, "malformed", None),
    TimeoutError("write may have happened"),
])
def test_shyft_ambiguous_write_never_rotates_and_preserves_signed_bytes(response):
    from sniper.execution.solana_fast_submit import SolanaRpcProvider
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append((url, json.loads(body)))
        if isinstance(response, Exception):
            raise response
        return response

    pool = RotatingSolanaRpcHttp(http, (AMS, FRA))
    provider = SolanaRpcProvider(pool, AMS)
    request = build_request(chain_id="solana", action="entry", tx_identity="our-sig",
                            signed_payload="c2lnbmVk")
    assert provider.submit(request).state is AttemptState.AMBIGUOUS
    assert calls == [(AMS, {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                            "params": ["c2lnbmVk", {"encoding": "base64", "skipPreflight": False,
                                                       "maxRetries": 0}]})]
    assert pool.snapshot()["active_index"] == 0


@pytest.mark.parametrize("error", [
    {"error": {"code": -32429}},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32429}},
])
def test_structured_32429_is_the_only_write_rotation_and_keeps_bytes(error):
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append((url, body))
        return ((200, error, None) if url == AMS else
                (200, {"result": "our-sig"}, None))

    pool = RotatingSolanaRpcHttp(http, (AMS, FRA))
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                       "params": ["c2lnbmVk"]})
    assert pool("POST", AMS, body=body) == (200, {"result": "our-sig"}, None)
    assert calls == [(AMS, body), (FRA, body)]


@pytest.mark.parametrize("profile", ["legacy", "shyft"])
@pytest.mark.parametrize("payload", [
    {"result": "our-sig", "error": {"code": -32429}},
    {"result": None, "error": {"code": -32429}},
    {"error": {"code": -32429.0}},
    {"error": {"code": "-32429"}},
    {"error": {"code": True}},
    {"jsonrpc": "1.0", "error": {"code": -32429}},
    {"jsonrpc": 2.0, "error": {"code": -32429}},
    {"id": 2, "error": {"code": -32429}},
    {"id": True, "error": {"code": -32429}},
    {"id": "1", "error": {"code": -32429}},
])
def test_malformed_credit_rejection_never_rotates_a_write(profile, payload):
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append((url, body))
        return 200, payload, None

    pool = RotatingSolanaRpcHttp(http, (AMS, FRA),
                                env={"MEMECOIN_SOLANA_INFRA_PROVIDER": profile})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                       "params": ["c2lnbmVk"]})
    assert pool("POST", AMS, body=body) == (200, payload, None)
    assert calls == [(AMS, body)]
    assert pool.snapshot()["active_index"] == 0


@pytest.mark.parametrize("payload", [
    {"result": "our-sig", "error": {"code": -32429}},
    {"result": "our-sig", "error": None},
    {"result": None, "error": {"message": "signature verification failure"}},
])
def test_contradictory_result_and_error_cannot_accept_or_reject_a_signed_send(payload):
    from sniper.execution.solana_fast_submit import SolanaRpcProvider
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append(url)
        return 200, payload, None

    pool = RotatingSolanaRpcHttp(http, (AMS, FRA), env=ENV)
    request = build_request(chain_id="solana", action="entry", tx_identity="our-sig",
                            signed_payload="c2lnbmVk")
    attempt = SolanaRpcProvider(pool, AMS).submit(request)
    assert attempt.state is AttemptState.AMBIGUOUS
    assert calls == [AMS]


def test_shyft_generic_read_limit_is_not_helius_credit_exhaustion(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOLANA_INFRA_PROVIDER", "shyft")
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append(url)
        return ((429, "max usage reached", None) if url == AMS else (200, {"result": 1}, None))

    pool = RotatingSolanaRpcHttp(http, (AMS, FRA))
    assert pool("POST", AMS, body=json.dumps({"method": "getSlot"})) == (200, {"result": 1}, None)
    assert calls == [AMS, FRA]
    assert pool.snapshot()["last_reason"] == "http_429"


def _armed_context(monkeypatch, tmp_path, *, mode="auto_free"):
    from sniper.execution.solana_wallet import generate_keypair_file
    from sniper.settings import memecoin as cfg
    keyfile = str(tmp_path / "key.json")
    generate_keypair_file(keyfile)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", keyfile)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_POOL_URLS", (AMS,))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_INFRA_PROVIDER", "shyft")
    for key, value in {**ENV, "MEMECOIN_SOLANA_SUBMIT_MODE": mode,
                       "MEMECOIN_SOLANA_READ_RPC_URL": FRA,
                       "MEMECOIN_TPU_RPC_URL": FRA}.items():
        monkeypatch.setenv(key, value)


def test_context_selects_explicit_shyft_tpu_and_read_once_at_startup(monkeypatch, tmp_path):
    from sniper.services import memecoin_real_runner as R
    from sniper.execution import solana_tpu_sidecar
    _armed_context(monkeypatch, tmp_path)
    resolved = []
    monkeypatch.setattr(solana_tpu_sidecar, "resolve_tpu_sidecar",
                        lambda **kw: resolved.append(kw["rpc_url"]) or
                        SimpleNamespace(process=None, reason="test"))
    monkeypatch.setattr(R.cfg, "MEMECOIN_TPU_RPC_URL", "https://public.invalid")

    def http(_method, url, **kwargs):
        assert url == AMS
        method = json.loads(kwargs["body"])["method"]
        return 200, {"result": "ok" if method == "getHealth" else SOLANA_MAINNET_GENESIS}, None

    context = R.build_real_context(http=http)
    assert resolved == [FRA]
    assert context["read_rpc_url"] == FRA
    assert context["rpc_pool_status_fn"]()["fallback_active"] is False
    monkeypatch.setenv("MEMECOIN_SOLANA_READ_RPC_URL", "https://public.invalid")
    assert context["read_rpc_url"] == FRA


@pytest.mark.parametrize("role", ["MEMECOIN_TPU_RPC_URL", "MEMECOIN_SOLANA_READ_RPC_URL"])
def test_context_missing_shyft_role_fails_before_sidecar_or_http(monkeypatch, tmp_path, role):
    from sniper.services import memecoin_real_runner as R
    from sniper.execution import solana_tpu_sidecar
    _armed_context(monkeypatch, tmp_path)
    monkeypatch.delenv(role)
    monkeypatch.setattr(solana_tpu_sidecar, "resolve_tpu_sidecar",
                        lambda **_kw: pytest.fail("invalid role reached sidecar"))
    with pytest.raises(ValueError, match="missing"):
        R.build_real_context(http=lambda *_a, **_k: pytest.fail("invalid role reached HTTP"))
