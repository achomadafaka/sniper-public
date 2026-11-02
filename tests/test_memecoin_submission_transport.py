"""Task 2 — typed same-identity submission transport tests.

The aggregate precedence and identity/byte-identity guarantees are load-bearing money safety:
one accepted wins over rejects; an ambiguous blocks release; an unexpected returned identity is
never accepted; a slow provider cannot exceed the bounded fanout deadline; workers get only the
request (no DB handle). All offline with stub providers.
"""
import threading
import time

import pytest

from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionRequest, SubmissionResult, SubmissionTransport,
    aggregate, build_request, sha256_hex,
)


class StubProvider:
    def __init__(self, provider_id, state, *, returned_identity="same", code="200",
                 reason=None, block=None):
        self.provider_id = provider_id
        self.state = state
        self.returned_identity = returned_identity
        self.code = code
        self.reason = reason
        self.block = block           # threading.Event to hold the worker past the deadline
        self.seen_payloads = []
        self.seen_types = []

    def submit(self, request):
        self.seen_payloads.append(request.signed_payload)
        self.seen_types.append(type(request).__name__)
        if self.block is not None:
            self.block.wait(2.0)
        ident = (request.tx_identity if self.returned_identity == "same"
                 else self.returned_identity)
        return SubmissionAttempt(self.provider_id, self.state, ident, 1.0, self.code, self.reason)


def _req(**kw):
    base = dict(chain_id="solana", action="entry", tx_identity="SIG1",
                signed_payload="AAAA")
    base.update(kw)
    return build_request(**base)



# -- the rejecting provider's own words travel with the outcome (E9, 2026-09-23) ----------------
#
# `_reason_with_error` already carries the node's message on the attempt, and the attempt audit
# already stores it -- but nothing joined it back to the caller, so a funded broadcast that a node
# positively refused recorded a bare `broadcast_rejected` and could not say whether the nonce, the
# gas or the balance was the problem (PAWSINU 0xbcc948e7, 2026-09-17). 11 such attempts all time,
# 0 carrying a detail string.


def _attempt(provider_id, state, reason=None):
    return SubmissionAttempt(provider_id, state, None, 1.0, "200", reason)


def test_aggregate_carries_the_first_rejecting_providers_words():
    res = aggregate(_req(), (
        _attempt("rpc_a", AttemptState.NOT_SENT, "not_sent"),
        _attempt("rpc_b", AttemptState.EXPLICIT_REJECTED, "rejected: nonce too low"),
        _attempt("rpc_c", AttemptState.EXPLICIT_REJECTED, "rejected: insufficient funds"),
    ))
    assert res.state is AttemptState.EXPLICIT_REJECTED
    # ONE provider's words, named as that provider's -- never a consensus, and never the second
    # rejection's different diagnosis silently standing in for the first.
    assert res.message == "rpc_b: rejected: nonce too low"


def test_aggregate_message_is_none_when_nothing_was_positively_rejected():
    """Absent rather than wrong: an accepted or ambiguous send has no rejection to describe, and
    an all-NOT_SENT set proved nothing left the boundary -- borrowing its reason_code would be
    inventing words for bytes that never went out."""
    accepted = aggregate(_req(), (
        _attempt("rpc_a", AttemptState.EXPLICIT_REJECTED, "rejected: nonce too low"),
        _attempt("rpc_b", AttemptState.ACCEPTED, None),
    ))
    ambiguous = aggregate(_req(), (
        _attempt("rpc_a", AttemptState.EXPLICIT_REJECTED, "rejected: nonce too low"),
        _attempt("rpc_b", AttemptState.AMBIGUOUS, "transport"),
    ))
    not_sent = aggregate(_req(), (_attempt("rpc_a", AttemptState.NOT_SENT, "oversized_tx"),))
    empty = aggregate(_req(), ())
    assert accepted.state is AttemptState.ACCEPTED and accepted.message is None
    assert ambiguous.state is AttemptState.AMBIGUOUS and ambiguous.message is None
    assert not_sent.state is AttemptState.AMBIGUOUS and not_sent.message is None
    assert empty.message is None


def test_aggregate_message_survives_a_provider_with_no_words_and_caps_a_verbose_one():
    """A silent node degrades to the bare class, no worse than before. A pathological one cannot
    bloat the reason: the attempt text is already capped, and the published string is capped
    again with its provider prefix included."""
    silent = aggregate(_req(), (_attempt("rpc_a", AttemptState.EXPLICIT_REJECTED, None),))
    assert silent.message == "rpc_a"
    loud = aggregate(_req(), (
        _attempt("rpc_a", AttemptState.EXPLICIT_REJECTED, "rejected: " + "x" * 400),))
    assert len(loud.message) <= 160

def test_build_request_computes_payload_hash():
    r = _req()
    assert r.signed_payload_sha256 == sha256_hex("AAAA")
    assert isinstance(r, SubmissionRequest)


@pytest.mark.parametrize("commitment", [None, "processed", "confirmed", "finalized"])
def test_request_preflight_commitment_is_optional_and_immutable(commitment):
    from dataclasses import FrozenInstanceError

    request = _req(preflight_commitment=commitment)
    assert request.preflight_commitment == commitment
    with pytest.raises(FrozenInstanceError):
        request.preflight_commitment = "processed"
    assert _req().preflight_commitment is None


@pytest.mark.parametrize("commitment", ["", "recent", "CONFIRMED", 0, True, []])
def test_request_rejects_invalid_preflight_commitment(commitment):
    with pytest.raises(ValueError, match="preflight_commitment"):
        _req(preflight_commitment=commitment)
    with pytest.raises(ValueError, match="preflight_commitment"):
        SubmissionRequest("solana", "withdraw", "SIG1", "AAAA", sha256_hex("AAAA"),
                          preflight_commitment=commitment)


def test_accepted_plus_rejected_is_accepted():
    t = SubmissionTransport(
        [StubProvider("rpc", AttemptState.EXPLICIT_REJECTED),
         StubProvider("sender", AttemptState.ACCEPTED)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.ACCEPTED
        assert res.accepted_by == "sender"
        assert res.tx_identity == "SIG1"
    finally:
        t.close()


def test_ambiguous_plus_rejected_is_ambiguous():
    t = SubmissionTransport(
        [StubProvider("rpc", AttemptState.EXPLICIT_REJECTED),
         StubProvider("jito", AttemptState.AMBIGUOUS)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.accepted_by is None
    finally:
        t.close()


def test_all_explicit_rejected_is_rejected():
    t = SubmissionTransport(
        [StubProvider("rpc", AttemptState.EXPLICIT_REJECTED),
         StubProvider("sender", AttemptState.EXPLICIT_REJECTED)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.EXPLICIT_REJECTED
    finally:
        t.close()


def test_unexpected_returned_identity_is_ambiguous_never_accepted():
    t = SubmissionTransport(
        [StubProvider("sender", AttemptState.ACCEPTED, returned_identity="OTHER_SIG")],
        deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.accepted_by is None
        mism = [a for a in res.attempts if a.reason_code == "identity_mismatch"]
        assert mism and mism[0].state is AttemptState.AMBIGUOUS
    finally:
        t.close()


def test_accepted_without_returned_identity_is_ambiguous_never_accepted():
    t = SubmissionTransport(
        [StubProvider("sender", AttemptState.ACCEPTED, returned_identity=None)],
        deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.accepted_by is None
        assert res.attempts[0].reason_code == "identity_missing"
    finally:
        t.close()


def test_malformed_provider_result_is_ambiguous_not_an_exception():
    class Malformed:
        provider_id = "sender"

        def submit(self, request):
            return None

    t = SubmissionTransport([Malformed()], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.attempts[0].reason_code == "invalid_provider_result"
    finally:
        t.close()


def test_provider_result_with_invalid_state_is_ambiguous():
    class InvalidState:
        provider_id = "sender"

        def submit(self, request):
            return SubmissionAttempt("sender", "accepted", request.tx_identity, 1.0, "200", None)

    t = SubmissionTransport([InvalidState()], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.attempts[0].reason_code == "invalid_provider_result"
    finally:
        t.close()


def test_all_providers_receive_byte_identical_payload():
    ps = [StubProvider("rpc", AttemptState.ACCEPTED),
          StubProvider("sender", AttemptState.ACCEPTED),
          StubProvider("jito", AttemptState.ACCEPTED)]
    t = SubmissionTransport(ps, deadline_s=1.0)
    try:
        t.submit(_req(signed_payload="DEADBEEF"))
        # every provider that ran saw the exact same bytes
        seen = [p.seen_payloads[0] for p in ps if p.seen_payloads]
        assert seen and all(s == "DEADBEEF" for s in seen)
    finally:
        t.close()


def test_slow_provider_does_not_exceed_deadline_and_fast_accept_wins():
    block = threading.Event()
    slow = StubProvider("jito", AttemptState.ACCEPTED, block=block)
    fast = StubProvider("sender", AttemptState.ACCEPTED)
    t = SubmissionTransport([slow, fast], deadline_s=0.08)
    try:
        started = time.monotonic()
        res = t.submit(_req())
        elapsed = time.monotonic() - started
        assert res.state is AttemptState.ACCEPTED     # the fast provider carried it
        assert elapsed < 1.0                          # never waited for the 2s-blocked provider
    finally:
        block.set()
        t.close()


def test_prior_inflight_provider_is_not_requeued_for_later_transaction():
    block = threading.Event()
    slow = StubProvider("slow", AttemptState.ACCEPTED, block=block)
    fast = StubProvider("fast", AttemptState.ACCEPTED)
    t = SubmissionTransport([slow, fast], deadline_s=0.08)
    try:
        assert t.submit(_req(tx_identity="SIG1")).state is AttemptState.ACCEPTED
        second = t.submit(_req(tx_identity="SIG2"))
        assert second.state is AttemptState.ACCEPTED
        assert len(slow.seen_payloads) == 1
        busy = [a for a in second.attempts if a.provider_id == "slow"]
        assert busy and busy[0].state is AttemptState.NOT_SENT
        assert busy[0].reason_code == "provider_busy"
    finally:
        block.set()
        t.close()


def test_deadline_marks_in_flight_provider_ambiguous():
    block = threading.Event()
    slow = StubProvider("jito", AttemptState.ACCEPTED, block=block)
    reject = StubProvider("rpc", AttemptState.EXPLICIT_REJECTED)
    t = SubmissionTransport([slow, reject], deadline_s=0.05)
    try:
        res = t.submit(_req())
        # one rejected, one still in flight at the deadline -> ambiguous overall (not rejected)
        assert res.state is AttemptState.AMBIGUOUS
        deadline_attempts = [a for a in res.attempts if a.reason_code == "deadline"]
        assert deadline_attempts and deadline_attempts[0].state is AttemptState.AMBIGUOUS
    finally:
        block.set()
        t.close()


def test_provider_exception_is_ambiguous_not_rejected():
    class Boom:
        provider_id = "sender"

        def submit(self, request):
            raise RuntimeError("network died mid-write")
    t = SubmissionTransport([Boom()], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS    # a write that may have landed is never rejected
    finally:
        t.close()


def test_workers_receive_only_the_request_no_db_handle():
    prov = StubProvider("rpc", AttemptState.ACCEPTED)
    t = SubmissionTransport([prov], deadline_s=1.0)
    try:
        t.submit(_req())
        # the request type carries no connection/cursor — workers cannot touch SQLite
        assert prov.seen_types == ["SubmissionRequest"]
        assert not hasattr(SubmissionRequest, "conn")
        r = _req()
        assert not any(f in ("conn", "connection", "cursor") for f in r.__dataclass_fields__)
    finally:
        t.close()


def test_no_providers_is_ambiguous_never_accepted_or_rejected():
    t = SubmissionTransport([], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
    finally:
        t.close()


def test_all_not_sent_is_ambiguous_never_rejected():
    # every real provider proved no bytes crossed its boundary -> nobody attempted delivery AND nobody
    # proved a real rejection -> ambiguous. Never fabricate a safe rejection when nothing was sent.
    t = SubmissionTransport(
        [StubProvider("public", AttemptState.NOT_SENT, returned_identity=None),
         StubProvider("tpu", AttemptState.NOT_SENT, returned_identity=None)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        assert res.accepted_by is None
    finally:
        t.close()


def test_explicit_reject_plus_optional_not_sent_is_explicit_rejected():
    # a real provider intrinsically rejected the exact bytes; the optional one never sent -> rejected
    # is safe (the reservation/claim can release and re-sign).
    t = SubmissionTransport(
        [StubProvider("custody", AttemptState.EXPLICIT_REJECTED),
         StubProvider("public", AttemptState.NOT_SENT, returned_identity=None)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.EXPLICIT_REJECTED
    finally:
        t.close()


def test_accepted_beats_not_sent():
    t = SubmissionTransport(
        [StubProvider("public", AttemptState.NOT_SENT, returned_identity=None),
         StubProvider("custody", AttemptState.ACCEPTED)], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.ACCEPTED
        assert res.accepted_by == "custody"
    finally:
        t.close()


def test_provider_not_sent_attempt_propagates_with_reason():
    prov = StubProvider("public", AttemptState.NOT_SENT, returned_identity=None,
                        reason="quarantined")
    t = SubmissionTransport([prov], deadline_s=1.0)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS            # a lone NOT_SENT is neutral -> ambiguous
        assert res.attempts[0].state is AttemptState.NOT_SENT
        assert res.attempts[0].reason_code == "quarantined"
    finally:
        t.close()


def test_pending_cancelled_before_worker_start_is_not_sent():
    # More providers than worker slots: the surplus future never starts before the deadline. A future
    # that provably never ran is NOT_SENT (no bytes crossed its boundary); a running one is AMBIGUOUS.
    block = threading.Event()
    providers = [StubProvider("p%d" % i, AttemptState.ACCEPTED, block=block) for i in range(17)]
    t = SubmissionTransport(providers, deadline_s=0.05)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS           # nothing completed; nothing rejected
        not_sent = [a for a in res.attempts if a.state is AttemptState.NOT_SENT]
        in_flight = [a for a in res.attempts if a.reason_code == "deadline"]
        assert not_sent and not_sent[0].reason_code == "not_started"
        assert in_flight and in_flight[0].state is AttemptState.AMBIGUOUS
    finally:
        block.set()
        t.close()


# -- a loser of a first-accept race is not a deadline (Alpha LOW, 2026-10-02) ---------------------
#
# The loop stopped at the first ACCEPTED and stamped every still-running provider AMBIGUOUS with
# reason "deadline" and latency = the whole fanout deadline, although no deadline passed. On the 47
# slot-lane sends that read as custody_rpc and public_rpc timing out (41 and 43 of 47).


def test_a_provider_still_running_after_another_accepted_is_in_flight_not_a_deadline():
    block = threading.Event()
    slow = StubProvider("custody_rpc", AttemptState.ACCEPTED, block=block)
    fast = StubProvider("tpu", AttemptState.ACCEPTED)
    t = SubmissionTransport([slow, fast], deadline_s=5.0)
    try:
        started = time.monotonic()
        res = t.submit(_req())
        assert res.state is AttemptState.ACCEPTED and time.monotonic() - started < 1.0
        (loser,) = [a for a in res.attempts if a.provider_id == "custody_rpc"]
        assert loser.state is AttemptState.AMBIGUOUS              # its bytes may be on the wire
        assert loser.reason_code == "in_flight_after_accept" and loser.latency_ms is None
    finally:
        block.set()
        t.close()


def test_a_real_deadline_still_reads_deadline_with_the_fanout_latency():
    block = threading.Event()
    slow = StubProvider("custody_rpc", AttemptState.ACCEPTED, block=block)
    t = SubmissionTransport([slow], deadline_s=0.05)
    try:
        res = t.submit(_req())
        assert res.state is AttemptState.AMBIGUOUS
        (a,) = res.attempts
        assert a.reason_code == "deadline" and a.latency_ms == 50.0
    finally:
        block.set()
        t.close()


def test_late_provider_result_is_a_separate_observation_not_an_early_accept():
    started, release = threading.Event(), threading.Event()

    class LateProvider(StubProvider):
        send_after_accept = True

        def submit(self, request):
            started.set()
            assert release.wait(2)
            return super().submit(request)

    class EarlyProvider(StubProvider):
        def submit(self, request):
            assert started.wait(2)
            return super().submit(request)

    late = LateProvider('solana:nozomi', AttemptState.ACCEPTED)
    early = EarlyProvider('solana:tpu', AttemptState.ACCEPTED)
    transport = SubmissionTransport([late, early], deadline_s=1)
    try:
        result = transport.submit(_req())
        snapshot = next(a for a in result.attempts if a.provider_id == late.provider_id)
        assert snapshot.state is AttemptState.AMBIGUOUS
        assert snapshot.latency_ms is None
        assert len(result.late_results) == 1
        release.set()
        final = result.late_results[0].result(timeout=2)
        assert final.state is AttemptState.ACCEPTED
        assert snapshot.state is AttemptState.AMBIGUOUS
        assert late.seen_payloads == early.seen_payloads == ['AAAA']
    finally:
        release.set()
        transport.close()


def test_late_provider_audit_appends_without_using_the_caller_connection(tmp_path, capsys):
    import sqlite3
    from sniper.execution import submission_wiring as wiring
    from sniper.persistence import db_memecoin as m

    conn = sqlite3.connect(tmp_path / 'ledger.db')
    m.ensure_memecoin_tables(conn)
    started, release = threading.Event(), threading.Event()

    class LateProvider(StubProvider):
        send_after_accept = True

        def submit(self, request):
            started.set()
            assert release.wait(2)
            from dataclasses import replace
            return replace(super().submit(request), provider_timing={
                'post_started_monotonic': 10.0, 'post_finished_monotonic': 10.25,
                'clock': 'perf_counter'})

    class EarlyProvider(StubProvider):
        def submit(self, request):
            assert started.wait(2)
            from dataclasses import replace
            return replace(super().submit(request), provider_timing={
                "tpu_guard": dict(version=1, estimated_slot=0, minimum_slot=0,
                    available_leader_count=2, age_since_advance_ms=0.0,
                    clock_frozen=False, behind_minimum=False, secret="NEVER_LOG"),
                "request_min_context_slot": None})

    transport = SubmissionTransport([
        LateProvider('solana:nozomi', AttemptState.ACCEPTED),
        EarlyProvider('solana:tpu', AttemptState.ACCEPTED)], deadline_s=1)
    try:
        record = wiring._record_fn(conn, chain_id='solana', action='entry',
                                  owner_kind='reservation', owner_id='1', identity='SIG1',
                                  sha256=sha256_hex('AAAA'), now_fn=lambda: '2026-10-11T12:00:00+00:00',
                                  async_write=True)
        result = wiring.guarded_submit(transport, _req(), record_fn=record)
        assert wiring.flush_async_audit()
        conn.close()  # A late worker must never touch this thread-bound, now-closed handle.
        audited = threading.Event()
        result.late_results[0].add_done_callback(lambda _: audited.set())
        release.set()
        result.late_results[0].result(timeout=2)
        assert audited.wait(2)
        assert wiring.flush_async_audit()
        with sqlite3.connect(tmp_path / 'ledger.db') as reader:
            rows = reader.execute("SELECT state,reason_code FROM memecoin_submission_attempts "
                                  "WHERE provider_id='solana:nozomi' ORDER BY id").fetchall()
        assert rows == [('ambiguous', 'after_accept'), ('accepted', None)]
        evidence = capsys.readouterr().out
        assert 'submission TPU guard' in evidence
        guard_line = next(line for line in evidence.splitlines() if 'submission TPU guard' in line)
        assert 'post_started_monotonic' not in guard_line
        assert 'NEVER_LOG' not in evidence
        assert 'submission provider final' in evidence
        assert '"post_started_monotonic": 10.0' in evidence
        assert '"post_finished_monotonic": 10.25' in evidence
        assert 'SIG1' in evidence and sha256_hex('AAAA') in evidence
    finally:
        release.set()
        transport.close()


@pytest.mark.parametrize("minimum", [None, 0, 2**64-1])
def test_tpu_guard_sanitizes_without_http_timing(minimum):
    from sniper.execution.submission_wiring import _sanitize_tpu_timing
    guard = dict(version=1, estimated_slot=0, minimum_slot=0, available_leader_count=2,
                 age_since_advance_ms=0.0, clock_frozen=False, behind_minimum=True,
                 secret="NEVER_LOG")
    value = _sanitize_tpu_timing(dict(tpu_guard=guard, request_min_context_slot=minimum))
    assert value["request_min_context_slot"] == minimum
    assert "secret" not in value["tpu_guard"]


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("estimated_slot", True),
    ("minimum_slot", -1), ("available_leader_count", 2**64),
    ("age_since_advance_ms", float("nan")), ("age_since_advance_ms", float("inf")),
    ("age_since_advance_ms", True), ("age_since_advance_ms", 10**1000), ("clock_frozen", 0), ("behind_minimum", None)])
def test_tpu_guard_drops_invalid_fields(field, value):
    from sniper.execution.submission_wiring import _sanitize_tpu_timing
    guard = dict(version=1, estimated_slot=0, minimum_slot=0, available_leader_count=2,
                 age_since_advance_ms=0.0, clock_frozen=False, behind_minimum=True)
    guard[field] = value
    assert _sanitize_tpu_timing(dict(tpu_guard=guard, request_min_context_slot=None)) is None
    del guard[field]
    assert _sanitize_tpu_timing(dict(tpu_guard=guard, request_min_context_slot=None)) is None


@pytest.mark.parametrize("minimum", [True, -1, 2**64, "0"])
def test_tpu_guard_invalid_request_minimum(minimum):
    from sniper.execution.submission_wiring import _sanitize_tpu_timing
    guard = dict(version=1, estimated_slot=0, minimum_slot=0, available_leader_count=2,
                 age_since_advance_ms=0.0, clock_frozen=False, behind_minimum=False)
    assert _sanitize_tpu_timing(dict(tpu_guard=guard, request_min_context_slot=minimum)) is None


def test_tpu_old_peer_is_unknown():
    from sniper.execution.submission_wiring import _sanitize_tpu_timing
    assert _sanitize_tpu_timing(None) is None
    assert _sanitize_tpu_timing({}) is None



def test_sanitizer_missing_request_minimum_stays_unknown():
    from sniper.execution.submission_wiring import _sanitize_tpu_timing
    guard = dict(version=1, estimated_slot=0, minimum_slot=0, available_leader_count=2,
                 age_since_advance_ms=0.0, clock_frozen=False, behind_minimum=False)
    assert _sanitize_tpu_timing(dict(tpu_guard=guard)) is None
