"""EVM same-raw-transaction delivery provider.

One ``EvmRpcProvider`` per endpoint. The shared transport delivers the SAME raw signed transaction to
every endpoint (warm custody RPC + chain-owner official endpoints) concurrently; this mirrors the
exact tri-state semantics the executor already relies on from ``evm_wallet.eth_send_raw`` (a returned
hash / already-known is accepted, a definitive node rejection is explicit, transport/ambiguous loss is
neither), so covering approvals, pre-transactions, entry swaps, and full/partial exits is uniform.

Provider labels come from the caller (``<chain>:custody_rpc``, ``<chain>:base_standard``, …) — they
never contain the endpoint URL or credentials.
"""
from __future__ import annotations

import json
import time as _time

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt

_ALREADY_KNOWN = ("already known", "known transaction", "already imported")
# Only transaction-intrinsic failures are explicit rejections. State-dependent errors such as
# nonce-too-low, underpriced, or base-fee can also be returned after another provider accepted the
# same bytes. A gas-funds failure occurs before the node accepts the transaction.
_DETERMINISTIC_REJECT = (
    "invalid sender", "invalid chain id", "rlp",
    "intrinsic gas too low", "transaction type not supported",
    "insufficient funds for gas * price + value",
)


def _reason_with_error(code, msg, limit=160):
    """Carry the node's own words next to the classification.

    The classifier reduced every RPC refusal to a bare `rejected` or `unclassified_error`, so a
    dead transaction left nothing readable about WHY. Live 2026-08-23: two funded Solana entries
    were refused by both real RPCs and the stored rows could not say that the cause was
    "transaction too large" — it took decoding the persisted payloads to find the 1232-byte packet
    limit. The code stays the prefix so it is still greppable; the message is truncated so one
    pathological node cannot bloat the audit table.
    """
    text = " ".join(str(msg or "").split())
    if not text:
        return code
    room = max(0, limit - len(code) - 2)
    if len(text) > room:
        text = text[:room - 1] + "\u2026" if room > 1 else ""
    return ("%s: %s" % (code, text)) if text else code


def _classify_evm(http, url, request, provider_id, *, timeout=None):
    started = _time.monotonic()
    from sniper.execution.free_endpoint_registry import private_only_refusal
    refusal = private_only_refusal(request.signed_payload, url)
    if refusal:
        # Chain-1 bytes off Flashbots: nothing crosses this boundary (spec v2 §3.6).
        return SubmissionAttempt(provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                 "private_only_chain")

    def lat():
        return (_time.monotonic() - started) * 1000.0

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
                       "params": [request.signed_payload]})
    kwargs = {"headers": None, "body": body}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        status, payload, _date = http("POST", url, **kwargs)
    except Exception:
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(), None, "transport")
    if status != 200 or not isinstance(payload, dict):
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                                 str(status), "http_%s" % status)
    result = payload.get("result")
    if result:
        if str(result).lower() == str(request.tx_identity).lower():
            return SubmissionAttempt(provider_id, AttemptState.ACCEPTED, str(result), lat(),
                                     "200", None)
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, str(result), lat(),
                                 "200", "identity_mismatch")
    err = payload.get("error")
    msg = str((err.get("message") if isinstance(err, dict) else err) or "").lower()
    if any(k in msg for k in _ALREADY_KNOWN):
        # already known/imported: the node already holds THESE exact bytes -> identity matches by
        # construction (we derived the hash locally and sent our own tx) -> accepted.
        return SubmissionAttempt(provider_id, AttemptState.ACCEPTED, str(request.tx_identity),
                                 lat(), "200", "already_known")
    if any(k in msg for k in _DETERMINISTIC_REJECT):
        return SubmissionAttempt(provider_id, AttemptState.EXPLICIT_REJECTED, None, lat(),
                                 "200", _reason_with_error("rejected", msg))
    return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                             "200", _reason_with_error("unclassified_error", msg))


class EvmRpcProvider:
    """One EVM RPC endpoint. ``submit`` broadcasts ``request.signed_payload`` (the raw signed tx)
    verbatim — never re-signs, never changes the nonce."""

    def __init__(self, http, url, *, provider_id, timeout_s=None):
        self._http = http
        self._url = url
        self.provider_id = provider_id
        self._timeout_s = timeout_s

    def submit(self, request):
        from sniper.execution import exec_lock
        try:
            intent = exec_lock.intent_send(request.signed_payload, "rpc")
        except exec_lock.IntentUnwritable:
            # EVM-08: no intent record, no send; this provider provably sent nothing.
            return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                     "intent_unwritable")
        try:
            return _classify_evm(self._http, self._url, request, self.provider_id,
                                 timeout=self._timeout_s)
        finally:
            exec_lock.stamp_send(request.signed_payload, intent=intent)  # every broadcast (Gamma MEDIUM)
