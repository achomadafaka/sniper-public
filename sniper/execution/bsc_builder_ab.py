"""BSC (56) builder A/B on the main entry path (operator order 2026-10-09 via Alpha).

Source: Lambda's docs/audits/2026-10-09-memecoin-evm-fast-inclusion-options.md, section 2, option 2.
Behind MEMECOIN_BSC_BUILDER_AB (default off), a 56 entry splits on rule
bsc-builder-sha256-attempt-odd-v1 over its pending row's attempt_id. Arm B also hands its signed
SWAP bytes to 48 Club Puissant, and to BlockRazor when MEMECOIN_BSC_BLOCKRAZOR_AUTH is set, as an
eth_sendBundle that expires 30 s after the send.

Why a bundle and not a private raw send: on 56 the never-admitted dead proof releases a reservation
after 300 s when custody shows pending == latest, and custody cannot see a copy a builder holds
privately. A bundle that expires at +30 s cannot land after that release.

Each builder is one more provider in the 56 transport, last and send_after_accept: it runs on its
own worker from dispatch, is never waited on and is never cancelled after custody accepts. It posts
only identities the arm-B entry path marked (the unwrap pre-transaction, approvals, exits and every
other chain are NOT_SENT). A bundle reply is the builder's acceptance, not inclusion, and a dropped
bundle is silent, so every answer reads AMBIGUOUS for that builder only. Cost: none (no tip, no
bundle payment). The token is sent only in the Authorization header and never logged or recorded.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
import time as _time

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt

RULE = "bsc-builder-sha256-attempt-odd-v1"
ALL_RULE = "bsc-builder-all-b-v1"
PUISSANT_BUNDLE_URL = "https://puissant-builder.48.club/"
BLOCKRAZOR_URL = "https://virginia.builder.blockrazor.io"
BUNDLE_TTL_S = 30                      # far inside the 300 s never-admitted dead proof
PROVIDER_IDS = ("56:puissant_bundle", "56:blockrazor_bundle")

_marks_lock = threading.Lock()
_marks = collections.OrderedDict()     # tx identity -> True, for arm-B swaps only
_MARKS_CAP = 256


def _flag(name, env=None):
    raw = str((os.environ if env is None else env).get(name, "") or "")
    return raw.strip().lower() in ("1", "true", "on", "yes")


def all_on(env=None):
    """MEMECOIN_BSC_BUILDER_ALL (operator 2026-10-09: "everything turned on live"): every 56 main-path
    entry swap also goes as a builder bundle; no control half."""
    return _flag("MEMECOIN_BSC_BUILDER_ALL", env)


def knob_on(env=None):
    return _flag("MEMECOIN_BSC_BUILDER_AB", env) or all_on(env)


def assign(attempt_id, env=None):
    """The pre-registered arm: B iff the first 32 bits of sha256(attempt_id) are odd; every attempt is
    B under MEMECOIN_BSC_BUILDER_ALL, with its own rule name. None without an attempt id (no arm,
    today's send)."""
    if not attempt_id:
        return None
    digest = hashlib.sha256(str(attempt_id).encode("utf-8")).digest()
    if all_on(env):
        return {"arm": "B", "rule": ALL_RULE, "attempt_hash": digest[:4].hex()}
    arm = "B" if int.from_bytes(digest[:4], "big") % 2 == 1 else "A"
    return {"arm": arm, "rule": RULE, "attempt_hash": digest[:4].hex()}


def mark(tx_identity):
    with _marks_lock:
        _marks[str(tx_identity).lower()] = True
        while len(_marks) > _MARKS_CAP:
            _marks.popitem(last=False)


def is_marked(tx_identity):
    with _marks_lock:
        return str(tx_identity).lower() in _marks


def _reset_marks():
    with _marks_lock:
        _marks.clear()


class BundleProvider:
    """One builder's eth_sendBundle for marked arm-B entry swaps. Keeps its own answer per identity
    (answer, HTTP status, ms) for the trace, capped, under its own lock."""

    send_after_accept = True

    def __init__(self, http, *, url, provider_id, auth=None, timeout_s=None, clock=_time.time,
                 cap=256):
        self._http, self._url, self.provider_id = http, url, provider_id
        self._auth, self._timeout_s, self._clock = auth, timeout_s, clock
        self._lock = threading.Lock()
        self._outcomes = collections.OrderedDict()
        self._cap = int(cap)

    def __repr__(self):
        return "BundleProvider(%s)" % self.provider_id     # never the token

    def submit(self, request):
        if request.action != "entry" or not is_marked(request.tx_identity):
            return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                     "not_arm_b")
        bundle = {"txs": [request.signed_payload],
                  "maxTimestamp": int(self._clock()) + BUNDLE_TTL_S}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle",
                           "params": [bundle]})
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        from sniper.execution import exec_lock
        try:
            intent = exec_lock.intent_send(request.signed_payload, "bundle")
        except exec_lock.IntentUnwritable:
            return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                     "intent_unwritable")
        started = _time.perf_counter()
        status = None
        try:
            kwargs = {"headers": headers, "body": body}
            if self._timeout_s is not None:
                kwargs["timeout"] = self._timeout_s
            status, payload, _date = self._http("POST", self._url, **kwargs)
            answer = _answer(status, payload)
        except Exception as exc:
            answer = "transport:%s" % type(exc).__name__
        finally:
            exec_lock.stamp_send(request.signed_payload, intent=intent)
        ms = round((_time.perf_counter() - started) * 1000.0, 3)
        self._put(request.tx_identity, {"answer": self._clean(answer), "ms": ms,
                                        "http_status": None if status is None else str(status),
                                        "max_timestamp": bundle["maxTimestamp"],
                                        "sha256": request.signed_payload_sha256})
        return SubmissionAttempt(self.provider_id, AttemptState.AMBIGUOUS, None, ms,
                                 None if status is None else str(status), "bundle")

    def _clean(self, text):
        text = " ".join(str(text).split())[:120]
        return text.replace(self._auth, "[REDACTED]") if self._auth else text

    def outcome(self, tx_identity):
        with self._lock:
            row = self._outcomes.get(str(tx_identity).lower())
            return dict(row) if row is not None else None

    def _put(self, tx_identity, row):
        with self._lock:
            self._outcomes[str(tx_identity).lower()] = row
            self._outcomes.move_to_end(str(tx_identity).lower())
            while len(self._outcomes) > self._cap:
                self._outcomes.popitem(last=False)


def _answer(status, payload):
    if status != 200:
        return "http_%s" % status
    if not isinstance(payload, dict):
        return "malformed"
    if payload.get("result"):
        return "ok"
    error = payload.get("error")
    if isinstance(error, dict):
        return "error code=%s message=%s" % (error.get("code"), error.get("message"))
    return "error %s" % error if error is not None else "no_result"


def providers(http, *, env=None, timeout_s=None):
    """The builder providers for the 56 transport: none with the knob off; Puissant with it on; and
    BlockRazor too when the operator's token is set."""
    env = os.environ if env is None else env
    if not knob_on(env):
        return []
    out = [BundleProvider(http, url=PUISSANT_BUNDLE_URL, provider_id=PROVIDER_IDS[0],
                          timeout_s=timeout_s)]
    token = str(env.get("MEMECOIN_BSC_BLOCKRAZOR_AUTH", "") or "").strip()
    if token:
        out.append(BundleProvider(http, url=BLOCKRAZOR_URL, provider_id=PROVIDER_IDS[1],
                                  auth=token, timeout_s=timeout_s))
    return out
