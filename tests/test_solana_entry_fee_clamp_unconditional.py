"""Option B (operator yes 2026-10-22): every Solana ENTRY build is clamped to the single entry fee ceiling at
signing, with or without the EntryBidCache. Codex's reproducer: no cache, Jupiter 'auto' returning 1.4M CU x
100M uL (140M lamports) used to be signed unchanged."""
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

from sniper.execution import solana_real_seams as seams
from sniper.execution import solana_swap_executor as executor
from sniper.execution.solana_entry_bids import MAX_TOTAL_LAMPORTS
from sniper.persistence import db_memecoin as db


def _offline(*a, **k):
    pytest.fail('offline regression reached an external provider')


@pytest.mark.parametrize("sender_tip", [0, 50_000_000])
def test_no_cache_jupiter_auto_entry_is_clamped_at_signing(monkeypatch, sender_tip):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv('MEMECOIN_NOZOMI_ALL_LANES', raising=False)
    monkeypatch.setattr(cfg, 'MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS', None)          # Jupiter 'auto'
    kp, router = Keypair(), Pubkey.new_unique()
    from sniper.execution import solana_sender_ab as A
    tips = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], sender_tip)) if sender_tip else []
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(1_400_000),
        set_compute_unit_price(100_000_000), Instruction(router, bytes(40), []), *tips], [], Hash.default())
    payload = base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()
    assert executor.signed_priority_fee_lamports(payload) == 140_000_000                # the unclamped build
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    pid = db.reserve_pending(c, mint='MINT', source_user_id='u1', strategy='alpha_b', notional=10,
                             reserved_at='2026-07-09T00:00:00+00:00', network_id='solana', signal_source='manual')
    signed, sent = [], []

    class Quotes:
        def __init__(self, http): pass
        def quote(self, mint, usd, max_accounts=None, **kw):
            return dict(inAmount='10000000', outAmount='500', otherAmountThreshold='470',
                        priceImpactPct=0.01, routePlan=[])
        def reverse(self, mint, amount, slip):
            return dict(inAmount=str(amount), outAmount='9000000', otherAmountThreshold='8100000')
        def miss(self): return None
        def reverse_miss(self): return None

    real_sign = seams.sign_swap_tx
    def sign(swap, keypair):
        out = real_sign(swap, keypair)
        if out: signed.append(out)
        return out
    def send(p, *a, **k):
        sent.append(p)
        return str(VersionedTransaction.from_bytes(base64.b64decode(p)).signatures[0])
    monkeypatch.setattr(seams, 'EntryQuoteMissRecorder', Quotes)
    monkeypatch.setattr(seams, 'jupiter_swap_tx', lambda quote, *a, **k: payload)
    monkeypatch.setattr(seams, 'sign_swap_tx', sign)
    monkeypatch.setattr(seams, 'rpc_send', send)
    monkeypatch.setattr(seams, 'rpc_confirm', lambda *a, **k: 'failed')
    monkeypatch.setattr(seams, 'exclude_failed_venue', lambda *a, **k: None)
    monkeypatch.setattr(executor, '_post_peak_confirmation', lambda *a, **k: None)
    try:
        seam = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused', http=_offline,
            usdc_fn=lambda: 100.0, token_fn=lambda _: (0.0, 6), entry_bids=None)
        seam['execute_fn'](c, pid, 'MINT', 10, '2026-07-09T00:00:01+00:00')
        if sender_tip:                      # Codex P1 on ffbaa9e43: a 50M known Sender tip is refused UNSIGNED
            assert not signed and not sent
            return
        assert signed, "nothing was signed"
        for p, _sig in signed:
            VersionedTransaction.from_bytes(base64.b64decode(p)).verify_and_hash_message()
            assert executor.signed_priority_fee_lamports(p) <= MAX_TOTAL_LAMPORTS
    finally:
        c.close()


def test_no_cache_slot_lane_entry_is_clamped_before_its_one_signature(conn, monkeypatch):  # noqa: F811
    from sniper.execution import solana_slot_executor as E
    from test_memecoin_solana_slot_executor import _reserve
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [set_compute_unit_limit(1_400_000), set_compute_unit_price(100_000_000),
                                              Instruction(Pubkey.new_unique(), bytes(8), [])], [], Hash.default())
    captured = []
    monkeypatch.setattr(E, 'execute_prebuilt_entry', lambda _conn, **kw: captured.append(kw) or
                        {'status': 'unknown', 'sig': kw['sig']})
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused', entry_bids=None)
    result = seam['slot_execute_fn'](conn, _reserve(conn), msg, validity={}, now_iso='2026-10-13T00:00:00Z')
    assert result['status'] == 'unknown' and len(captured) == 1
    signed = captured[0]['signed_b64']
    VersionedTransaction.from_bytes(base64.b64decode(signed)).verify_and_hash_message()
    assert executor.signed_priority_fee_lamports(signed) <= MAX_TOTAL_LAMPORTS


from test_memecoin_solana_slot_executor import conn  # noqa: E402,F401
