"""Real slot wrapper: signed fee evidence, deferred receipts, and worker-owned traces."""
import base64
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.message import MessageAddressTableLookup, MessageV0
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.execution import solana_real_seams as seams, solana_sender_ab as tips
from sniper.execution.solana_wallet import USDC_MINT
from sniper.execution.solana_slot_guard import GUARD_COMPUTE_UNITS
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionTransport
from sniper.execution.submission_wiring import flush_async_audit
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from sniper.services.memecoin_slot_lane import NATIVE_ATA_RENT_RESERVE_LAMPORTS
from test_memecoin_slot_lane_sender_ab import _SenderHttp, _msg, _tip_ixs
from test_memecoin_slot_lane_nozomi_ab import KEY, _noz_ixs, _provider

NOW = '2026-10-09T20:00:00+00:00'
ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
LIMIT, PRICE = 400_001, 1_234_567
GUARDED_LIMIT = LIMIT + GUARD_COMPUTE_UNITS
GUARDED_PRICE = PRICE * LIMIT // GUARDED_LIMIT
NETWORK = 5_000 + (GUARDED_LIMIT * GUARDED_PRICE + 999_999) // 1_000_000
SOURCE_SLOT = 100


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'deferred.db'

    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    conn = connect()
    m.ensure_memecoin_tables(conn)
    conn.commit()
    yield conn, connect
    assert flush_async_audit(timeout_s=2)
    conn.close()


def _ata(kp, *, data=b'\x01'):
    return Instruction(Pubkey.from_string(ATA), data, [
        AccountMeta(kp.pubkey(), True, True), AccountMeta(Pubkey.new_unique(), False, True),
        AccountMeta(kp.pubkey(), False, False), AccountMeta(Pubkey.new_unique(), False, False),
        AccountMeta(Pubkey.default(), False, False),
        AccountMeta(Pubkey.from_string(TOKEN), False, False)])


def _budget(*, with_price=True):
    return [set_compute_unit_limit(LIMIT)] + ([set_compute_unit_price(PRICE)] if with_price else [])


def _reserve(conn, *, native=False, attempt_id=None, overhead=0, held_tip=None, signal_source='slot_lane'):
    return m.reserve_pending(
        conn, mint='coin', source_user_id='wallet', strategy='alpha_b', notional=50.,
        reserved_at=NOW, network_id='solana', origin_event_id='source-A',
        signal_source=signal_source, source_price=.01, attempt_id=attempt_id,
        **({'funding_asset': 'sol', 'funding_amount_atomic': 500_000_000,
            'funding_sol_usd': 100., 'funding_overhead_atomic': overhead} if native else
           {'funding_asset': 'usdc', 'funding_overhead_atomic': held_tip} if held_tip is not None else {}))


def _executor(monkeypatch, *, kp, confirm, receipts=None, provider=None):
    sent, reads, balances = [], [], []

    class Tpu:
        provider_id = 'solana:tpu'

        def submit(self, request):
            sent.append(request)
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                     request.tx_identity, 0., '200', None)

    def http(method, url, headers=None, body=None, timeout=None):
        payload = json.loads(body)
        assert payload['method'] == 'getTransaction', 'entry added an unexpected RPC'
        reads.append((payload, threading.get_ident()))
        assert receipts is not None
        return 200, {'result': receipts(payload['params'][0])}, None

    def balance():
        balances.append(True)
        pytest.fail('prebuilt entry must not read wallet balances')

    monkeypatch.setattr(seams, 'rpc_confirm', confirm)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'memecoin_slot_lane_resend_offsets_ms', lambda: ())
    transport = SubmissionTransport([Tpu()] + ([provider] if provider else []), deadline_s=1.)
    executor = seams.build_real_solana_executor(
        keypair=kp, rpc_url='http://offline.invalid', http=http, usdc_fn=balance,
        submission_transport=transport, now_fn=lambda: NOW)
    return executor['slot_execute_fn'], sent, reads, balances, transport


def _invoke(conn, execute, kp, ixs, *, native=False, tip=0, nozomi=0, trace=None, overhead=0,
            held_tip=None):
    pid = _reserve(conn, native=native, attempt_id=trace.attempt_id if trace else None,
                   overhead=overhead, held_tip=held_tip)
    validity = {'sender_tip_lamports': tip, 'nozomi_tip_lamports': nozomi}
    if native:
        validity.update(funding_asset='sol', funding_amount_atomic=500_000_000)
    result = execute(conn, m.pending_row(conn, pid), _msg(kp, ixs), validity=validity,
                     now_iso=NOW, trace=trace, sol_usd=100., defer_confirmation=True,
                     entry_min_slot=SOURCE_SLOT, entry_max_slot=SOURCE_SLOT + 1,
                     entry_head_fn=lambda: (SOURCE_SLOT, time.monotonic()))
    return result, pid


def test_actual_wrapper_returns_before_confirmation_and_holds_signed_network_ceiling(
        ledger, monkeypatch):
    conn, connect = ledger
    kp, confirmed = Keypair(), []
    execute, sent, reads, balances, transport = _executor(
        monkeypatch, kp=kp, confirm=lambda sig, *_a, **_k: confirmed.append(sig) or 'failed')
    try:
        result, pid = _invoke(conn, execute, kp, _budget())
        assert result['status'] == 'submitted' and result['_gas_debit_lamports'] == NETWORK
        assert not confirmed and not reads and not balances
        request, = sent
        signed = VersionedTransaction.from_bytes(base64.b64decode(request.signed_payload))
        assert [int.from_bytes(bytes(ix.data)[3:11], 'little')
                for ix in signed.message.instructions[-2:]] == [SOURCE_SLOT, SOURCE_SLOT + 1]
        row = m.pending_row(conn, pid)
        assert row['tx_sig'] == str(signed.signatures[0]) == result['sig']
        assert row['signed_payload_b64'] == request.signed_payload
        with connect() as worker:
            assert result['_settlement'].resolve(worker)['status'] == 'failed'
        assert confirmed == [result['sig']] and len(sent) == 1
    finally:
        transport.close()


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('kind,tip', [('sender', 5_000), ('nozomi', 5_000_000)])
def test_signed_ata_rent_and_exact_tip_are_counted_once(ledger, monkeypatch, native, kind, tip):
    conn, _ = ledger
    kp, calls, provider_http = Keypair(), [], _SenderHttp()
    provider = tips.SenderProvider(provider_http, timeout_s=1.) if kind == 'sender' else _provider(provider_http)
    execute, sent, reads, balances, transport = _executor(
        monkeypatch, kp=kp, provider=provider,
        confirm=lambda sig, *_a, **_k: calls.append(sig) or 'failed')
    transfer = _tip_ixs(kp, tip) if kind == 'sender' else _noz_ixs(kp, tip)
    overhead = 2 * NATIVE_ATA_RENT_RESERVE_LAMPORTS + tip
    try:
        result, pid = _invoke(
            conn, execute, kp, _budget() + [_ata(kp, data=b''), _ata(kp)] + transfer, native=native,
            tip=tip if kind == 'sender' else 0, nozomi=tip if kind == 'nozomi' else 0,
            overhead=overhead)
        assert result['status'] == 'submitted'
        assert result['_gas_debit_lamports'] == NETWORK + (0 if native else overhead)
        assert not calls and not reads and not balances
        if native:
            assert int(m.pending_row(conn, pid)['funding_overhead_atomic']) == overhead
        result['_settlement'].resolve(conn)
        assert len(sent) == 1
    finally:
        transport.close()


@pytest.mark.parametrize('shape', ['missing_price', 'duplicate_price', 'loaded_program'])
def test_unprovable_signed_priority_fee_settles_serially_without_resigning_or_resending(
        ledger, monkeypatch, shape):
    conn, _ = ledger
    kp, calls = Keypair(), []
    execute, sent, reads, _, transport = _executor(
        monkeypatch, kp=kp, confirm=lambda sig, *_a, **_k: calls.append(sig) or 'failed')
    ixs = _budget(with_price=shape != 'missing_price')
    if shape == 'duplicate_price':
        ixs.append(set_compute_unit_price(PRICE))
    message = _msg(kp, ixs)
    if shape == 'loaded_program':
        # Unresolved program identity must fail closed, even if the compute-budget pair exists.
        message = MessageV0(message.header, message.account_keys, message.recent_blockhash,
                            [*message.instructions, CompiledInstruction(len(message.account_keys), b'', b'')],
                            [MessageAddressTableLookup(Pubkey.new_unique(), b'\x00', b'')])
    # Manual unguarded entries still settle unprovable fee evidence serially.
    pid = _reserve(conn, signal_source='manual')
    try:
        result = execute(conn, m.pending_row(conn, pid), message, now_iso=NOW, defer_confirmation=True)
        if shape != 'missing_price':
            # Option B (2026-10-22): the unconditional entry fee clamp cannot read this compute budget, so the
            # entry is refused BEFORE signing: nothing signed, nothing sent, no fee to prove.
            assert result['status'] == 'aborted' and not sent and not calls
            assert m.pending_row(conn, pid)['tx_sig'] is None
            return
        assert result['status'] == 'failed' and '_settlement' not in result
        assert calls == [result['sig']] and not reads
        request, = sent
        assert request.tx_identity == result['sig'] == m.pending_row(conn, pid)['tx_sig']
        assert request.signed_payload == m.pending_row(conn, pid)['signed_payload_b64']
    finally:
        transport.close()


def test_worker_owns_trace_connection_and_records_cu_and_private_provider_outcome_after_fill(
        ledger, monkeypatch):
    conn, connect = ledger
    kp, calls, provider_http = Keypair(), [], _SenderHttp()
    owner = str(kp.pubkey())

    def receipt(sig):
        def token(index, mint, amount):
            return {'accountIndex': index, 'mint': mint, 'owner': owner,
                    'uiTokenAmount': {'amount': str(amount), 'decimals': 6}}
        return {'transaction': {'signatures': [sig], 'message': {'accountKeys': [
                    {'pubkey': owner, 'signer': True}, {'pubkey': 'token'}, {'pubkey': 'usdc'}]}},
                'meta': {'err': None, 'fee': NETWORK, 'computeUnitsConsumed': 123_456,
                         'preBalances': [2_000_000_000, 2_039_280, 2_039_280],
                         'postBalances': [2_000_000_000 - NETWORK - 5_000_000, 2_039_280, 2_039_280],
                         'preTokenBalances': [token(1, 'coin', 0), token(2, USDC_MINT, 100_000_000)],
                         'postTokenBalances': [token(1, 'coin', 2_000_000_000), token(2, USDC_MINT, 50_000_000)]}}

    def confirm(sig, *_a, **_k):
        calls.append((sig, threading.get_ident()))
        # The offline paid-provider worker may finish after the free accept.
        deadline = time.monotonic() + 1.
        while not provider_http.calls and time.monotonic() < deadline:
            time.sleep(.001)
        assert provider_http.calls
        return 'confirmed'

    trace = EntryAttemptTrace.start(conn, SimpleNamespace(
        network_id='solana', mint='coin', source_user_id='wallet', signal_source='slot_lane',
        observed_at=NOW, clock_domain='observed_real_utc'), NOW, time)
    execute, sent, reads, _, transport = _executor(
        monkeypatch, kp=kp, confirm=confirm, receipts=receipt, provider=_provider(provider_http))
    result, _pid = _invoke(conn, execute, kp, _budget() + _noz_ixs(kp, 5_000_000),
                           nozomi=5_000_000, trace=trace)
    assert result['status'] == 'submitted' and not calls and not reads
    assert not any(e['name'] in ('receipt_cu', 'nozomi_ab_send') for e in trace._substage_events)
    completed, errors = [], []

    def work():
        try:
            with connect() as worker:
                settled = result['_settlement'].resolve(worker)
                assert trace.conn is worker
                assert trace.finish(settled['status'], 'test_outcome')
                completed.append((settled, threading.get_ident()))
        except BaseException as exc:
            errors.append(exc)

    try:
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive() and not errors
        settled, worker_id = completed[0]
        assert settled['status'] == 'filled' and settled['notional'] == 50.
        assert calls == [(result['sig'], worker_id)]
        assert len(reads) == 1 and reads[0][1] == worker_id
        stored = conn.execute('SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?',
                              (trace.attempt_id,)).fetchone()[0]
        stages = {e['name']: e for e in json.loads(stored)['substage_events']}
        assert stages['receipt_cu']['consumed'] == 123_456
        assert stages['nozomi_ab_send']['nozomi_payload_sha_match'] is True
        assert stages['nozomi_ab_send']['nozomi_sig_match'] is True
        assert stages['nozomi_ab_send']['nozomi_state'] == 'accepted'
        assert KEY not in stored and 'temporal.xyz' not in stored
        assert len(sent) == 1
    finally:
        transport.close()


@pytest.mark.parametrize('native', [False, True])
def test_frozen_nozomi_liability_and_snapshot_delta_count_tip_once(ledger, monkeypatch, native):
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    from types import SimpleNamespace
    conn, _ = ledger
    kp, tip = Keypair(), 5_000_000
    overhead = 2 * NATIVE_ATA_RENT_RESERVE_LAMPORTS + tip
    execute, sent, reads, balances, transport = _executor(
        monkeypatch, kp=kp, provider=_provider(_SenderHttp()), confirm=lambda *_a, **_k: 'failed')
    try:
        result, pid = _invoke(conn, execute, kp,
            _budget() + [_ata(kp, data=b''), _ata(kp)] + _noz_ixs(kp, tip), native=native,
            nozomi=tip, overhead=overhead, held_tip=None if native else tip)
        assert result['_gas_debit_lamports'] == NETWORK + (0 if native else overhead-tip)
        pool = SlotConfirmations(conn_factory=lambda: None, put=lambda _: None,
                                 clock=SimpleNamespace(monotonic=lambda: 100.), start_worker=lambda _: None)
        pool.register(result)
        evidence = pool.snapshot_funding(100.)
        raw = m.pending_funding_totals(conn, 'solana', snapshot_debits=evidence['pending_ids'])[1]
        expected = NETWORK + overhead + (500_000_000 if native else 0)
        assert raw + evidence['extra_sol_atomic'] == expected
        result['_settlement'].resolve(conn)
        pool.completed(pid, 101., status='failed')
        # A balance captured before settlement retains exactly the same spend envelope.
        old = pool.snapshot_funding(100.)
        assert m.pending_funding_totals(conn, 'solana', snapshot_debits=old['pending_ids'])[1] + old['extra_sol_atomic'] == expected
        assert pool.snapshot_funding(101.) == {'pending_ids': (), 'extra_sol_atomic': 0}
        assert not reads and not balances and len(sent) == 1
    finally:
        transport.close()
