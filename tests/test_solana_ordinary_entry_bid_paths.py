"""Cached ordinary bids preserve safe retries and trace the actual frozen Nozomi tip."""
import base64
import sqlite3

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.execution import solana_real_seams as seams, solana_sender_ab as sender
from sniper.execution import solana_swap_executor as executor
from sniper.execution.solana_entry_bids import EntryBidCache
from sniper.persistence import db_memecoin as db
from test_memecoin_nozomi_all_lanes import _Trace, conn  # noqa: F401


def _offline(*a, **k):
    pytest.fail('offline regression reached an external provider')


def test_ordinary_cached_bid_preserves_oversize_retry_before_any_binding_or_send(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv('MEMECOIN_NOZOMI_ALL_LANES', raising=False)
    monkeypatch.setattr(cfg, 'MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS', 48)
    kp = Keypair()
    router = Pubkey.new_unique()
    def unsigned(pad):
        msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(400_000),
            set_compute_unit_price(250_000), Instruction(router, bytes(pad), [])], [], Hash.default())
        return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()
    big, small = unsigned(1200), unsigned(100)
    assert len(base64.b64decode(big)) > 1232 and len(base64.b64decode(small)) < 1232
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    pid = db.reserve_pending(c, mint='MINT', source_user_id='u1', strategy='alpha_b',
        notional=10, reserved_at='2026-07-09T00:00:00+00:00', network_id='solana', signal_source='manual')
    quotes, bound, sent, signed = [], [], [], []

    class Quotes:
        def __init__(self, http): pass
        def quote(self, mint, usd, max_accounts=None, **kw):
            assert not sent and db.pending_row(c, pid)['tx_sig'] is None
            quotes.append(max_accounts)
            return dict(inAmount='10000000', outAmount='500', otherAmountThreshold='470',
                        priceImpactPct=0.01, routePlan=[], _max_accounts=max_accounts)
        def reverse(self, mint, amount, slip):
            return dict(inAmount=str(amount), outAmount='9000000', otherAmountThreshold='8100000')
        def miss(self): return None
        def reverse_miss(self): return None

    monkeypatch.setattr(seams, 'EntryQuoteMissRecorder', Quotes)
    monkeypatch.setattr(seams, 'jupiter_swap_tx',
        lambda quote, *a, **k: small if quote.get('_max_accounts') else big)
    real_sign, real_bind = seams.sign_swap_tx, db.set_pending_sig
    def sign(swap, keypair):
        result = real_sign(swap, keypair)
        if result: signed.append(result)
        return result
    def bind(conn_, pending_id, signature, now, **kw):
        bound.append(signature)
        return real_bind(conn_, pending_id, signature, now, **kw)
    def send(payload, *a, **k):
        tx = VersionedTransaction.from_bytes(base64.b64decode(payload))
        assert len(base64.b64decode(payload)) <= 1232
        assert db.pending_row(c, pid)['tx_sig'] == str(tx.signatures[0])
        sent.append(payload)
        return str(tx.signatures[0])
    monkeypatch.setattr(seams, 'sign_swap_tx', sign)
    monkeypatch.setattr(db, 'set_pending_sig', bind)
    monkeypatch.setattr(seams, 'rpc_send', send)
    monkeypatch.setattr(seams, 'rpc_confirm', lambda *a, **k: 'failed')
    monkeypatch.setattr(seams, 'exclude_failed_venue', lambda *a, **k: None)
    monkeypatch.setattr(executor, '_post_peak_confirmation', lambda *a, **k: None)
    cache = EntryBidCache(_offline, _offline)
    try:
        seam = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused', http=_offline,
            usdc_fn=lambda: 100.0, token_fn=lambda _: (0.0, 6), entry_bids=cache)
        result = seam['execute_fn'](c, pid, 'MINT', 10, '2026-07-09T00:00:01+00:00')
        assert quotes == [None, 48], result
        assert len(signed) == 2 and len(sent) == 1
        assert bound == [signed[1][1]] and sent == [signed[1][0]]
        assert len(base64.b64decode(signed[0][0])) > 1232
        for payload, signature in signed:
            tx = VersionedTransaction.from_bytes(base64.b64decode(payload))
            tx.verify_and_hash_message()
            assert executor.signed_priority_fee_lamports(payload) == 1_520_000
            assert executor.signed_priority_fee_lamports(payload) <= 6_520_000
    finally:
        c.close()


def test_frozen_dynamic_nozomi_tip_keeps_real_ack_identity_and_exact_trace(conn, monkeypatch):
    import test_memecoin_nozomi_all_lanes as fixture
    from sniper.execution.submission_transport import SubmissionTransport
    cache = EntryBidCache(_offline, _offline)
    selections = []
    def tip():
        selections.append(1)
        return 1_250_000
    cache.tip_lamports = tip
    real_build, real_provider = seams.build_real_solana_executor, sender.NozomiProvider
    monkeypatch.setattr(seams, 'build_real_solana_executor',
        lambda **kwargs: real_build(**kwargs, entry_bids=cache))
    monkeypatch.setattr(sender, 'NozomiProvider',
        lambda *a, **k: real_provider(*a, **k, dynamic_tips=True))
    # One real offline Nozomi provider makes its ACK available before the trace is projected.
    monkeypatch.setattr(fixture, 'SubmissionTransport', lambda providers, **kw:
        SubmissionTransport([p for p in providers if p.provider_id == 'solana:nozomi'], **kw))
    reserve_pending = db.reserve_pending
    def reserve_manual(*args, **kwargs):
        kwargs['signal_source'] = 'manual'
        return reserve_pending(*args, **kwargs)
    monkeypatch.setattr(db, 'reserve_pending', reserve_manual)
    trace = _Trace(fixture._ids()['B'])
    seam, pid, raw, kp, tpu, sender_http, nozomi_http, signed = fixture._executor(
        conn, monkeypatch, knob=True)
    seam['execute_fn'](conn, pid, 'Coin', 50.0, '2026-10-09T14:00:01+00:00', trace=trace)
    assert selections == [1]
    assert len(nozomi_http.calls) == 1 and sender_http.calls == [] and tpu == []
    payload, signature = signed[-1][:2]
    assert nozomi_http.calls[0][2]['params'][0] == payload
    tx = VersionedTransaction.from_bytes(base64.b64decode(payload))
    tx.verify_and_hash_message()
    assert sender.payload_tip(payload, sender._NOZOMI_SET, 5_000_000) == 1_250_000
    assert trace.stages['nozomi_ab']['tip_lamports'] == 1_250_000
    ack = trace.stages['nozomi_ab_send']
    assert ack['tip_lamports'] == 1_250_000
    assert ack['nozomi_state'] == 'accepted'
    assert ack['nozomi_payload_sha_match'] is True and ack['nozomi_sig_match'] is True
