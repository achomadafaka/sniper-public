"""Solana buys need the trader's signature and linked payment, rather than a receipt of tokens."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_source_initiation as initiation
from sniper.services.memecoin_source_valuation import value_source_buy


def receipt(name='untaxxable'):
    tx = json.loads((Path(__file__).parent/'fixtures/memecoin/confirmed_source_buys.json').read_text())[name]
    return tx, tx['meta']['postTokenBalances'][0]['owner']


def proof_for(tx, owner):
    build = getattr(initiation, 'solana_receipt_initiation', None)
    assert callable(build), 'Solana receipt initiation is missing'
    facts = value_source_buy(tx, owner, sol_usd=(120.,100.), now=105.)
    return build(tx, owner, facts)


def event(proof):
    return SimpleNamespace(signal_source='onchain_watch', network_id='solana',
        fomo_event_id='onchain:'+proof['tx_hash'], source_initiation=proof,
        source_wallet=proof['beneficiary'], source_block_number=proof['block_number'],
        mint=proof['mint'], source_user_id='u1')


@pytest.mark.parametrize('name', ['untaxxable','notdecu-later'])
def test_signed_owner_paid_receipt_passes_without_fee_payer_assumption(name):
    tx, owner = receipt(name)
    proof = dict(proof_for(tx,owner),source_user_id='u1')
    assert proof['payer']==owner and proof['network_id']=='solana'
    assert initiation.source_initiation_refusal(event(proof),'solana') is None


@pytest.mark.parametrize('damage', ['nonsigner','missing_signer','failed','signature','slot','payment','mint'])
def test_solana_delivery_and_incomplete_initiation_are_not_authority(damage):
    tx, owner = receipt()
    if damage in ('nonsigner','missing_signer'):
        for key in tx['transaction']['message']['accountKeys']:
            if key['pubkey']==owner:
                if damage=='missing_signer':key.pop('signer')
                else:key['signer']=False
    elif damage=='failed':tx['meta']['err']={'InstructionError':[0,'failed']}
    elif damage=='signature':tx['transaction']['signatures']=[]
    elif damage=='slot':tx['slot']=0
    elif damage=='payment':
        tx['meta']['logMessages']=[]
        tx['meta']['innerInstructions']=[]
        tx['transaction']['message']['instructions']=[]
    else:tx['meta']['postTokenBalances'][0]['mint']='wrong'
    assert proof_for(tx,owner) is None


@pytest.mark.parametrize('field,value', [('payer','11111111111111111111111111111111'),
    ('source_user_id','other'),('network_id','56'),('block_number',True),('acquired_atomic','0'),
    ('payment_atomic','0'),('mint','invalid'),('tx_hash','invalid')])
def test_solana_proof_cannot_move_between_wallets_coins_chains_and_slots(field,value):
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=event(proof);ev.source_initiation=dict(proof,**{field:value})
    assert initiation.source_initiation_refusal(ev,'solana')=='receipt_identity_mismatch'


def test_raw_message_header_requires_the_owner_among_static_signers():
    tx,owner=receipt();msg=tx['transaction']['message'];keys=msg['accountKeys']
    required=sum(k.get('signer') is True for k in keys)
    msg['accountKeys']=[k['pubkey'] for k in keys]
    msg['header']={'numRequiredSignatures':required}
    assert proof_for(tx,owner)
    msg['header']['numRequiredSignatures']=0
    assert proof_for(tx,owner) is None


def slot_buy():
    import base58
    from sniper.services.memecoin_slot_signal import decode_owner_buy
    tx,owner=receipt('notdecu-later')
    frame=dict(params=dict(result=dict(context=dict(slot=tx['slot']),value=dict(
        signature=tx['transaction']['signatures'][0],err=None,logs=tx['meta']['logMessages']))))
    buy=decode_owner_buy(frame,owner,base58.b58decode(owner),'u1',recv_monotonic=1.,recv_utc='2026-10-06T14:00:00Z')
    assert buy
    return buy


def test_processed_known_program_buy_proves_its_own_actor_without_a_receipt_read():
    buy=slot_buy();proof=initiation.solana_program_initiation(buy,buy.mint)
    assert proof and proof['payer']==buy.owner
    ev=event(proof);ev.signal_source='slot_lane';ev.fomo_event_id='slotlane:'+buy.signature
    assert initiation.source_initiation_refusal(ev,'solana') is None
    import dataclasses
    foreign=dataclasses.replace(buy,owner='11111111111111111111111111111111')
    assert initiation.solana_program_initiation(foreign,buy.mint) is None


def test_unproved_direct_slot_event_stops_before_classification_or_reservation(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane,memecoin_market_state as market
    def forbidden(*a,**k):pytest.fail('unproved slot signal reached funded classification')
    monkeypatch.setattr(market,'classify_funded_entry',forbidden)
    tx,owner=receipt();facts=value_source_buy(tx,owner,sol_usd=(120.,100.),now=105.)
    proof=dict(proof_for(tx,owner),source_user_id='u1');ev=event(proof)
    ev.signal_source='slot_lane';ev.fomo_event_id='slotlane:'+proof['tx_hash'];ev.source_initiation=None
    result=lane.SlotLane._reserve_and_execute(SimpleNamespace(conn=None,routed_handoff=None),None,ev,None,None,
        dict(mint=facts['mint'],mcap_usd=11000.,price_usd=1.),'2026-10-06T14:00:00Z')
    assert result=={'status':'source_initiation_unknown','reason':'missing_receipt_proof'}


def test_unproved_processed_handoff_leaves_the_confirmed_receipt_free_to_write(tmp_path):
    import sqlite3
    import threading
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_slot_lane import RoutedHandoff
    from test_slot_lane_routed import _rb
    conn=sqlite3.connect(tmp_path/'ledger.db');m.ensure_memecoin_tables(conn)
    handoff=RoutedHandoff(conn_factory=lambda: conn,wake=threading.Event())
    assert handoff.write(_rb())=='awaiting_signer_proof'
    assert conn.execute('SELECT COUNT(*) FROM onchain_wallet_detections').fetchone()[0]==0


def test_slot_refusal_for_missing_initiation_allows_one_confirmed_receipt_attempt():
    from test_memecoin_onchain_lane import _slot_conn,_slot_attempt,NOW
    from sniper.services.memecoin_onchain_lane import slot_lane_outcome
    conn=_slot_conn(sig='unproved')
    _slot_attempt(conn,sig='unproved',terminal='source_initiation_unknown',reason='missing_receipt_proof')
    assert slot_lane_outcome(conn,'unproved',NOW.isoformat())=='failed'


@pytest.mark.parametrize('signer',[True,False])
def test_confirmed_writer_and_retry_keep_the_bound_signer_proof(tmp_path,monkeypatch,signer):
    import asyncio
    import sqlite3
    import time
    from scripts.research import onchain_wallet_watch as watcher
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_onchain_lane import OnchainEv,retry_payload,event_from_retry_payload
    tx,owner=receipt()
    for key in tx['transaction']['message']['accountKeys']:
        if key['pubkey']==owner:key['signer']=signer
    calls=[]
    def rpc(method,*args):
        calls.append(method);assert method=='getTransaction';return tx
    monkeypatch.setattr(watcher,'_http_rpc',rpc)
    monkeypatch.setattr(watcher,'_stage_write',lambda *_:None)
    monkeypatch.setattr(watcher,'send_wake',lambda *_:None)
    monkeypatch.setattr(watcher,'DB',tmp_path/'ledger.db')
    conn=sqlite3.connect(tmp_path/'ledger.db');conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    asyncio.run(watcher.process_signature(tx['transaction']['signatures'][0],owner,
        dict(uid='u1',handle='h',status='armed'),conn,sol_usd=(120.,time.time())))
    row=conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    ev=event_from_retry_payload(json.loads(json.dumps(retry_payload(OnchainEv(row)))))
    assert calls==['getTransaction']
    assert (ev.source_initiation is not None)==signer
    assert initiation.source_initiation_refusal(ev,'solana')==(None if signer else 'missing_receipt_proof')


def test_signed_solana_buy_reaches_the_shared_funded_hook(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn,_event
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source='onchain_watch',
        event_id='onchain:'+proof['tx_hash'],market_cap=11000.)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    conn=_conn();_arm(monkeypatch);_approve(conn,chains=('solana',))
    reached=[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45)
    assert hook.raw(conn,ev)['status']=='fixture_admitted' and reached


@pytest.mark.parametrize('venue',['raydium_cpmm','raydium_launchlab','meteora_damm_v2'])
def test_processed_balance_pair_is_not_signer_authority_on_any_venue(venue):
    import dataclasses
    buy=dataclasses.replace(slot_buy(),venue=venue)
    assert initiation.solana_program_initiation(buy,buy.mint) is None


@pytest.mark.parametrize('price',[None,(120.,100.)])
@pytest.mark.parametrize('name,owner_key',[
    ('untaxxable-route-753be289','owner'),
    ('notdecu-nonsol-curve-5CUHe5jr','owner_notdecu'),
    ('notdecu-two-legs-voizEg93','owner_notdecu')])
def test_real_routed_receipt_binds_signer_and_payment_independently_of_usd_price(price,name,owner_key):
    fixtures=json.loads((Path(__file__).parent/'fixtures/memecoin/route_source_buys.json').read_text())
    tx,owner=fixtures[name],fixtures[owner_key]
    facts=value_source_buy(tx,owner,sol_usd=price,now=105.)
    proof=initiation.solana_receipt_initiation(tx,owner,facts)
    assert proof, facts.get('unknown_reason')
    proof['source_user_id']='u1'
    assert initiation.source_initiation_refusal(event(proof),'solana') is None


@pytest.mark.parametrize('fallback,manual,price,expected', [
    (False,False,1.,'native_fast_lane_unavailable'),
    (True,False,None,'copy_source_price_unavailable'),
    (True,False,None,'movement_fixture_admitted'),
    (True,False,1.,'fixture_admitted'),
    (False,True,None,'fixture_admitted')])
def test_fast_only_and_source_ceiling_gate_shared_hook_before_provider_work(monkeypatch, fallback, manual, price, expected):
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn,_event
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source='manual' if manual else 'onchain_watch',
        event_id='onchain:'+proof['tx_hash'],market_cap=11000.)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    ev.native_failure_fallback=fallback;ev.their_price=price
    conn=_conn();_arm(monkeypatch);_approve(conn,chains=('solana',))
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    if expected == 'movement_fixture_admitted':
        monkeypatch.setattr(runner.cfg, 'MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY', True)
        expected = 'fixture_admitted'
    reached=[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45)
    result=hook.raw(conn,ev)
    assert result.get('reason',result['status'])==expected
    assert bool(reached)==(expected=='fixture_admitted')
    assert conn.execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0]==0
    conn.close()


@pytest.mark.parametrize('source,mcap,expected', [
    ('onchain_watch', 500_000_000., 'skipped_mcap'),           # the real reason comes first
    ('onchain_watch', 11000., 'native_fast_lane_unavailable'),  # then the fast-lane refusal
    ('follow_activity', 500_000_000., 'native_fast_lane_unavailable')])  # J7 stays early
def test_fast_only_refuses_onchain_rows_after_the_mcap_gate_and_j7_before_it(monkeypatch, source, mcap, expected):
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn,_event
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source=source,
        event_id=('onchain:' if source=='onchain_watch' else 'swap:')+proof['tx_hash'],market_cap=mcap)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    ev.native_failure_fallback=False;ev.slot_fallback_class=None;ev.their_price=1.
    conn=_conn();_arm(monkeypatch);_approve(conn,chains=('solana',))
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    reached,stats=[],[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45,
        stats_fn=lambda *a,**k:stats.append(a) or None)
    result=hook.raw(conn,ev)
    assert result.get('reason',result['status'])==expected or result['status']==expected
    assert not reached
    if source=='follow_activity':
        assert stats==[]                                      # no market read for J7
    assert conn.execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0]==0
    conn.close()


def test_a_refused_onchain_row_without_an_event_mcap_starts_no_read(monkeypatch):
    """Gamma MED-1 (f1b316293): an mcap that needs a read would start the funded early quote and
    the prereads; a row fast-lane-only refuses anyway must start none of them."""
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn,_event
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source='onchain_watch',
        event_id='onchain:'+proof['tx_hash'],market_cap=0.)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    ev.native_failure_fallback=False;ev.slot_fallback_class=None;ev.their_price=1.
    conn=_conn();_arm(monkeypatch);_approve(conn,chains=('solana',))
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    reads=[]
    for name in ('_start_entry_prereads','_start_early_entry_quote','_start_evm_post_trade_read'):
        monkeypatch.setattr(runner,name,lambda *a,_n=name,**k:reads.append(_n))
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reads.append('entry') or {'status':'x'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45,
        stats_fn=lambda *a,**k:reads.append('stats') or {'market_cap':100000.})
    result=hook.raw(conn,ev)
    assert result['reason']=='native_fast_lane_unavailable'
    assert reads==[]
    conn.close()


@pytest.mark.parametrize('klass,classes,admitted', [
    ('landed_fail', None, True),                         # the Degen dip path (slippage landed fail)
    ('routed_handoff', None, True),
    ('routed_handoff', frozenset({'landed_fail'}), False),   # re-judged against the knob NOW
    (None, None, False)])
def test_a_retried_or_dip_reattempted_fallback_keeps_its_admission(monkeypatch, klass, classes, admitted):
    """Gamma MED-2 + operator order fbe64db4 (Degen dip retry live): the retry payload carries the
    slot class, so a transient miss or a skipped_chase dip re-attempt of an ADMITTED fallback is
    admitted again (every other gate still runs); a non-admitted one stays refused."""
    import json as _json
    from sniper.services import memecoin_real_runner as runner
    from sniper.services import memecoin_onchain_lane as O
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    if classes is not None:
        monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FALLBACK_CLASSES',classes)
    row={'signature':proof['tx_hash'],'source_user_id':'u1','handle':'alice','address':owner,
         'network_id':'solana','mint':proof['mint'],'source_size_usd':5000.,'source_price':1.,
         'detected_at':NOW,'source_slot':tx['slot'],
         'source_valuation_json':_json.dumps({'source_initiation':proof})}
    first=O.OnchainEv(row); first.delivery_lane=O.FALLBACK_LANE; first.slot_fallback_class=klass
    rebuilt=runner._retry_event_from_json(_json.dumps(runner._retry_payload(first)))
    assert rebuilt.slot_fallback_class==klass and rebuilt.native_failure_fallback is admitted
    conn=_conn();_arm(monkeypatch);_approve(conn,chains=('solana',))
    reached=[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45,
        stats_fn=lambda *a,**k:{'market_cap':50000.,'price':1.})
    result=hook.attempt(conn,rebuilt)
    assert bool(reached) is admitted, result
    if admitted:                                          # Gamma LOW-3: the class is stamped
        stamps=[e for e in reached[0]['trace']._substage_events if e.get('name')=='slot_fallback']
        assert [(e['slot_fallback_class'],e['admitted']) for e in stamps]==[(klass,True)]
    conn.close()


@pytest.mark.parametrize('armed_chains,roster_gate,price,expected', [
    ((), True, 1., 'not_approved'),                               # not armed: the roster names it
    (('4663',), True, 1., 'not_approved'),                        # armed elsewhere only: same
    (('solana',), True, 1., 'native_fast_lane_unavailable'),      # armed on solana: unchanged
    (('solana',), True, None, 'native_fast_lane_unavailable'),    # no source price: still the silenced label
    ((), False, 1., 'native_fast_lane_unavailable')])             # roster gate off: unchanged
def test_a_j7_solana_buy_from_a_wallet_not_armed_there_reads_not_approved(monkeypatch, armed_chains, roster_gate, price, expected):
    """Lambda 2026-10-07: since c04e4d7a4 the J7 fast-lane-only refusal ran before the roster gate, so
    387 J7 rows in 16 h from wallets NOT armed on Solana read native_fast_lane_unavailable instead of
    not_approved. Only the label moves: either way the buy is refused before any market read,
    reservation, quote or entry hook."""
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_approve,_conn,_event
    from sniper.persistence import db_memecoin as m
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source='follow_activity',event_id='swap:'+proof['tx_hash'],market_cap=11000.)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    ev.native_failure_fallback=False;ev.slot_fallback_class=None;ev.their_price=price
    conn=_conn();_arm(monkeypatch)
    if armed_chains: _approve(conn,chains=armed_chains)
    else: m.add_wallet(conn,'u1','alice')
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_ROSTER_GATE_ENABLED',roster_gate)
    reached,stats=[],[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45,
        stats_fn=lambda *a,**k:stats.append(a) or None)
    result=hook.raw(conn,ev)
    assert result.get('reason',result['status'])==expected
    assert not reached and stats==[]                              # refused before any read or entry
    assert conn.execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0]==0
    conn.close()


def test_an_unreadable_roster_raises_on_a_j7_solana_buy_and_never_reaches_entry(monkeypatch):
    """The roster now runs before the fast-lane refusal, so a J7 Solana buy reads it like every other
    source: a read failure raises out of the hook (the caller logs it), never admits."""
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import NOW,_arm,_conn,_event
    from sniper.persistence import db_memecoin as m
    import sqlite3
    tx,owner=receipt();proof=dict(proof_for(tx,owner),source_user_id='u1')
    ev=_event(network_id='solana',mint=proof['mint'],source='follow_activity',event_id='swap:'+proof['tx_hash'],market_cap=11000.)
    ev.source_wallet=owner;ev.source_block_number=tx['slot'];ev.source_initiation=proof
    ev.native_failure_fallback=False;ev.slot_fallback_class=None;ev.their_price=1.
    conn=_conn();_arm(monkeypatch);m.add_wallet(conn,'u1','alice')
    monkeypatch.setattr(runner.cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(runner.cfg,'MEMECOIN_REAL_ROSTER_GATE_ENABLED',True)
    def broken(*a,**k): raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(runner.m,'real_approved_user_ids',broken)
    reached=[]
    monkeypatch.setattr(runner,'real_entry_hook',lambda *a,**k:reached.append(k) or {'status':'fixture_admitted'})
    ctx=dict(usdc_fn=lambda:100.,native_fn=lambda:1.,token_safety_fn=lambda *a,**k:(True,None),execute_fn=lambda *a:None)
    hook=runner.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=30.,max_age_s=45)
    with pytest.raises(sqlite3.OperationalError):
        hook.raw(conn,ev)
    assert not reached
    assert conn.execute('SELECT COUNT(*) FROM memecoin_pending_entries').fetchone()[0]==0
    conn.close()
