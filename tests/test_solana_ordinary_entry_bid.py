"""Ordinary entry builders must apply the approved cached bid before signing."""
import base64
import sqlite3

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as A
from sniper.execution.solana_entry_bids import EntryBidCache


def cache():
    def no_provider(*a, **k):
        pytest.fail('cached fee selection performed provider I/O')
    return EntryBidCache(no_provider, no_provider)


def test_ordinary_executor_applies_bid_and_records_exact_signed_fee(monkeypatch):
    from sniper.execution import solana_real_seams as S, solana_swap_executor as E
    from sniper.persistence import db_memecoin as m
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(400_000),
        set_compute_unit_price(250_000)], [], Hash.default())
    raw = base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *a, **k: raw)
    captured, events = [], []
    class Trace:
        attempt_id = 'ordinary-cached-bid'
        def mark_substage(self, name, **values): events.append((name, values))
    def execute(conn, *, sign_fn, **kw):
        captured.append(sign_fn({'routePlan': [], 'outputMint': 'fixture'}))
        return {'status': 'aborted', 'reason': 'offline fixture'}
    monkeypatch.setattr(E, 'execute_entry', execute)
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    try:
        m.ensure_memecoin_tables(c)
        pid = m.reserve_pending(c, mint='fixture', source_user_id='wallet', strategy='mirror',
            notional=50, reserved_at='2026-10-13T00:00:00+00:00', network_id='solana',
            signal_source='manual')
        seam = S.build_real_solana_executor(keypair=kp, rpc_url='http://unused', entry_bids=cache())
        seam['execute_fn'](c, pid, 'fixture', 50, '2026-10-13T00:00:01+00:00', trace=Trace())
        signed = captured[0][0]
        assert E.signed_priority_fee_lamports(signed) == 1_520_000
        bid = [v for name, v in events if name == 'entry_bid_signed'][-1]
        assert bid['priority_fee_lamports'] == 1_520_000
        assert bid['priority_source'] == 'fallback_cold'
        VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_and_hash_message()
    finally:
        c.close()


def test_cached_bid_preserves_swap_identity_and_approved_combined_cap():
    from test_solana_entry_bid_wiring import message
    msg = message(price=250_000, limit=400_000, tip=5_000_000)
    got, info = A.cap_entry_priority_fee(msg, tip_lamports=5_000_000, entry_bids=cache())
    assert info['priority_fee_lamports'] == 1_520_000
    assert info['combined_fee_lamports'] == 6_520_000
    assert got.account_keys == msg.account_keys
    assert got.address_table_lookups == msg.address_table_lookups
    assert got.recent_blockhash == msg.recent_blockhash
    assert got.instructions[0] == msg.instructions[0]
    assert got.instructions[2:] == msg.instructions[2:]


def test_cached_bid_adds_missing_price_without_shifting_any_account_index():
    from test_memecoin_nozomi_all_lanes import _jupiter_like
    msg = _jupiter_like(Keypair().pubkey())
    got, info = A.cap_entry_priority_fee(msg, tip_lamports=5_000_000, entry_bids=cache())
    assert info['priority_fee_lamports'] == 1_520_000
    assert got.account_keys == msg.account_keys and got.header == msg.header
    assert got.instructions[:-1] == msg.instructions
    assert got.address_table_lookups == msg.address_table_lookups


def test_cached_unsigned_repricing_never_edits_an_existing_signature():
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(400_000),
        set_compute_unit_price(250_000)], [], Hash.default())
    signed = base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode()
    with pytest.raises(ValueError, match='signed'):
        A.cap_unsigned_entry_priority(signed, tip_lamports=5_000_000, entry_bids=cache())


def test_cached_unsigned_repricing_preserves_oversize_for_the_existing_executor_retry():
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    kp = Keypair()
    def payload(pad):
        msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(400_000),
            set_compute_unit_price(250_000), Instruction(Pubkey.new_unique(), bytes(pad), [])],
            [], Hash.default())
        return base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()
    raw = payload(900)
    raw = payload(900 + 1200 - len(base64.b64decode(raw)))
    assert len(base64.b64decode(raw)) == 1200
    out, _ = A.cap_unsigned_entry_priority(raw, tip_lamports=5_000_000, entry_bids=cache())
    tx = VersionedTransaction.from_bytes(base64.b64decode(out))
    signed = VersionedTransaction(tx.message, [kp])
    assert len(bytes(signed)) > 1232


def test_missing_compute_limit_never_fabricates_exact_fee_or_selects_a_bid():
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_price(25_000)], [], Hash.default())
    class NeverSelect:
        def select(self, **kw): pytest.fail('unknown CU limit cannot select an exact bid')
    got, info = A.cap_entry_priority_fee(msg, tip_lamports=5_000_000, entry_bids=NeverSelect())
    assert bytes(got) == bytes(msg)
    assert info['cu_limit'] is None
    assert info['priority_fee_lamports'] is None and info['combined_fee_lamports'] is None
    assert info['priority_fee_upper_bound_lamports'] == 35_000
    assert info['combined_fee_upper_bound_lamports'] == 5_035_000
