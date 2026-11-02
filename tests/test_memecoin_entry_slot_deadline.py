"""Head freshness and optional legacy deadlines never release signed ownership."""
from dataclasses import FrozenInstanceError, replace
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.execution import submission_transport as T, submission_wiring as W
from sniper.execution.solana_slot_resend import SlotResender
from sniper.persistence import db_memecoin as m
from test_memecoin_fast_submit_wiring import _conn, _reserve, _signed_tx, StubTransport


@pytest.mark.parametrize('maximum,head,stamp,reason', [
    (102, 100, 100., None), (102, 101, 100., None), (102, 102, 99.5, None),
    (102, 103, 100., 'entry_slot_expired'),
    (102, 102, 99.499, 'entry_slot_head_stale'),
    (102, 102, 100.001, 'entry_slot_head_unknown'),
    (102, None, 100., 'entry_slot_head_unknown'),
    (102, True, 100., 'entry_slot_head_unknown'),
    (102, 102., 100., 'entry_slot_head_unknown'),
    (102, 102, None, 'entry_slot_head_unknown'),
    (102, 102, True, 'entry_slot_head_unknown'),
    (102, 102, float('nan'), 'entry_slot_head_unknown'),
    (102, 102, float('inf'), 'entry_slot_head_unknown'),
    (None, 202, 100., None),
    (None, 202, 99.5, None),
    (None, 202, 99.499, 'entry_slot_head_stale'),
    (None, None, 100., 'entry_slot_head_unknown'),
    (None, True, 100., 'entry_slot_head_unknown'),
    (None, 202, None, 'entry_slot_head_unknown'),
    (None, 202, float('nan'), 'entry_slot_head_unknown'),
    (True, 102, 100., 'entry_slot_deadline_invalid'),
    (0, 102, 100., 'entry_slot_deadline_invalid'),
    (102., 102, 100., 'entry_slot_deadline_invalid'),
])
def test_observed_head_deadline_is_inclusive_and_fails_closed(maximum, head, stamp, reason):
    assert T.entry_slot_refusal(maximum, lambda: (head, stamp), clock=lambda: 100.) == reason


def test_absent_policy_is_unchanged_and_an_unreadable_head_cannot_pass():
    assert T.entry_slot_refusal(None, None) is None
    assert T.entry_slot_refusal(102, None) == 'entry_slot_head_unknown'
    assert T.entry_slot_refusal(102, lambda: (_ for _ in ()).throw(OSError())) == 'entry_slot_head_unknown'
    assert T.entry_slot_refusal(None, lambda: (_ for _ in ()).throw(OSError())) == 'entry_slot_head_unknown'


def _request(head, *, maximum=102, **kwargs):
    return T.build_request(chain_id='solana', action='entry', tx_identity='SIG', signed_payload='AAEC',
                           entry_max_slot=maximum, entry_head_fn=lambda: (head[0], time.monotonic()),
                           **kwargs)


def test_request_freezes_the_original_cap_but_reads_the_live_head():
    head = [100]
    request = _request(head)
    assert request.entry_max_slot == 102
    head[0] = 103
    assert T.entry_slot_refusal(request.entry_max_slot, request.entry_head_fn) == 'entry_slot_expired'
    with pytest.raises(FrozenInstanceError):
        request.entry_max_slot = 105


class Provider:
    provider_id = 'solana:tpu'

    def __init__(self):
        self.calls = []

    def submit(self, request):
        self.calls.append(request)
        return T.SubmissionAttempt(self.provider_id, T.AttemptState.ACCEPTED, request.tx_identity, 0., 'ok')


def test_expired_provider_and_single_provider_resend_send_no_bytes():
    provider = Provider()
    transport = T.SubmissionTransport([provider], deadline_s=1.)
    try:
        request = _request([103])
        result = transport.submit(request)
        assert result.state is T.AttemptState.AMBIGUOUS
        assert result.attempts[0].state is T.AttemptState.NOT_SENT
        assert result.attempts[0].reason_code == 'entry_slot_expired'
        assert transport.submit_one(provider.provider_id, request).state is T.AttemptState.NOT_SENT
        assert not provider.calls
    finally:
        transport.close()


@pytest.mark.parametrize('fresh', [True, False])
def test_head_only_provider_and_resender_allow_any_fresh_slot_but_refuse_stale(fresh):
    provider, stamp = Provider(), time.monotonic() - (0. if fresh else 1.)
    request = T.build_request(chain_id='solana', action='entry', tx_identity='SIG',
        signed_payload='AAEC', entry_head_fn=lambda: (200, stamp))
    transport = T.SubmissionTransport([provider], deadline_s=1.)
    try:
        result = transport.submit(request)
        resender = SlotResender(lambda req: transport.submit_one(provider.provider_id, req),
            lambda: request, expected_sha256=request.signed_payload_sha256, offsets_ms=(0,),
            source_slot=100, dispatch_slot=102, head_fn=lambda: 200)
        resender._run(time.monotonic())
        if fresh:
            assert result.state is T.AttemptState.ACCEPTED
            assert provider.calls == [request, request]
            assert resender.report()['sent'] == 1
        else:
            assert result.state is T.AttemptState.AMBIGUOUS and not provider.calls
            assert result.attempts[0].reason_code == 'entry_slot_head_stale'
            assert resender.report()['sends'][0]['reason'] == 'entry_slot_head_stale'
    finally:
        transport.close()


def test_head_only_staleness_during_identity_binding_keeps_signed_ownership(tmp_path, monkeypatch):
    conn, stamp = _conn(tmp_path), [time.monotonic()]
    transport = StubTransport(T.AttemptState.ACCEPTED)
    pid, tx, bind = _reserve(conn), _signed_tx(), m.bind_entry_submission
    def bind_then_stale(*args, **kwargs):
        result = bind(*args, **kwargs)
        stamp[0] -= 1.
        return result
    monkeypatch.setattr(m, 'bind_entry_submission', bind_then_stale)
    try:
        send = W.make_solana_send_fn(transport, conn=conn, action='entry',
            owner_kind='reservation', owner_id=pid, persist=True,
            entry_head_fn=lambda: (200, stamp[0]))
        assert send(tx.signed_b64) is None and transport.calls == 0
        row = m.pending_row(conn, pid)
        assert row['status'] == 'reserved' and row['tx_sig'] == tx.signature
        assert row['signed_payload_b64'] == tx.signed_b64
        assert send.last_request.entry_max_slot is None
    finally:
        conn.close()


def test_a_queued_provider_checks_the_head_when_its_worker_runs():
    provider, head = Provider(), [102]
    transport = T.SubmissionTransport([provider], deadline_s=1.)
    entered, release = threading.Event(), threading.Event()
    run = transport._run_one_with_lease

    def delayed(*args):
        entered.set()
        assert release.wait(1.)
        return run(*args)

    transport._run_one_with_lease = delayed
    results = []
    request = _request(head)
    caller = threading.Thread(target=lambda: results.append(transport.submit(request)))
    caller.start()
    try:
        assert entered.wait(1.)
        head[0] = 103
        release.set()
        caller.join(1.)
        assert not caller.is_alive()
        assert not provider.calls
        assert results[0].attempts[0].reason_code == 'entry_slot_expired'
    finally:
        release.set()
        caller.join(1.)
        transport.close()


@pytest.mark.parametrize('phase', ['before_bind', 'after_bind', 'after_observation'])
def test_expiry_around_identity_binding_stays_ambiguous_and_never_dispatches(phase):
    head, events = [103 if phase == 'before_bind' else 102], []

    def bind(request):
        events.append('bound')
        if phase == 'after_bind':
            head[0] = 103
        return True

    def observe(event):
        if phase == 'after_observation':
            head[0] = 103

    transport = SimpleNamespace(submit=lambda req: events.append('sent'))
    result = W.guarded_submit(transport, _request(head), persist_fn=bind, observe_fn=observe)
    assert result.state is T.AttemptState.AMBIGUOUS
    assert events == ([] if phase == 'before_bind' else ['bound'])


def test_adapter_propagates_cap_and_keeps_an_existing_signed_owner(tmp_path):
    conn, head = _conn(tmp_path), [102]
    transport = StubTransport(T.AttemptState.ACCEPTED)
    pid, tx = _reserve(conn), _signed_tx()
    try:
        send = W.make_solana_send_fn(transport, conn=conn, action='entry', owner_kind='reservation',
            owner_id=pid, persist=True, entry_max_slot=102,
            entry_head_fn=lambda: (head[0], time.monotonic()))
        assert send(tx.signed_b64) == tx.signature
        before = m.entry_submission_binding(conn, pid)
        head[0] = 103
        assert send(tx.signed_b64) is None
        assert transport.calls == 1
        assert m.entry_submission_binding(conn, pid) == before
        assert m.pending_row(conn, pid)['status'] == 'reserved'
        assert send.last_request.entry_max_slot == 102
    finally:
        conn.close()


def test_expiry_during_real_identity_commit_keeps_the_bound_reservation(tmp_path, monkeypatch):
    conn, head = _conn(tmp_path), [102]
    transport = StubTransport(T.AttemptState.ACCEPTED)
    pid, tx = _reserve(conn), _signed_tx()
    bind = m.bind_entry_submission

    def bind_then_expire(*args, **kwargs):
        bound = bind(*args, **kwargs)
        head[0] = 103
        return bound

    monkeypatch.setattr(m, 'bind_entry_submission', bind_then_expire)
    try:
        send = W.make_solana_send_fn(transport, conn=conn, action='entry', owner_kind='reservation',
            owner_id=pid, persist=True, entry_max_slot=102,
            entry_head_fn=lambda: (head[0], time.monotonic()))
        assert send(tx.signed_b64) is None
        assert transport.calls == 0
        row = m.pending_row(conn, pid)
        assert row['status'] == 'reserved' and row['tx_sig'] == tx.signature
        assert row['signed_payload_b64'] == tx.signed_b64
    finally:
        conn.close()


@pytest.mark.parametrize('changes', [dict(chain_id='8453'), dict(action='exit')])
def test_deadline_is_scoped_to_solana_entries(changes):
    provider = Provider()
    transport = T.SubmissionTransport([provider], deadline_s=1.)
    try:
        request = replace(_request([103]), **changes)
        assert transport.submit(request).state is T.AttemptState.ACCEPTED
        assert provider.calls == [request]
    finally:
        transport.close()


@pytest.mark.parametrize('head_slot,sends', [(102, 1), (103, 0), (None, 0)])
def test_guarded_resender_never_rebases_to_a_late_dispatch(head_slot, sends):
    head, calls = [head_slot], []
    request = _request(head)
    resender = SlotResender(lambda req: calls.append(req) or T.SubmissionAttempt(
        'solana:tpu', T.AttemptState.ACCEPTED, req.tx_identity, 0., 'ok'), lambda: request,
        expected_sha256=request.signed_payload_sha256, offsets_ms=(0,), source_slot=100,
        dispatch_slot=102, head_fn=lambda: head[0])
    resender._run(time.monotonic())
    assert len(calls) == sends
    assert resender.report()['window_start_slot'] == 100
    if not sends:
        assert resender.report()['sends'][0]['state'] == 'window_ended'
