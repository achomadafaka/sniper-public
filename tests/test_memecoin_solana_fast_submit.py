"""Solana standard JSON-RPC submission provider (``SolanaRpcProvider``).

The provider implements the transport contract (.provider_id + .submit(request)->SubmissionAttempt)
over an injected http seam. Proves: matching signature -> ACCEPTED; identity mismatch -> AMBIGUOUS
(never accepted); transport failure / 5xx -> AMBIGUOUS; deterministic tx rejection -> EXPLICIT_REJECTED;
already-processed -> ACCEPTED; the signed bytes are sent verbatim (no blockhash refresh). The removed
tipped Sender/Jito providers are no longer part of the free-direct backend. All offline.
"""
import json

import pytest

from sniper.execution import solana_fast_submit as P
from sniper.execution.submission_transport import AttemptState, build_request


def _req(payload, identity):
    return build_request(chain_id="solana", action="entry", tx_identity=identity,
                         signed_payload=payload)


class RecordingHttp:
    def __init__(self, status=200, payload=None, raise_exc=None):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "body": body})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.status, self.payload, None


def test_rpc_provider_accepts_matching_signature():
    http = RecordingHttp(200, {"result": "SIG1"})
    prov = P.SolanaRpcProvider(http, "https://rpc", skip_preflight=True)
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.ACCEPTED
    assert attempt.returned_identity == "SIG1"
    sent = json.loads(http.calls[0]["body"])
    assert sent["method"] == "sendTransaction"
    assert sent["params"][0] == "PAYLOAD"                       # verbatim signed bytes
    assert sent["params"][1] == {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}


@pytest.mark.parametrize("configured, requested, expected", [
    (False, False, False),   # custody leg, J7 entry / exit: preflight stays ON
    (False, True, True),     # custody leg, slot-lane send: the request removes it
    (True, False, True),     # public leg: a request can never ADD preflight back
    (True, True, True),
])
def test_rpc_provider_request_can_only_remove_preflight(configured, requested, expected):
    # Slot lane 2026-09-26: the custody leg (configured skip_preflight=False) answered "blockhash
    # not found" on every lane send because its preflight runs at the finalized bank.
    http = RecordingHttp(200, {"result": "SIG1"})
    prov = P.SolanaRpcProvider(http, "https://custody", skip_preflight=configured, max_retries=3)
    req = build_request(chain_id="solana", action="entry", tx_identity="SIG1",
                        signed_payload="PAYLOAD", skip_preflight=requested)
    prov.submit(req)
    assert json.loads(http.calls[0]["body"])["params"][1] == {
        "encoding": "base64", "skipPreflight": expected, "maxRetries": 3}
    assert prov._skip_preflight is configured          # never mutated: one transport, many threads


def test_request_skip_preflight_accepts_only_true():
    # A truthy non-bool (a stray "0" from a knob) must not strip a funded send's preflight.
    for value in ("0", "false", 1, None):
        req = build_request(chain_id="solana", action="entry", tx_identity="S",
                            signed_payload="P", skip_preflight=value)
        assert req.skip_preflight is False


@pytest.mark.parametrize("commitment", ["processed", "confirmed", "finalized"])
@pytest.mark.parametrize("skip_preflight", [False, True])
def test_rpc_provider_uses_request_preflight_commitment(commitment, skip_preflight):
    http = RecordingHttp(200, {"result": "SIG1"})
    provider = P.SolanaRpcProvider(http, "https://rpc", skip_preflight=skip_preflight,
                                   max_retries=3)
    request = build_request(chain_id="solana", action="withdraw", tx_identity="SIG1",
                            signed_payload="PAYLOAD", preflight_commitment=commitment)
    assert provider.submit(request).state is AttemptState.ACCEPTED
    sent = json.loads(http.calls[0]["body"])
    assert sent["params"] == ["PAYLOAD", {
        "encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 3,
        "preflightCommitment": commitment}]


def test_rpc_preflight_commitment_does_not_leak_between_concurrent_requests():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    sent = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        sent.append(json.loads(body))
        barrier.wait(timeout=2)
        return 200, {"result": "SIG1"}, None

    provider = P.SolanaRpcProvider(http, "https://rpc", max_retries=3)
    withdrawal = build_request(chain_id="solana", action="withdraw", tx_identity="SIG1",
                               signed_payload="WITHDRAW", preflight_commitment="confirmed")
    entry = _req("ENTRY", "SIG1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(provider.submit, (withdrawal, entry)))
    assert all(result.state is AttemptState.ACCEPTED for result in results)
    options = {call["params"][0]: call["params"][1] for call in sent}
    assert options["WITHDRAW"] == {
        "encoding": "base64", "skipPreflight": False, "maxRetries": 3,
        "preflightCommitment": "confirmed"}
    assert options["ENTRY"] == {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}
    assert provider._skip_preflight is False


@pytest.mark.parametrize("action,maximum,want", [
    ("entry", 101, 0),
    ("entry", None, 3),
    ("exit", 101, 3),
    ("exit", None, 3),
    ("withdraw", 101, 3),
])
def test_slot_bounded_entries_do_not_delegate_retries_past_their_deadline(action, maximum, want):
    http = RecordingHttp(200, {"result": "SIG1"})
    provider = P.SolanaRpcProvider(http, "https://custody", max_retries=3)
    request = build_request(chain_id="solana", action=action, tx_identity="SIG1",
                            signed_payload="PAYLOAD", entry_max_slot=maximum,
                            skip_preflight=True, preflight_commitment="confirmed")
    assert provider.submit(request).state is AttemptState.ACCEPTED
    sent = json.loads(http.calls[0]["body"])
    assert sent["params"] == ["PAYLOAD", {
        "encoding": "base64", "skipPreflight": True, "maxRetries": want,
        "preflightCommitment": "confirmed"}]
    assert provider._max_retries == 3


def test_bounded_entry_retry_options_do_not_leak_to_concurrent_exits():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    sent = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        sent.append(json.loads(body))
        barrier.wait(timeout=2)
        return 200, {"result": "SIG1"}, None

    provider = P.SolanaRpcProvider(http, "https://custody", max_retries=3)
    entry = build_request(chain_id="solana", action="entry", tx_identity="SIG1",
                          signed_payload="ENTRY", entry_max_slot=101)
    exit_request = build_request(chain_id="solana", action="exit", tx_identity="SIG1",
                                 signed_payload="EXIT", entry_max_slot=101)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(provider.submit, (entry, exit_request)))
    assert all(result.state is AttemptState.ACCEPTED for result in results)
    options = {call["params"][0]: call["params"][1] for call in sent}
    assert options["ENTRY"]["maxRetries"] == 0
    assert options["EXIT"]["maxRetries"] == 3
    assert provider._max_retries == 3


def test_rpc_identity_mismatch_is_ambiguous_never_accepted():
    http = RecordingHttp(200, {"result": "OTHER"})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.AMBIGUOUS
    assert attempt.reason_code == "identity_mismatch"


def test_rpc_transport_error_is_ambiguous():
    http = RecordingHttp(raise_exc=RuntimeError("connection reset"))
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.AMBIGUOUS


def test_rpc_server_5xx_is_ambiguous():
    http = RecordingHttp(503, {"error": "unavailable"})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.AMBIGUOUS


def test_rpc_deterministic_rejection_is_explicit_rejected():
    http = RecordingHttp(200, {"error": {"message": "signature verification failure"}})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.EXPLICIT_REJECTED


@pytest.mark.parametrize("message", [
    "Blockhash not found",
    "block height exceeded",
    "insufficient funds for fee",
])
def test_rpc_state_dependent_rejection_is_ambiguous(message):
    http = RecordingHttp(200, {"error": {"message": message}})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    assert prov.submit(_req("PAYLOAD", "SIG1")).state is AttemptState.AMBIGUOUS


def test_rpc_already_processed_is_accepted():
    http = RecordingHttp(200, {"error": {"message": "This transaction has already been processed"}})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.ACCEPTED


def test_rpc_unclassified_error_is_ambiguous():
    http = RecordingHttp(200, {"error": {"message": "node is behind by 42 slots"}})
    prov = P.SolanaRpcProvider(http, "https://rpc")
    attempt = prov.submit(_req("PAYLOAD", "SIG1"))
    assert attempt.state is AttemptState.AMBIGUOUS
