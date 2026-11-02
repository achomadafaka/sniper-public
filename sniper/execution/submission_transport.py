"""Typed same-identity submission transport for the funded memecoin backend.

One chain-agnostic contract for broadcasting an ALREADY-SIGNED transaction through one or more
providers with a bounded fanout deadline. The transport NEVER signs, quotes, or decides — it takes
the exact signed bytes it is given and delivers the SAME bytes to every provider, then aggregates
the provider verdicts into one outcome.

Aggregate precedence (money-safe):

    any ACCEPTED                           -> ACCEPTED
    else any AMBIGUOUS                      -> AMBIGUOUS
    else any EXPLICIT_REJECTED             -> EXPLICIT_REJECTED
    else (all NOT_SENT, or zero providers) -> AMBIGUOUS

``NOT_SENT`` means a provider proved that no transaction bytes crossed its boundary — excluded by a
wrong-chain health check, quarantined before submit, a sidecar unavailable before the local socket
write, or a queued worker that provably never started before the deadline. It is neutral when a real
provider attempted the transaction, and it prevents an unhealthy optional provider from fabricating a
false ambiguity; but when NOBODY attempted delivery (all NOT_SENT / zero providers) the aggregate is
AMBIGUOUS, never a fabricated safe rejection.

Only an all-explicit-rejection result may release/re-arm the owning reservation or claim. A timeout
or transport loss after a possible write is AMBIGUOUS, never rejected. A provider that returns an
unexpected transaction identity is AMBIGUOUS, never accepted. Provider workers receive only the
``SubmissionRequest`` (no DB handle) and touch no SQLite; the caller owns all persistence.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time as _time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional


def entry_slot_refusal(max_slot, head_fn, *, clock=_time.monotonic):
    """Require a fresh processed head, with an optional explicit legacy slot deadline.

    A head-only request permits any slot. A missing or >0.5 s old head still refuses.
    """
    if max_slot is None and head_fn is None:
        return None
    if max_slot is not None and (type(max_slot) is not int or max_slot <= 0):
        return "entry_slot_deadline_invalid"
    try:
        head, stamp = head_fn()
        if (type(head) is not int or head <= 0 or type(stamp) not in (int, float)
                or not math.isfinite(stamp)):
            return "entry_slot_head_unknown"
        age = clock() - stamp
        if not math.isfinite(age) or age < 0:
            return "entry_slot_head_unknown"
        if age > 0.5:
            return "entry_slot_head_stale"
    except Exception:
        return "entry_slot_head_unknown"
    return "entry_slot_expired" if max_slot is not None and head > max_slot else None


def _entry_request_slot_refusal(request, *, clock=_time.monotonic):
    if getattr(request, "chain_id", None) == "solana" and getattr(request, "action", None) == "entry":
        return entry_slot_refusal(request.entry_max_slot, request.entry_head_fn, clock=clock)
    return None


class AttemptState(str, Enum):
    ACCEPTED = "accepted"
    EXPLICIT_REJECTED = "explicit_rejected"
    AMBIGUOUS = "ambiguous"
    NOT_SENT = "not_sent"           # proved no bytes crossed this provider's boundary (neutral)


@dataclass(frozen=True)
class SubmissionAttempt:
    provider_id: str
    state: AttemptState
    returned_identity: Optional[str]
    latency_ms: Optional[float]                 # None: unknown (a loser still in flight)
    response_code: Optional[str]
    reason_code: Optional[str] = None
    provider_timing: Optional[dict] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SubmissionRequest:
    chain_id: str
    action: str
    tx_identity: str
    signed_payload: str
    signed_payload_sha256: str
    blockhash: Optional[str] = None
    last_valid_height: Optional[int] = None
    nonce: Optional[int] = None
    owner_kind: Optional[str] = None
    owner_id: Optional[str] = None
    created_at: Optional[str] = None
    # Solana only, and one-directional: True makes EVERY RPC leg skip preflight for this send; False
    # leaves each provider's own setting. It exists for the slot lane (2026-09-26, operator order):
    # the lane signs with a `confirmed` blockhash, so the custody leg's finalized-bank preflight
    # can answer "blockhash not found". Per request, never a provider mutation,
    # because one transport serves every funded thread.
    skip_preflight: bool = False
    preflight_commitment: Optional[str] = None
    entry_max_slot: Optional[int] = None
    entry_head_fn: Optional[Callable[[], tuple[Optional[int], Optional[float]]]] = field(
        default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.preflight_commitment is not None and self.preflight_commitment not in (
                "processed", "confirmed", "finalized"):
            raise ValueError("preflight_commitment must be processed, confirmed, finalized or None")


@dataclass(frozen=True)
class SubmissionResult:
    state: AttemptState
    tx_identity: str
    attempts: tuple = field(default_factory=tuple)
    accepted_by: Optional[str] = None
    signed_payload_sha256: Optional[str] = None
    # ONE provider's own words for a positive rejection, or None. Defaulted, so every existing
    # positional construction (including the persist-gate synthetic rejection in
    # `submission_wiring.guarded_submit`, which contacts no provider) keeps meaning "no words".
    message: Optional[str] = None
    # Separate final observations; they never revise the first-return aggregate.
    late_results: tuple = field(default_factory=tuple, repr=False, compare=False)


def sha256_hex(payload) -> str:
    raw = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_request(*, chain_id, action, tx_identity, signed_payload, blockhash=None,
                  last_valid_height=None, nonce=None, owner_kind=None, owner_id=None,
                  created_at=None, skip_preflight=False, preflight_commitment=None,
                  entry_max_slot=None, entry_head_fn=None) -> SubmissionRequest:
    """Assemble an immutable request, computing the payload hash from the exact signed bytes."""
    return SubmissionRequest(
        chain_id=str(chain_id), action=str(action), tx_identity=str(tx_identity),
        signed_payload=signed_payload, signed_payload_sha256=sha256_hex(signed_payload),
        blockhash=blockhash, last_valid_height=last_valid_height, nonce=nonce,
        owner_kind=owner_kind, owner_id=owner_id, created_at=created_at,
        skip_preflight=skip_preflight is True, preflight_commitment=preflight_commitment,
        entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn)


_REJECTION_MESSAGE_LIMIT = 160


def _rejection_message(attempts, state):
    """The rejecting provider's own words, named as that provider's -- or None (E9, 2026-09-23).

    `_reason_with_error` already puts the node's message on the attempt and the attempt audit
    already stores it; nothing joined it back to the caller, so a funded broadcast the node
    positively refused recorded a bare `broadcast_rejected` and could not say whether the nonce,
    the gas or the balance was the problem (PAWSINU 0xbcc948e7, 2026-09-17; 11 such attempts all
    time, 0 with a detail string).

    WHICH ATTEMPT WINS: the first EXPLICIT_REJECTED one in provider order. A result only reaches
    EXPLICIT_REJECTED when no attempt accepted and none was ambiguous, so the choice is between
    rejections, never between outcomes. The provider_id is part of the string precisely so one
    node's diagnosis can never read as consensus.

    ONLY on EXPLICIT_REJECTED, and only from a rejecting attempt. An accepted or ambiguous send
    has no rejection to describe, and an all-NOT_SENT set proved nothing crossed a provider
    boundary -- lending it a `reason_code` would invent words for bytes that never went out.
    Absent rather than wrong, the same rule E8 settled for the thread-local marker.
    """
    if state is not AttemptState.EXPLICIT_REJECTED:
        return None
    first = next((a for a in attempts if a.state is AttemptState.EXPLICIT_REJECTED), None)
    if first is None:
        return None
    text = " ".join(str(first.reason_code or "").split())
    words = ("%s: %s" % (first.provider_id, text)) if text else str(first.provider_id)
    # Capped WITH the prefix included: the attempt text is already bounded, but one pathological
    # node must not be able to bloat a reason string the ledger keeps.
    return words[:_REJECTION_MESSAGE_LIMIT] or None


def aggregate(request: SubmissionRequest, attempts) -> SubmissionResult:
    """Fold provider attempts into one outcome using the money-safe precedence.

    NOT_SENT is neutral: an EXPLICIT_REJECTED only wins when at least one real provider produced it,
    and an all-NOT_SENT (or zero-attempt) set is AMBIGUOUS, never a fabricated safe rejection."""
    attempts = tuple(attempts)
    accepted = next((a for a in attempts if a.state is AttemptState.ACCEPTED), None)
    if accepted is not None:
        state, accepted_by = AttemptState.ACCEPTED, accepted.provider_id
    elif any(a.state is AttemptState.AMBIGUOUS for a in attempts):
        state, accepted_by = AttemptState.AMBIGUOUS, None
    elif any(a.state is AttemptState.EXPLICIT_REJECTED for a in attempts):
        state, accepted_by = AttemptState.EXPLICIT_REJECTED, None
    else:
        # only NOT_SENT attempts (or none): nobody proved delivery AND nobody proved a real
        # rejection -> ambiguous / submitted_unknown, never a false release.
        state, accepted_by = AttemptState.AMBIGUOUS, None
    return SubmissionResult(state=state, tx_identity=request.tx_identity, attempts=attempts,
                            accepted_by=accepted_by,
                            signed_payload_sha256=request.signed_payload_sha256,
                            message=_rejection_message(attempts, state))


class SubmissionTransport:
    """Bounded same-bytes fanout over an ordered list of providers.

    A provider is any object with ``.provider_id`` and ``.submit(request) -> SubmissionAttempt``.
    ``submit`` returns as soon as one provider ACCEPTs or the ``deadline_s`` fanout deadline
    elapses, whichever is first; still-in-flight providers become AMBIGUOUS (never rejected).
    """

    def __init__(self, providers, *, deadline_s, verify_identity=True, clock=None):
        self._providers = list(providers)
        self._deadline_s = float(deadline_s)
        self._verify_identity = bool(verify_identity)
        self._clock = clock or _time.monotonic
        # At most one in-flight call per provider. This is acquired BEFORE queueing so a provider
        # hung past an earlier deadline cannot accumulate an unbounded ThreadPoolExecutor backlog.
        self._provider_leases = [(p, threading.Lock()) for p in self._providers]
        # One bounded, reused executor for the transport's lifetime — no per-call thread pool and
        # no unbounded future accumulation. At least one worker even with zero providers.
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(16, len(self._providers))),
            thread_name_prefix="memecoin-submit")

    def provider(self, provider_id):
        """The configured provider with this id, or None."""
        return next((p for p in self._providers if p.provider_id == provider_id), None)

    def submit_one(self, provider_id, request: SubmissionRequest):
        """One provider only, synchronously, under the same per-provider lease: the slot lane's
        re-send of identical bytes (2026-09-28). None when no such provider is configured; a busy
        lease is NOT_SENT, never a wait."""
        for provider, lease in self._provider_leases:
            if provider.provider_id != provider_id:
                continue
            if not lease.acquire(blocking=False):
                return SubmissionAttempt(provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                         "provider_busy")
            try:
                refused = _entry_request_slot_refusal(request, clock=self._clock)
                if refused:
                    return SubmissionAttempt(provider_id, AttemptState.NOT_SENT, None, 0.0, None, refused)
                return provider.submit(request)
            finally:
                lease.release()
        return None

    def submit(self, request: SubmissionRequest) -> SubmissionResult:
        if not self._providers:
            # Nothing to submit through -> ambiguous: never a false accept or a false reject.
            return SubmissionResult(AttemptState.AMBIGUOUS, request.tx_identity, (), None,
                                    request.signed_payload_sha256)
        deadline = self._clock() + self._deadline_s
        attempts = []
        futures = {}
        for provider, lease in self._provider_leases:
            if not lease.acquire(blocking=False):
                attempts.append(SubmissionAttempt(
                    provider.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                    "provider_busy"))
                continue
            try:
                future = self._executor.submit(
                    self._run_one_with_lease, provider, request, lease)
            except Exception:
                lease.release()
                attempts.append(SubmissionAttempt(
                    provider.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                    "not_started"))
                continue
            futures[future] = (provider, lease)
        pending = set(futures)
        while pending:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for fut in done:
                attempts.append(fut.result())        # _run_one never raises
            if any(a.state is AttemptState.ACCEPTED for a in attempts):
                break
        # Resolve anything still pending at the deadline (or after an early accept). ``cancel()``
        # returns True only for a future that provably never started running: no bytes crossed that
        # provider's boundary -> NOT_SENT. A future already running may have a write on the wire ->
        # AMBIGUOUS, never rejected.
        # SEND AFTER ACCEPT (Sender A/B, 2026-10-01): a provider marked `send_after_accept` is NOT
        # cancelled once another provider accepted: the same signed bytes are already accepted, so
        # its later send cannot fill twice, and a fast TPU accept must not silently drop an arm-B
        # Sender send that had not reached a worker yet. Without an accept it is cancelled as today,
        # so a release can never be followed by a late send.
        accepted = any(a.state is AttemptState.ACCEPTED for a in attempts)
        for fut in pending:
            prov, lease = futures[fut]
            if accepted and getattr(prov, "send_after_accept", False) is True:
                attempts.append(SubmissionAttempt(
                    prov.provider_id, AttemptState.AMBIGUOUS, None, None, None, "after_accept"))
                continue
            if fut.cancel():
                lease.release()                    # wrapper never ran, so it cannot release its lease
                attempts.append(SubmissionAttempt(
                    prov.provider_id, AttemptState.NOT_SENT, None, 0.0, None, "not_started"))
            elif accepted:
                # A LOSER OF THE FIRST-ACCEPT RACE IS NOT A DEADLINE (Alpha 2026-10-02). It was
                # stamped "deadline" at the whole fanout latency, so 41 and 43 of 47 slot sends
                # read as custody and public RPC timing out. Still AMBIGUOUS (its bytes may be on
                # the wire); its latency is unknown, never the deadline.
                attempts.append(SubmissionAttempt(
                    prov.provider_id, AttemptState.AMBIGUOUS, None, None, None,
                    "in_flight_after_accept"))
            else:
                attempts.append(SubmissionAttempt(
                    prov.provider_id, AttemptState.AMBIGUOUS, None, self._deadline_s * 1000.0,
                    None, "deadline"))
        return replace(aggregate(request, attempts),
                       late_results=tuple(fut for fut in pending if not fut.cancelled()))

    def _run_one_with_lease(self, provider, request, lease) -> SubmissionAttempt:
        try:
            return self._run_one(provider, request)
        finally:
            lease.release()

    def _run_one(self, provider, request) -> SubmissionAttempt:
        started = self._clock()
        refused = _entry_request_slot_refusal(request, clock=self._clock)
        if refused:
            return SubmissionAttempt(provider.provider_id, AttemptState.NOT_SENT, None, 0.0, None, refused)
        try:
            attempt = provider.submit(request)
        except Exception:
            # A provider that raised mid-write may still have delivered the bytes -> AMBIGUOUS.
            return SubmissionAttempt(
                getattr(provider, "provider_id", "unknown"), AttemptState.AMBIGUOUS, None,
                (self._clock() - started) * 1000.0, None, "provider_exception")
        if (not isinstance(attempt, SubmissionAttempt)
                or not isinstance(attempt.state, AttemptState)):
            return SubmissionAttempt(
                getattr(provider, "provider_id", "unknown"), AttemptState.AMBIGUOUS, None,
                (self._clock() - started) * 1000.0, None, "invalid_provider_result")
        # Defensive identity enforcement: even if a provider mislabels a mismatched echo as
        # ACCEPTED, a missing or unexpected returned identity can never be an accept.
        if (self._verify_identity and attempt.state is AttemptState.ACCEPTED
                and attempt.returned_identity is None):
            return SubmissionAttempt(
                attempt.provider_id, AttemptState.AMBIGUOUS, None,
                attempt.latency_ms, attempt.response_code, "identity_missing")
        if (self._verify_identity and attempt.state is AttemptState.ACCEPTED
                and str(attempt.returned_identity) != str(request.tx_identity)):
            return SubmissionAttempt(
                attempt.provider_id, AttemptState.AMBIGUOUS, attempt.returned_identity,
                attempt.latency_ms, attempt.response_code, "identity_mismatch")
        return attempt

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
