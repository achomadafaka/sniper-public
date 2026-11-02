"""Task 7 — EVM same-raw-transaction fanout providers.

One provider per configured RPC endpoint, all sent the SAME raw signed transaction. Returned hash
must equal the locally derived identity; already-known/imported is accepted (same bytes we sent);
only a definitive node rejection is EXPLICIT_REJECTED; timeout/transport is AMBIGUOUS. Provider
labels never contain the endpoint URL. All offline.
"""
import json

import pytest

from sniper.execution import evm_fast_submit as E
from sniper.execution.submission_transport import AttemptState, build_request


def _signed_raw(chain_id, nonce=1):
    """A real signed transaction: the EVM-08 intent fence never sends bytes without a chain id."""
    from eth_account import Account
    tx = {"type": 2, "chainId": chain_id, "nonce": nonce, "maxFeePerGas": 10,
          "maxPriorityFeePerGas": 0, "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""}
    return "0x" + bytes(Account.sign_transaction(tx, b"\x03" * 32).raw_transaction).hex()


RAW = _signed_raw(8453)


def _req(raw=None, identity="0xhash1"):
    return build_request(chain_id="8453", action="entry", tx_identity=identity,
                         signed_payload=RAW if raw is None else raw)


class RecordingHttp:
    def __init__(self, status=200, payload=None, raise_exc=None):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append({"url": url, "body": body})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.status, self.payload, None


def test_provider_accepts_matching_hash_sends_raw_tx():
    http = RecordingHttp(200, {"result": "0xHASH1"})
    prov = E.EvmRpcProvider(http, "https://secret-rpc/key", provider_id="8453:rpc0")
    attempt = prov.submit(_req(RAW, "0xhash1"))
    assert attempt.state is AttemptState.ACCEPTED       # case-insensitive hash match
    sent = json.loads(http.calls[0]["body"])
    assert sent["method"] == "eth_sendRawTransaction"
    assert sent["params"] == [RAW]                  # verbatim raw bytes


def test_provider_label_never_contains_url():
    http = RecordingHttp(200, {"result": "0xhash1"})
    prov = E.EvmRpcProvider(http, "https://secret-rpc/APIKEY", provider_id="8453:rpc1")
    assert "secret-rpc" not in prov.provider_id
    assert "APIKEY" not in prov.provider_id


def test_identity_mismatch_is_ambiguous():
    http = RecordingHttp(200, {"result": "0xOTHER"})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    assert prov.submit(_req(RAW, "0xhash1")).state is AttemptState.AMBIGUOUS


def test_already_known_is_accepted():
    http = RecordingHttp(200, {"error": {"message": "already known"}})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    attempt = prov.submit(_req(RAW, "0xhash1"))
    assert attempt.state is AttemptState.ACCEPTED
    assert attempt.returned_identity == "0xhash1"


def test_locally_invalid_transaction_is_explicit_rejected():
    http = RecordingHttp(200, {"error": {"message": "invalid sender"}})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    assert prov.submit(_req()).state is AttemptState.EXPLICIT_REJECTED


@pytest.mark.parametrize("message", [
    "nonce too low",
    "replacement transaction underpriced",
])
def test_state_dependent_rejection_is_ambiguous(message):
    http = RecordingHttp(200, {"error": {"message": message}})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    assert prov.submit(_req()).state is AttemptState.AMBIGUOUS


def test_insufficient_gas_is_explicit_rejected():
    http = RecordingHttp(200, {"error": {"message": "insufficient funds for gas * price + value"}})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    assert prov.submit(_req()).state is AttemptState.EXPLICIT_REJECTED


def test_transport_and_5xx_are_ambiguous():
    prov1 = E.EvmRpcProvider(RecordingHttp(raise_exc=RuntimeError("reset")), "u", provider_id="p")
    prov2 = E.EvmRpcProvider(RecordingHttp(502, {"error": "bad gateway"}), "u", provider_id="p")
    assert prov1.submit(_req()).state is AttemptState.AMBIGUOUS
    assert prov2.submit(_req()).state is AttemptState.AMBIGUOUS


def test_unclassified_error_is_ambiguous():
    http = RecordingHttp(200, {"error": {"message": "some transient node issue"}})
    prov = E.EvmRpcProvider(http, "https://rpc", provider_id="8453:rpc0")
    assert prov.submit(_req()).state is AttemptState.AMBIGUOUS


def test_same_raw_tx_delivered_byte_identically_to_every_endpoint():
    from sniper.execution.submission_transport import SubmissionTransport
    seen = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        seen.append((url, json.loads(body)["params"][0]))
        if url.endswith("/a"):
            return 200, {"error": {"message": "invalid sender"}}, None  # one rejects
        return 200, {"result": "0xhash1"}, None                          # one accepts

    providers = [E.EvmRpcProvider(http, "https://x/a", provider_id="8453:custody_rpc"),
                 E.EvmRpcProvider(http, "https://y/b", provider_id="8453:base_standard")]
    transport = SubmissionTransport(providers, deadline_s=1.0)
    try:
        res = transport.submit(_req(RAW, "0xhash1"))
        assert res.state is AttemptState.ACCEPTED       # one accept overrides one reject
        assert len(seen) == 2
        assert {payload for _u, payload in seen} == {RAW}       # byte-identical to all
        labels = [p.provider_id for p in providers]
        assert labels == ["8453:custody_rpc", "8453:base_standard"]
        assert all("https" not in lbl for lbl in labels)                 # labels never carry URLs
    finally:
        transport.close()
