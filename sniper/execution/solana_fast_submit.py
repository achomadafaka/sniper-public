"""Solana standard JSON-RPC ``sendTransaction`` submission provider.

``SolanaRpcProvider`` implements the transport contract — ``provider_id`` + ``submit(request)``
returning a ``SubmissionAttempt`` — over the injected http seam. It NEVER signs, quotes, or refreshes
a blockhash: it broadcasts ``request.signed_payload`` verbatim, so a same-bytes rebroadcast (the
recovery path re-invoking the transport) is bounded by the original blockhash validity, never a
fresh one.

Outcome normalization (money-safe):
  - a returned signature equal to the local identity, or an "already processed" response -> ACCEPTED;
  - a deterministically invalid transaction / expired blockhash -> EXPLICIT_REJECTED;
  - a timeout, disconnect, 5xx/4xx, or malformed response after a possible write -> AMBIGUOUS;
  - a returned identity that does NOT match the local one -> AMBIGUOUS (never accepted).

The removed Helius Sender / Jito providers required an extra on-chain tip and are intentionally not
part of the free-direct backend.
"""
from __future__ import annotations

import json
import time as _time

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
from sniper.execution.solana_rpc_pool import _CREDIT_EXHAUSTED, _credit_exhausted

# Only transaction-intrinsic failures are explicit. Blockhash/height and balance errors are state
# dependent: the same signature may already have landed through another provider, so they remain
# ambiguous until the confirmation/recovery path proves otherwise.
_DETERMINISTIC_REJECT = (
    "transaction too large", "signature verification failure", "sanitize",
)
_ALREADY_PROCESSED = ("already processed", "already been processed")


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


def _classify_rpc(http, url, method, params, request, provider_id, *, headers=None, timeout=None, observe_http_fn=None):
    started = _time.monotonic()

    def lat():
        return (_time.monotonic() - started) * 1000.0

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    kwargs = {"headers": headers, "body": body}
    if timeout is not None:
        kwargs["timeout"] = timeout
    def observe(phase):
        if observe_http_fn is not None:
            try:
                observe_http_fn(phase)
            except Exception:
                pass  # Diagnostics cannot prevent or reclassify a funded send.

    try:
        observe("started")
        try:
            status, payload, _date = http("POST", url, **kwargs)
        finally:
            observe("finished")
    except Exception:
        # a transport failure after a possible write is ambiguous, never rejected
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(), None, "transport")
    if status != 200 or not isinstance(payload, dict):
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                                 str(status), "http_%s" % status)
    if "result" in payload and "error" in payload:
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                                 "200", "malformed")
    result = payload.get("result")
    if result:
        if str(result) == str(request.tx_identity):
            return SubmissionAttempt(provider_id, AttemptState.ACCEPTED, str(result), lat(),
                                     "200", None)
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, str(result), lat(),
                                 "200", "identity_mismatch")
    err = payload.get("error")
    if err is not None:
        if (isinstance(err, dict) and err.get("code") in (_CREDIT_EXHAUSTED, "-32429")
                and not _credit_exhausted((status, payload), request_body=body)):
            return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                                     "200", "malformed")
        msg = str((err.get("message") if isinstance(err, dict) else err) or "").lower()
        if any(m in msg for m in _ALREADY_PROCESSED):
            return SubmissionAttempt(provider_id, AttemptState.ACCEPTED, str(request.tx_identity),
                                     lat(), "200", "already_processed")
        if any(m in msg for m in _DETERMINISTIC_REJECT):
            return SubmissionAttempt(provider_id, AttemptState.EXPLICIT_REJECTED, None, lat(),
                                     "200", _reason_with_error("rejected", msg))
        return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(),
                                 "200", _reason_with_error("unclassified_error", msg))
    return SubmissionAttempt(provider_id, AttemptState.AMBIGUOUS, None, lat(), "200", "malformed")


class SolanaRpcProvider:
    """Standard Solana JSON-RPC ``sendTransaction``. Baseline keeps preflight (skip_preflight=False);
    the fanout copy uses skip_preflight=True on the same tipped bytes."""

    def __init__(self, http, url, *, provider_id="solana:rpc", skip_preflight=False,
                 max_retries=0, timeout_s=None):
        self._http = http
        self._url = url
        self.provider_id = provider_id
        self._skip_preflight = bool(skip_preflight)
        self._max_retries = int(max_retries)
        self._timeout_s = timeout_s

    def submit(self, request, *, observe_http_fn=None):
        # A request can remove preflight for one send (the slot lane), never add it back.
        skip = self._skip_preflight or getattr(request, "skip_preflight", False) is True
        # The node cannot enforce our slot cap on its later rebroadcasts.
        retries = (0 if request.chain_id == "solana" and request.action == "entry"
                   and request.entry_max_slot is not None else self._max_retries)
        params = [request.signed_payload,
                  {"encoding": "base64", "skipPreflight": skip,
                   "maxRetries": retries}]
        if request.preflight_commitment is not None:
            params[1]["preflightCommitment"] = request.preflight_commitment
        return _classify_rpc(self._http, self._url, "sendTransaction", params, request,
                             self.provider_id, timeout=self._timeout_s, observe_http_fn=observe_http_fn)
