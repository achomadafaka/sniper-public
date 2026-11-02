"""Automatic copies have no slot deadline; explicit legacy bounds remain atomic."""
import base64
import time

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from sniper.execution import solana_real_seams as S, solana_slot_executor as slot, solana_swap_executor as swap
from sniper.persistence import db_memecoin as m
from test_memecoin_entry_slot_seams import ledger, factory, NOW

PROGRAM = 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95'


def test_heartbeat_preserves_the_verified_onchain_deadline_basis(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'heartbeat.json'
    assert write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={
        'entry_opportunity': {'landing_deadline_guaranteed': True,
                              'deadline_basis': 'onchain_clock_slot'}})
    policy = json.loads(path.read_text())['slot_lane']['entry_opportunity']
    assert policy['landing_deadline_guaranteed'] is True
    assert policy['deadline_basis'] == 'onchain_clock_slot'


def pending(conn, source='onchain_watch', *, native=False):
    pid = m.reserve_pending(conn, mint='coin', source_user_id='wallet', strategy='alpha_b',
        notional=10., reserved_at=NOW, network_id='solana', origin_event_id='onchain:source',
        signal_source=source, **({'funding_asset': 'sol', 'funding_amount_atomic': 10_000_000,
                                'funding_sol_usd': 100.} if native else {}))
    return m.pending_row(conn, pid)


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.usefixtures("fake_payload_clamp")
def test_explicit_legacy_bounds_include_both_atomic_clock_assertions(ledger, monkeypatch, lane):
    signed = []
    def inspect(_conn, **kw):
        signed.append(kw['sign_fn']({})[0] if lane == 'ordinary' else kw['signed_b64'])
        return {'status': 'unknown'}
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', inspect)
    ex, kp, http = factory(monkeypatch)
    message = MessageV0.try_compile(kp.pubkey(),
        [set_compute_unit_limit(400_000), set_compute_unit_price(1_000)], [], Hash.default())
    wire = base64.b64encode(bytes(VersionedTransaction(message, [kp]))).decode()
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *_a, **_kw: wire)
    row = pending(ledger)
    bounds = dict(entry_min_slot=100, entry_max_slot=101,
                  entry_head_fn=lambda: (100, time.monotonic()))
    if lane == 'ordinary':
        ex['execute_fn'](ledger, row['id'], 'coin', 10., NOW, **bounds)
    else:
        ex['slot_execute_fn'](ledger, row, message, now_iso=NOW, **bounds)
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed[0]))
    tx.verify_and_hash_message()
    guards = tx.message.instructions[-2:]
    assert [str(tx.message.account_keys[ix.program_id_index]) for ix in guards] == [PROGRAM, PROGRAM]
    assert [bytes(ix.data) for ix in guards] == [
        b'\x0f\x00\x00' + (100).to_bytes(8, 'little') + b'\x04',
        b'\x0f\x00\x00' + (101).to_bytes(8, 'little') + b'\x05']
    assert not http


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.parametrize('source', ['onchain_watch', None])
@pytest.mark.parametrize('bounds,reason', [
    ({'entry_max_slot': 101}, 'source_buy_slot_unknown'),
    ({'entry_min_slot': 100}, 'entry_slot_deadline_invalid'),
    ({'entry_min_slot': 100, 'entry_max_slot': 102}, 'entry_slot_deadline_invalid'),
])
def test_explicit_partial_or_widened_bounds_never_reach_execution(
        ledger, monkeypatch, lane, source, bounds, reason):
    def forbidden(*_a, **_kw):
        pytest.fail('unbounded automatic copy reached execution')
    monkeypatch.setattr(swap, 'execute_entry', forbidden)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', forbidden)
    ex, kp, http = factory(monkeypatch, fast_builder=forbidden)
    row = pending(ledger, source)
    if bounds:
        bounds = dict(bounds, entry_head_fn=lambda: (100, time.monotonic()))
    if lane == 'ordinary':
        result = ex['execute_fn'](ledger, row['id'], 'coin', 10., NOW, **bounds)
    else:
        message = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
        result = ex['slot_execute_fn'](ledger, row, message, now_iso=NOW, **bounds)
    assert result['status'] == 'aborted' and result['reason'] == reason
    assert m.pending_row(ledger, row['id'])['status'] == 'failed'
    assert not m.pending_row(ledger, row['id'])['tx_sig'] and not http


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.parametrize('source', ['onchain_watch', None])
@pytest.mark.parametrize('freshness', [False, True])
@pytest.mark.usefixtures("fake_payload_clamp")
def test_automatic_copies_sign_and_bind_original_instructions_without_slot_bounds(
        ledger, monkeypatch, lane, source, freshness):
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    from sniper.execution.submission_wiring import flush_async_audit

    sent = []
    def inspect(_conn, **kw):
        payload, _sig, *validity = kw['sign_fn']({}) if lane == 'ordinary' else (
            kw['signed_b64'], kw['sig'])
        assert kw['send_fn'](payload, **({'validity': validity[0]} if validity else {}))
        sent.append(kw['send_fn'].last_request)
        return {'status': 'unknown'}
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', inspect)
    ex, kp, http = factory(monkeypatch)
    message = MessageV0.try_compile(kp.pubkey(), [
        set_compute_unit_limit(400_000), set_compute_unit_price(1_000),
        Instruction(Pubkey.new_unique(), b'min_out' + (261_880_438_953).to_bytes(8, 'little'), [])
    ], [], Hash.default())
    wire = base64.b64encode(bytes(VersionedTransaction(message, [kp]))).decode()
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *_a, **_kw: {
        'swapTransaction': wire, 'lastValidBlockHeight': 777})
    row = pending(ledger, source, native=lane == 'slot')
    head = (lambda: (200, time.monotonic())) if freshness else None
    if lane == 'ordinary':
        result = ex['execute_fn'](ledger, row['id'], 'coin', 10., NOW, entry_head_fn=head)
    else:
        result = ex['slot_execute_fn'](ledger, row, message, now_iso=NOW,
            entry_head_fn=head,
            boundary_fn=lambda: {'source_slot': 100, 'head_slot': 200})
    assert result['status'] == 'unknown' and len(sent) == 1
    request = sent[0]
    assert request.entry_max_slot is None and request.entry_head_fn is head
    tx = VersionedTransaction.from_bytes(base64.b64decode(request.signed_payload))
    tx.verify_and_hash_message()
    assert bytes(tx.message) == bytes(message)
    assert PROGRAM not in [str(key) for key in tx.message.account_keys]
    bound = m.entry_submission_binding(ledger, row['id'])
    assert bound['signed_payload_b64'] == request.signed_payload
    assert bound['tx_sig'] == str(tx.signatures[0]) == request.tx_identity
    if lane == 'ordinary':
        assert bound['submission_last_valid_height'] == 777
    assert not http
    assert flush_async_audit(timeout_s=2)


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401
