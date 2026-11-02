"""The funded signing seams carry one source deadline through to provider submission."""
import sqlite3
import time

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0

from sniper.execution import solana_real_seams as S, solana_swap_executor as swap
from sniper.execution import solana_slot_executor as slot
from sniper.execution.submission_transport import AttemptState
from sniper.execution.submission_wiring import flush_async_audit
from sniper.execution.solana_wallet import USDC_MINT
from sniper.persistence import db_memecoin as m
from test_memecoin_fast_submit_wiring import StubTransport, _signed_tx

NOW = '2026-10-18T20:00:00+00:00'


@pytest.fixture
def ledger(tmp_path):
    conn = sqlite3.connect(tmp_path / 'slot-seams.db')
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def reserve(conn):
    return m.reserve_pending(conn, mint='coin', source_user_id='wallet', strategy='alpha_b',
        notional=10., reserved_at=NOW, network_id='solana', origin_event_id='manual:source',
        signal_source='manual')


def factory(monkeypatch, *, transport=True, fast_builder=None):
    kp, calls = Keypair(), []

    def no_http(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail('a blocked entry made an HTTP request')

    ex = S.build_real_solana_executor(keypair=kp, rpc_url='http://offline.invalid', http=no_http,
        submission_transport=StubTransport(AttemptState.ACCEPTED) if transport else None,
        fast_tx_builder=fast_builder, usdc_fn=lambda: 100., token_fn=lambda _mint: 0.)
    return ex, kp, calls


def invoke(conn, ex, kp, pid, lane, **kwargs):
    if kwargs.get('entry_max_slot') is not None:
        kwargs.setdefault('entry_min_slot', kwargs['entry_max_slot'] - 1)
    if lane == 'ordinary':
        return ex['execute_fn'](conn, pid, 'coin', 10., NOW, **kwargs)
    message = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    return ex['slot_execute_fn'](conn, m.pending_row(conn, pid), message, now_iso=NOW, **kwargs)


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.parametrize('head,reason', [
    (lambda: (103, time.monotonic()), 'entry_slot_expired'),
    (None, 'entry_slot_head_unknown'),
    (lambda: (102, time.monotonic() - 1), 'entry_slot_head_stale'),
])
def test_unusable_window_releases_only_unsigned_before_build_or_sign(
        ledger, monkeypatch, lane, head, reason):
    def forbidden(*args, **kwargs):
        pytest.fail('a blocked entry reached execution or signing')
    monkeypatch.setattr(swap, 'execute_entry', forbidden)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', forbidden)
    ex, kp, http = factory(monkeypatch, fast_builder=forbidden)
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, lane, entry_max_slot=102, entry_head_fn=head)
    assert result['status'] == 'aborted' and result['reason'] == reason
    row = m.pending_row(ledger, pid)
    assert row['status'] == 'failed' and row['resolution_reason'] == reason
    assert not row['tx_sig'] and not http


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.parametrize('head,reason', [
    (lambda: (None, time.monotonic()), 'entry_slot_head_unknown'),
    (lambda: (200, time.monotonic() - 1), 'entry_slot_head_stale'),
    (lambda: (_ for _ in ()).throw(OSError()), 'entry_slot_head_unknown'),
])
def test_head_only_quality_refuses_stale_unknown_or_failed_head_before_execution(
        ledger, monkeypatch, lane, head, reason):
    def forbidden(*args, **kwargs):
        pytest.fail('entry with unproven head freshness reached execution')
    monkeypatch.setattr(swap, 'execute_entry', forbidden)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', forbidden)
    ex, kp, http = factory(monkeypatch, fast_builder=forbidden)
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, lane, entry_head_fn=head)
    assert result['status'] == 'aborted' and result['reason'] == reason
    row = m.pending_row(ledger, pid)
    assert row['status'] == 'failed' and not row['tx_sig'] and not http


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
def test_expired_reentry_never_releases_existing_signed_owner(ledger, monkeypatch, lane):
    ex, kp, _ = factory(monkeypatch)
    pid = reserve(ledger)
    m.set_pending_sig(ledger, pid, 'possibly-broadcast', NOW)
    result = invoke(ledger, ex, kp, pid, lane, entry_max_slot=102,
                    entry_head_fn=lambda: (103, time.monotonic()))
    row = m.pending_row(ledger, pid)
    assert result['status'] == 'unknown' and result['reason'] == 'entry_slot_expired'
    assert row['status'] == 'reserved' and row['tx_sig'] == 'possibly-broadcast'


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
def test_guarded_automatic_entry_cannot_use_the_legacy_rpc_bypass(ledger, monkeypatch, lane):
    ex, kp, http = factory(monkeypatch, transport=False)
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, lane, entry_max_slot=102,
                    entry_head_fn=lambda: (102, time.monotonic()))
    assert result['status'] == 'aborted'
    assert result['reason'] == 'entry_slot_transport_unavailable'
    assert m.pending_row(ledger, pid)['status'] == 'failed' and not http


def test_ordinary_quote_delay_is_refused_before_builder_and_preserves_existing_presign(
        ledger, monkeypatch):
    head, presign = [102], []
    def inspect(_conn, **kw):
        assert kw['presign_refusal_fn']() == 'existing_coin_age_policy'
        head[0] = 103
        kw['sign_fn']({})
        pytest.fail('expired signer returned')
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    ex, kp, _ = factory(monkeypatch, fast_builder=lambda _quote: pytest.fail('builder ran'))
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, 'ordinary', entry_max_slot=102,
        entry_head_fn=lambda: (head[0], time.monotonic()),
        presign_refusal_fn=lambda: presign.append(True) or 'existing_coin_age_policy')
    assert result['status'] == 'aborted' and result['reason'] == 'entry_slot_expired'
    assert presign == [True] and m.pending_row(ledger, pid)['resolution_reason'] == 'entry_slot_expired'


def test_ordinary_swap_build_delay_is_refused_before_local_signature(ledger, monkeypatch):
    head, built = [102], []
    def inspect(_conn, **kw):
        kw['sign_fn']({})
        pytest.fail('expired signer returned')
    def build(*args, **kwargs):
        built.append(True)
        head[0] = 103
        return 'unsigned'
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    monkeypatch.setattr(S, 'jupiter_swap_tx', build)
    monkeypatch.setattr(S, 'sign_swap_tx', lambda *_a: pytest.fail('expired transaction signed'))
    ex, kp, _ = factory(monkeypatch)
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, 'ordinary', entry_max_slot=102,
                    entry_head_fn=lambda: (head[0], time.monotonic()))
    assert built == [True] and result['reason'] == 'entry_slot_expired'
    assert m.pending_row(ledger, pid)['status'] == 'failed'


@pytest.mark.parametrize('expire', [False, True])
def test_guarded_fast_builder_uses_separable_signing_and_retains_validity(ledger, monkeypatch, expire):
    import base64
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    head, built, signed = [102], [], []
    holder = {}
    real_sign = S.sign_swap_tx

    def build(*args, **kwargs):
        assert kwargs['return_metadata'] is True
        built.append(True)
        if expire:
            head[0] = 103
        message = MessageV0.try_compile(holder['key'].pubkey(), [], [], Hash.default())
        unsigned = VersionedTransaction.populate(message, [Signature.default()])
        return dict(swapTransaction=base64.b64encode(bytes(unsigned)).decode(), lastValidBlockHeight=777)

    def sign(*args):
        signed.append(True)
        return real_sign(*args)

    def inspect(_conn, **kw):
        payload, _sig, validity = kw['sign_fn']({})
        assert validity == dict(blockhash=str(Hash.default()), last_valid_height=777)
        assert kw['send_fn'](payload, validity=validity)
        return {'status': 'unknown'}

    monkeypatch.setattr(S, 'jupiter_swap_tx', build)
    monkeypatch.setattr(S, 'sign_swap_tx', sign)
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    ex, kp, _ = factory(monkeypatch, fast_builder=lambda _q: pytest.fail('guarded monolithic builder ran'))
    holder['key'] = kp
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, 'ordinary', entry_max_slot=102,
        entry_head_fn=lambda: (head[0], time.monotonic()))
    assert built == [True]
    if expire:
        assert signed == [] and result['reason'] == 'entry_slot_expired'
        assert not m.pending_row(ledger, pid)['tx_sig']
    else:
        assert signed == [True]
        binding = m.entry_submission_binding(ledger, pid)
        assert binding['submission_blockhash'] == str(Hash.default())
        assert binding['submission_last_valid_height'] == 777
        assert binding['signed_payload_b64']


def test_unguarded_legacy_entry_keeps_monolithic_fast_builder(ledger, monkeypatch):
    signed, calls = _signed_tx(), []
    def fast(quote):
        calls.append(quote)
        return signed.signed_b64, signed.signature
    def inspect(_conn, **kw):
        assert kw['sign_fn']({}) == (signed.signed_b64, signed.signature)
        return {'status': 'unknown'}
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *_a, **_kw: pytest.fail('legacy builder changed'))
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    ex, kp, _ = factory(monkeypatch, fast_builder=fast)
    invoke(ledger, ex, kp, reserve(ledger), 'ordinary')
    assert calls == [{}]


@pytest.mark.parametrize('lane', ['ordinary', 'slot'])
@pytest.mark.usefixtures("fake_payload_clamp")
def test_both_signing_seams_pass_the_original_cap_and_live_head_to_the_adapter(
        ledger, monkeypatch, lane):
    seen = []
    signed = _signed_tx()
    def inspect(_conn, **kw):
        if lane == 'ordinary':
            payload = kw['sign_fn']({})[0]
        else:
            payload = kw['signed_b64']
        send = kw['send_fn']
        assert send(payload)
        seen.append(send.last_request)
        return {'status': 'unknown'}
    monkeypatch.setattr(swap, 'execute_entry', inspect)
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', inspect)
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *_a, **_kw: signed.signed_b64)
    monkeypatch.setattr(S, 'sign_swap_tx', lambda *_a: (signed.signed_b64, signed.signature))
    ex, kp, _ = factory(monkeypatch, fast_builder=lambda _q: (signed.signed_b64, signed.signature))
    pid = reserve(ledger)
    head = lambda: (102, time.monotonic())
    invoke(ledger, ex, kp, pid, lane, entry_max_slot=102, entry_head_fn=head)
    req, = seen
    assert req.entry_max_slot == 102 and req.entry_head_fn is head
    assert m.pending_row(ledger, pid)['tx_sig'] == req.tx_identity


def test_slot_boundary_delay_is_refused_before_signature(ledger, monkeypatch):
    head = [102]
    monkeypatch.setattr(slot, 'execute_prebuilt_entry', lambda *_a, **_k: pytest.fail('entry signed'))
    ex, kp, _ = factory(monkeypatch)
    pid = reserve(ledger)
    def boundary():
        head[0] = 103
        return {'source_slot': 100, 'head_slot': 103}
    result = invoke(ledger, ex, kp, pid, 'slot', entry_max_slot=102,
        entry_head_fn=lambda: (head[0], time.monotonic()), boundary_fn=boundary)
    assert result['reason'] == 'entry_slot_expired' and result['status'] == 'aborted'
    assert not m.pending_row(ledger, pid)['tx_sig']


def test_actual_slot_wrapper_retains_binding_if_commit_consumes_the_window(ledger, monkeypatch):
    head = [102]
    bind = m.bind_entry_submission
    def delayed_bind(*args, **kwargs):
        result = bind(*args, **kwargs)
        head[0] = 103
        return result
    monkeypatch.setattr(m, 'bind_entry_submission', delayed_bind)
    monkeypatch.setattr(StubTransport, 'submit', lambda *_a: pytest.fail('expired bytes sent'))
    monkeypatch.setattr(S, 'rpc_confirm', lambda *_a, **_kw: 'pending')
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'memecoin_slot_lane_resend_offsets_ms', lambda: ())
    ex, kp, _ = factory(monkeypatch)
    pid = reserve(ledger)
    result = invoke(ledger, ex, kp, pid, 'slot', entry_max_slot=102,
                    entry_head_fn=lambda: (head[0], time.monotonic()))
    assert result['status'] == 'unknown' and head == [103]
    row = m.pending_row(ledger, pid)
    assert row['status'] == 'reserved' and row['tx_sig'] == result['sig']
    assert row['signed_payload_b64'] and row['signed_payload_sha256']
    assert flush_async_audit(timeout_s=2)


def test_deferred_confirmation_after_the_cap_books_an_in_window_landing(ledger, monkeypatch):
    head, confirmations = [102], []
    kp = Keypair()
    owner = str(kp.pubkey())
    def receipt(sig):
        def balance(index, mint, amount):
            return {'accountIndex': index, 'mint': mint, 'owner': owner,
                    'uiTokenAmount': {'amount': str(amount), 'decimals': 6}}
        return {'transaction': {'signatures': [sig], 'message': {'accountKeys': [
                    {'pubkey': owner, 'signer': True}, {'pubkey': 'token'}, {'pubkey': 'usdc'}]}},
                'meta': {'err': None, 'preBalances': [1_000_000, 1, 1],
                    'postBalances': [990_000, 1, 1],
                    'preTokenBalances': [balance(1, 'coin', 0), balance(2, USDC_MINT, 10_000_000)],
                    'postTokenBalances': [balance(1, 'coin', 2_000_000_000), balance(2, USDC_MINT, 0)]}}
    monkeypatch.setattr(S, 'successful_receipt_cache', lambda _read: receipt)
    def confirm(sig, *_a, **_kw):
        confirmations.append(sig)
        return S.ConfirmState('confirmed', landing_slot=102)
    monkeypatch.setattr(S, 'rpc_confirm', confirm)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'memecoin_slot_lane_resend_offsets_ms', lambda: ())
    transport = StubTransport(AttemptState.ACCEPTED)
    ex = S.build_real_solana_executor(keypair=kp, rpc_url='http://offline.invalid',
        http=lambda *_a, **_kw: pytest.fail('unexpected HTTP'), submission_transport=transport)
    pid = reserve(ledger)
    message = MessageV0.try_compile(kp.pubkey(),
        [set_compute_unit_limit(400_000), set_compute_unit_price(1_000)], [], Hash.default())
    result = ex['slot_execute_fn'](ledger, m.pending_row(ledger, pid), message, now_iso=NOW,
        defer_confirmation=True, entry_min_slot=101, entry_max_slot=102,
        entry_head_fn=lambda: (head[0], time.monotonic()))
    assert result['status'] == 'submitted' and not confirmations and transport.calls == 1
    head[0] = 103
    settled = result['_settlement'].resolve(ledger)
    assert settled['status'] == 'filled' and settled['landing_slot'] == 102
    assert confirmations == [result['sig']] and transport.calls == 1
    assert m.pending_row(ledger, pid)['status'] == 'confirmed'
    assert flush_async_audit(timeout_s=2)


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401
