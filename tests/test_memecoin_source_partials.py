"""Repeatable source trims retain independent durable identity and exact quantity."""
from contextlib import nullcontext
import json
import sqlite3

import pytest

from sniper.execution.solana_exit_executor import execute_partial_exit
from sniper.execution.evm_exit_executor import execute_partial_exit_evm
from sniper.execution.solana_wallet import TokenBalanceSnapshot
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_exit_intents as xi
from sniper.services import memecoin_real_runner as runner

NOW = "2026-10-20T10:00:00+00:00"
LATER = "2026-10-21T10:00:00+00:00"


def setup(network="solana", prior=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    mint = "Mint" if network == "solana" else "0x" + "ab" * 20
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint,
        ticker="M", entry_price=1, entry_notional=100, entry_fill_source="chain",
        entry_lag_s=0, their_size_usd=100, opened_at=NOW, strategy="alpha_b",
        execution_mode="real", network_id=network)
    m.insert_fill(c, position_id=pid, kind="entry", price=1, qty=100, notional=100,
                  fill_source="chain", fraction=1, created_at=NOW, tx_sig="entry")
    if prior:
        m.set_position_derisked(c, pid, prior)
        m.insert_fill(c, position_id=pid, kind="derisk", price=1, qty=10, notional=10,
                      fill_source="chain", fraction=.1, created_at=prior, tx_sig="old")
    return c, pid


def request(c, pid, signature="source1", sold="1", pre="4", **extra):
    iid = xi.enqueue_source_partial(c, position_id=pid, network_id=m.get_position(c,pid)["network_id"],
        source_owner="owner", mint=m.get_position(c,pid)["mint"], source_signature=signature, source_event_index=0,
        source_sold_atomic=sold, source_pre_atomic=pre, now_iso=NOW, **extra)
    return dict(c.execute("SELECT * FROM memecoin_manual_exit_intents WHERE intent_id=?",(iid,)).fetchone())


def execute(c, pid, req, *, atomic=1000000, status="confirmed", sig="ours1", send=None, evm=False,
            receipt=None, snapshot=None, sign=None):
    data = json.loads(req["request_json"])
    part = atomic * int(data["source_sold_atomic"]) // int(data["source_pre_atomic"])
    seen = []
    def quote(*args, atomic=None, **kwargs):
        seen.append(atomic)
        return {"inAmount":str(atomic), "sellAmount":str(atomic), "outAmount":"25000000",
                "otherAmountThreshold":"20000000", "minBuyAmount":"20000000",
                "transaction":{"to":"router"}}
    kw = dict(quote_fn=quote, sign_fn=sign or (lambda q:("bytes",sig,1) if evm else ("bytes",sig)),
        send_fn=send or (lambda _:None), confirm_fn=lambda _:status, usdc_fn=lambda:100.,
        token_balance_fn=lambda _:atomic/10000, token_snapshot_fn=snapshot or (lambda _:TokenBalanceSnapshot(atomic,4)),
        slippage_ladder=[3], sleep_fn=lambda _:None, source_request=req,
        receipt_fill_fn=receipt or (lambda *_:{"sold_atomic":part,"decimals":4,"received":25.}))
    if evm:
        kw.update(approve_fn=lambda *_:True)
        result = execute_partial_exit_evm(c,m.get_position(c,pid),.25,NOW,**kw)
    else:
        kw.update(receipt_first=True)
        result = execute_partial_exit(c,m.get_position(c,pid),.25,NOW,**kw)
    return result, seen


def test_request_identity_is_durable_and_stronger_proof_cannot_duplicate():
    c,pid=setup()
    a=request(c,pid,source_proof={"feed":"processed"})
    b=request(c,pid,source_proof={"feed":"confirmed"})
    assert a["intent_id"]==b["intent_id"]
    assert c.execute("SELECT count(*) FROM memecoin_manual_exit_intents").fetchone()[0]==1
    with pytest.raises(ValueError): request(c,pid,sold="2")
    with pytest.raises(ValueError): request(c,pid,signature="bad",sold=True)


@pytest.mark.parametrize("evm",[False,True])
def test_two_distinct_trims_use_new_owned_remainder_and_preserve_oneshot(evm):
    c,pid=setup("4663" if evm else "solana",prior=NOW)
    a,_=execute(c,pid,request(c,pid),atomic=900000,sig="ours1",evm=evm)
    b,_=execute(c,pid,request(c,pid,"source2"),atomic=675000,sig="ours2",evm=evm)
    assert a["status"]==b["status"]=="partial_sold"
    p=m.get_position(c,pid)
    assert p["is_open"]==1 and p["derisked_at"]==NOW
    assert p["derisk_claim_id"] is None and p["derisk_sig"] is None
    assert m.derisk_proceeds(c,pid)==60


def test_exact_atoms_above_float_integer_precision_reach_quote():
    c,pid=setup(); req=request(c,pid,sold="1",pre="3")
    atomic=2**62+19
    result,seen=execute(c,pid,req,atomic=atomic)
    assert result["status"]=="partial_sold"
    assert seen==[atomic//3]
    row=c.execute("SELECT intent_json FROM memecoin_exit_intents").fetchone()
    assert json.loads(row[0])["sell_amount_atomic"]==atomic//3


@pytest.mark.parametrize('evm',[False,True])
def test_a_partial_ratio_near_one_does_not_round_into_a_full_sale(evm):
    c,pid=setup('4663' if evm else 'solana')
    pre=2**62+1;atomic=2**62+7
    req=request(c,pid,sold=str(pre-1),pre=str(pre))
    result,seen=execute(c,pid,req,atomic=atomic,evm=evm)
    assert result['status']=='partial_sold'
    assert seen==[atomic*(pre-1)//pre] and seen[0]<atomic


def test_unknown_second_partial_is_recoverable_and_excludes_other_executors():
    c,pid=setup(prior=NOW)
    execute(c,pid,request(c,pid),sig="first")
    req=request(c,pid,"source2")
    result,_=execute(c,pid,req,status="pending",sig="second")
    assert result["status"]=="unknown"
    p=m.get_position(c,pid)
    assert p["derisk_pending_at"] and p["derisk_sig"]=="second"
    assert p["derisked_at"]==NOW
    assert m.claim_position_close(c,pid,LATER,"real_tp") is None
    assert m.claim_position_derisk(c,pid,LATER) is None
    assert m.stale_derisk_pendings(c,LATER)==[]
    assert m.unverified_derisks(c)==[]
    xi.expire_stale(c,now_iso=LATER)
    assert xi.read_status(c,req["intent_id"])["state"] not in ("queued","expired")
    def partial(*_): pass
    partial.recovery_lock=lambda:nullcontext(True)
    ctx={"solana":{"source_partial_fn":partial,"confirm_once_fn":lambda _:"confirmed",
        "source_partial_receipt_fn":lambda *_:{"sold_atomic":250000,"decimals":4,"received":24.}}}
    runner.recover_source_partials(c,ctx,LATER)
    assert m.get_position(c,pid)["derisk_sig"] is None
    assert xi.read_status(c,req["intent_id"])["result_status"]=="partial_sold"
    assert m.derisk_proceeds(c,pid)==59


def test_signed_identity_survives_crash_and_missing_receipt_never_releases():
    c,pid=setup();req=request(c,pid)
    def crash(_): raise RuntimeError("after identity")
    with pytest.raises(RuntimeError,match="after identity"):
        execute(c,pid,req,send=crash)
    p=m.get_position(c,pid)
    intent=json.loads(c.execute("SELECT intent_json FROM memecoin_exit_intents").fetchone()[0])
    assert p["derisk_sig"]=="ours1" and intent["signed_payload_b64"]
    assert p["derisked_at"] is None
    assert execute(c,pid,req)[0]["status"]=="partial_pending"
    def partial(*_): pass
    partial.recovery_lock=lambda:nullcontext(True)
    ctx={"solana":{"source_partial_fn":partial,"confirm_once_fn":lambda _:"confirmed",
                   "source_partial_receipt_fn":lambda *_:None}}
    runner.recover_source_partials(c,ctx,LATER)
    assert m.get_position(c,pid)["derisk_sig"]=="ours1"


def test_failed_receipt_requeues_exact_request_without_resetting_prior_oneshot():
    c,pid=setup(prior=NOW);req=request(c,pid)
    execute(c,pid,req,status="pending")
    def partial(*_): pass
    partial.recovery_lock=lambda:nullcontext(True)
    runner.recover_source_partials(c,{"solana":{"source_partial_fn":partial,
        "confirm_once_fn":lambda _:"failed"}},LATER)
    assert m.get_position(c,pid)["derisked_at"]==NOW
    assert xi.read_status(c,req["intent_id"])["state"]=="queued"


@pytest.mark.parametrize('evm',[False,True])
def test_zero_atom_trim_finishes_its_journal_without_a_fill_or_policy_change(evm):
    c,pid=setup('4663' if evm else 'solana',prior=NOW);req=request(c,pid,sold='1',pre='100')
    result,seen=execute(c,pid,req,atomic=1,evm=evm)
    assert result['status']=='partial_noop' and seen==[]
    state=xi.read_status(c,req['intent_id'])
    assert state['state']=='done' and state['result_status']=='rounded_to_zero'
    pos=m.get_position(c,pid)
    assert pos['derisked_at']==NOW and pos['derisk_claim_id'] is None and pos['derisk_pending_at'] is None
    assert c.execute('SELECT count(*) FROM memecoin_fills WHERE position_id=?',(pid,)).fetchone()[0]==2


@pytest.mark.parametrize('evm',[False,True])
def test_zero_atom_trim_does_not_block_a_later_full_source_sale(evm):
    c,pid=setup('4663' if evm else 'solana');trim=request(c,pid,sold='1',pre='100')
    execute(c,pid,trim,atomic=1,evm=evm)
    full=request(c,pid,'full',sold='100',pre='100');seen=[]
    def close(conn,pos,req,now):
        seen.append(req['intent_id'])
        assert req['intent_id']==full['intent_id'] and m.claim_position_close(conn,pid,now,'mirror')
        m.close_position(conn,pid,now,0,'mirror')
        return {'status':'closed'}
    xi.drain_source_partials(c,close,now_fn=lambda:NOW,position_fn=m.get_position)
    assert seen==[full['intent_id']] and xi.read_status(c,full['intent_id'])['state']=='done'


def test_zero_atom_completion_requires_the_exact_unsigned_claim():
    c,pid=setup();req=request(c,pid);claim=m.claim_source_partial(c,pid,req['intent_id'],NOW)
    assert not m.finish_source_partial_noop(c,pid,'other-claim',NOW)
    assert xi.read_status(c,req['intent_id'])['state']=='claimed'
    assert m.release_source_partial(c,pid,claim)
    execute(c,pid,req,status='pending')
    claim=m.get_position(c,pid)['derisk_claim_id']
    assert not m.finish_source_partial_noop(c,pid,claim,NOW)
    assert m.get_position(c,pid)['derisk_sig']=='ours1'


@pytest.mark.parametrize('evm',[False,True])
def test_wrong_receipt_or_lost_booking_never_reports_a_completed_partial(monkeypatch, evm):
    c,pid=setup('4663' if evm else 'solana');req=request(c,pid)
    result,_=execute(c,pid,req,evm=evm,
        receipt=lambda *_:{'sold_atomic':1,'decimals':4,'received':25})
    assert result['status']=='unknown' and m.get_position(c,pid)['derisk_sig']
    c,pid=setup('4663' if evm else 'solana');req=request(c,pid)
    monkeypatch.setattr(m,'book_source_partial_fill',lambda *a,**kw:False)
    result,_=execute(c,pid,req,evm=evm)
    assert result['status']=='partial_pending'


@pytest.mark.parametrize('evm',[False,True])
def test_even_one_atom_balance_change_supersedes_before_signing(evm):
    c,pid=setup('4663' if evm else 'solana');req=request(c,pid)
    raw=iter([1000000,999999])
    result,_=execute(c,pid,req,evm=evm,snapshot=lambda _:TokenBalanceSnapshot(next(raw),4),
                     sign=lambda _:pytest.fail('changed balance reached signing'))
    assert result['status']=='superseded'
    assert m.get_position(c,pid)['derisk_claim_id'] is None


def test_source_request_is_not_taken_or_expired_by_the_manual_worker():
    c,pid=setup();req=request(c,pid)
    assert xi.claim_next(c,now_iso=NOW) is None
    assert xi.pending_count(c)==0 and xi.source_pending_count(c)==1
    xi.expire_stale(c,now_iso=LATER)
    assert xi.read_status(c,req['intent_id'])['state']=='queued'


def test_full_and_partial_claims_exclude_a_later_source_request():
    for kind in ('close','derisk'):
        c,pid=setup();req=request(c,pid)
        claim=m.claim_position_close(c,pid,NOW,'real_tp') if kind=='close' else m.claim_position_derisk(c,pid,NOW)
        assert claim
        result,seen=execute(c,pid,req,snapshot=lambda _:pytest.fail('competing owner reached balance read'))
        assert result['status']=='partial_pending' and seen==[]


def test_stale_unsigned_source_claim_is_recovered_under_the_executor_lock():
    c,pid=setup();req=request(c,pid)
    assert m.claim_source_partial(c,pid,req['intent_id'],NOW)
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    runner.recover_source_partials(c,{'solana':{'source_partial_fn':partial}},LATER)
    assert xi.read_status(c,req['intent_id'])['state']=='queued'
    assert m.get_position(c,pid)['derisk_claim_id'] is None


@pytest.mark.parametrize('hold',['mirror_disabled','tracking_paused_at'])
def test_queued_source_request_and_atomic_claim_respect_a_fresh_operator_hold(hold):
    c,pid=setup();req=request(c,pid)
    c.execute(f'UPDATE memecoin_positions SET {hold}=? WHERE id=?',(1 if hold=='mirror_disabled' else NOW,pid));c.commit()
    assert m.claim_source_partial(c,pid,req['intent_id'],NOW) is None
    xi.drain_source_partials(c,lambda *_:pytest.fail('held source sale entered executor'),
                            now_fn=lambda:NOW,position_fn=m.get_position)
    assert xi.read_status(c,req['intent_id'])['state']=='queued'


def test_mirror_disable_race_also_blocks_full_source_claim_but_not_tp():
    c,pid=setup()
    c.execute('UPDATE memecoin_positions SET mirror_disabled=1 WHERE id=?',(pid,));c.commit()
    assert m.claim_position_close(c,pid,NOW,'mirror') is None
    assert m.claim_position_close(c,pid,NOW,'real_tp')


def test_deferred_earlier_trim_keeps_a_later_partial_request_queued():
    c,pid=setup();a=request(c,pid);b=request(c,pid,'next',sold='1',pre='4')
    calls=[]
    xi.drain_source_partials(c,lambda conn,pos,req,now:calls.append(req['intent_id']) or {'status':'deferred'},
                            now_fn=lambda:NOW,position_fn=m.get_position)
    assert calls==[a['intent_id']]
    assert xi.read_status(c,b['intent_id'])['state']=='queued'


def test_later_full_sale_supersedes_a_queued_unquotable_trim_without_cancelling_it():
    c,pid=setup();trim=request(c,pid)
    xi.drain_source_partials(c,lambda *_:{'status':'partial_failed','reason':'route_dust'},
                            now_fn=lambda:NOW,position_fn=m.get_position)
    full=request(c,pid,'full',sold='4',pre='4');seen=[]
    def close(conn,pos,req,now):
        seen.append(req['intent_id'])
        assert req['intent_id']==full['intent_id'] and m.claim_position_close(conn,pid,now,'mirror')
        m.close_position(conn,pid,now,0,'mirror')
        return {'status':'closed'}
    xi.drain_source_partials(c,close,now_fn=lambda:NOW,position_fn=m.get_position,max_per_pass=1)
    assert seen==[full['intent_id']] and xi.read_status(c,trim['intent_id'])['state']=='queued'
    xi.drain_source_partials(c,close,now_fn=lambda:LATER,position_fn=m.get_position)
    assert xi.read_status(c,trim['intent_id'])['result_status']=='already_closed'


def test_a_failed_full_request_keeps_earlier_unsigned_trims_durable():
    c,pid=setup();trim=request(c,pid);full=request(c,pid,'full',sold='4',pre='4');seen=[]
    xi.drain_source_partials(c,lambda conn,pos,req,now:seen.append(req['intent_id']) or
        {'status':'deferred','reason':'unquotable'},now_fn=lambda:NOW,position_fn=m.get_position)
    assert seen==[full['intent_id']]
    assert xi.read_status(c,trim['intent_id'])['state']==xi.read_status(c,full['intent_id'])['state']=='queued'
    assert m.get_position(c,pid)['is_open']==1


@pytest.mark.parametrize('signed',[False,True])
def test_later_full_sale_cannot_bypass_a_claimed_or_signed_unknown_trim(signed):
    c,pid=setup();trim=request(c,pid)
    if signed:execute(c,pid,trim,status='pending')
    else:assert m.claim_source_partial(c,pid,trim['intent_id'],NOW)
    full=request(c,pid,'full',sold='4',pre='4')
    xi.drain_source_partials(c,lambda *_:pytest.fail('full bypassed existing ownership'),
                            now_fn=lambda:NOW,position_fn=m.get_position)
    assert xi.read_status(c,full['intent_id'])['state']=='queued'


def test_journal_position_identity_is_rechecked_before_quote():
    c,pid=setup();req=request(c,pid)
    c.execute("UPDATE memecoin_positions SET mint='Other' WHERE id=?",(pid,));c.commit()
    result,seen=execute(c,pid,req)
    assert result['status']=='partial_failed' and seen==[]


@pytest.mark.parametrize('evm',[False,True])
def test_real_venue_factory_exposes_source_callback_with_exact_quote_atoms(monkeypatch, evm):
    from sniper.execution import solana_exit_executor as sx, evm_exit_executor as ex
    from sniper.execution import solana_real_seams as ss, evm_real_seams as es
    from solders.keypair import Keypair
    from eth_account import Account
    seen=[]
    def core(conn, pos, fraction, now, **kw):
        assert kw['source_request']['intent_id']==req['intent_id']
        quote_fn=kw['quote_fn']
        if evm: quote_fn('Mint',100,3,fraction,atomic=2**62+7)
        else: quote_fn('Mint',100,3,atomic=2**62+7)
        assert callable(kw['token_snapshot_fn']) and callable(kw['receipt_fill_fn'])
        return {'status':'partial_failed'}
    monkeypatch.setattr(ex if evm else sx,'execute_partial_exit_evm' if evm else 'execute_partial_exit',core)
    if evm:
        monkeypatch.setattr(es,'quote_usd_price',lambda *a,**k:2000.)
        monkeypatch.setattr(es,'venue_quote_with_v2_fallback',lambda primary,**kw:primary)
        venue=es.build_real_evm_executor(acct=Account.create(),chain_id='4663',rpc_url='rpc',
            usdc_fn=lambda:100,token_fn=lambda _:100,
            venue_quote_fn=lambda mint,buy,atomic,**kw:seen.append(atomic) or {})
    else:
        monkeypatch.setattr(ss,'jupiter_exit_quote',lambda mint,atomic,*a,**k:seen.append(atomic) or {})
        venue=ss.build_real_solana_executor(keypair=Keypair(),rpc_url='rpc',
            usdc_fn=lambda:100,token_fn=lambda _:100)
    c,pid=setup('4663' if evm else 'solana');req=request(c,pid)
    venue['source_partial_fn'](c,m.get_position(c,pid),req,NOW)
    assert seen==[2**62+7]
    assert callable(venue['source_partial_receipt_fn'])


def test_actual_evm_receipt_callback_gets_bound_decimals_and_frozen_quote_price(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_real_seams as es, evm_exit_executor as ex, evm_wallet
    acct=Account.create();pool='0x'+'12'*20;quote=evm_wallet.CHAIN_QUOTE['4663']['token']
    topic=lambda address:'0x'+address[2:].lower().rjust(64,'0')
    def transfer(token, sender, recipient, amount):
        return {'address':token,'topics':[es._TRANSFER_TOPIC,topic(sender),topic(recipient)],
                'data':'0x'+hex(amount)[2:].rjust(64,'0')}
    receipt={'status':'0x1','transactionHash':'0xours','blockHash':'0xblock','logs':[
        transfer('Mint',acct.address,pool,250000),transfer(quote,pool,acct.address,10**16)]}
    def http(method,url,headers=None,body=None,**kw):
        assert json.loads(body)['method']=='eth_getTransactionReceipt'
        return 200,{'result':receipt},None
    monkeypatch.setattr(es,'_canonical_receipt_on_rpc',lambda *a,**k:True)
    price={'value':3000.};proofs=[]
    monkeypatch.setattr(es,'quote_usd_price',lambda *a,**k:price['value'])
    def core(c,pos,fraction,now,**kw):
        ledger=xi.SourcePartialExecution(c,pos,kw['source_request'])
        ledger.snapshot(lambda _:TokenBalanceSnapshot(1000000,4))
        price['value']=9999.
        proofs.append(ledger.receipt(kw['receipt_fill_fn'],'0xours','Mint',{'sellAmount':'250000'}))
        return {'status':'partial_failed'}
    monkeypatch.setattr(ex,'execute_partial_exit_evm',core)
    venue=es.build_real_evm_executor(acct=acct,chain_id='4663',rpc_url='rpc',http=http,
        usdc_fn=lambda:100,token_fn=lambda _:100)
    c,pid=setup('4663');req=request(c,pid)
    venue['source_partial_fn'](c,m.get_position(c,pid),req,NOW)
    assert proofs==[{'sold_atomic':250000,'decimals':4,'received':30.}]


@pytest.mark.parametrize('height,drop,available,released',[
    (501,0,True,True),(500,0,True,False),(501,1,True,False),(501,0,False,False)])
def test_solana_drop_recovery_requires_expiry_and_exact_confirmed_atoms(height,drop,available,released):
    c,pid=setup(prior=NOW);req=request(c,pid);atomic=2**62+19
    execute(c,pid,req,atomic=atomic,status='pending',sign=lambda _:('bytes','ours1',{'last_valid_height':500}))
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    ctx={'source_partial_fn':partial,'confirm_once_fn':lambda _:'not_found'}
    if available:ctx['source_partial_proof_snapshot_fn']=lambda _:{'block_height':height,
        'anchor_slot':700,'balance_slot':700,'balance_atomic':atomic-drop,'decimals':4}
    runner.recover_source_partials(c,{'solana':ctx},LATER)
    assert (xi.read_status(c,req['intent_id'])['state']=='queued') is released
    assert m.get_position(c,pid)['derisked_at']==NOW


@pytest.mark.parametrize('nonce,drop,deadline,stamp,released',[
    (2,0,None,10,True),(2,1,None,10,False),(1,0,None,200,False),
    (1,0,100,100,False),(1,0,100,101,True)])
def test_evm_drop_recovery_uses_same_block_raw_atoms_and_nonce_or_signed_deadline(
        monkeypatch,nonce,drop,deadline,stamp,released):
    from sniper.execution import evm_real_seams as es
    c,pid=setup('4663',prior=NOW);req=request(c,pid);atomic=2**62+19
    execute(c,pid,req,atomic=atomic,status='pending',evm=True)
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    monkeypatch.setattr(es,'decode_signed_swap_deadline',lambda _:deadline)
    monkeypatch.setattr(runner.cfg,'memecoin_evm_expiry_margin_s',lambda _:0)
    ctx={'source_partial_fn':partial,'confirm_once_fn':lambda _:'not_found',
         'source_partial_proof_snapshot_fn':lambda _:{'block':1,'ts':stamp,'nonce':nonce,
                                                    'balance_atomic':atomic-drop,'decimals':4}}
    runner.recover_source_partials(c,{'4663':ctx},LATER)
    assert (xi.read_status(c,req['intent_id'])['state']=='queued') is released
    assert m.get_position(c,pid)['derisked_at']==NOW


def test_a_signature_found_after_expiry_proof_never_rearms():
    c,pid=setup();req=request(c,pid)
    execute(c,pid,req,status='pending',sign=lambda _:('bytes','ours1',{'last_valid_height':500}))
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    statuses=iter(['not_found','confirmed'])
    seen=[]
    def confirm(sig):
        seen.append(sig)
        return next(statuses)
    ctx={'source_partial_fn':partial,'confirm_once_fn':confirm,
        'source_partial_proof_snapshot_fn':lambda _:{'block_height':501,'anchor_slot':700,
            'balance_slot':700,'balance_atomic':1000000,'decimals':4}}
    runner.recover_source_partials(c,{'solana':ctx},LATER)
    assert m.get_position(c,pid)['derisk_sig']=='ours1' and seen==['ours1','ours1']


def test_a_later_request_cannot_reuse_an_earlier_signed_transaction_identity():
    c,pid=setup();execute(c,pid,request(c,pid),sig='same')
    second=request(c,pid,'next',sold='1',pre='3')
    sent=[]
    result,_=execute(c,pid,second,atomic=750000,sig='same',send=sent.append)
    assert result['status']=='partial_pending' and sent==[]
    assert c.execute("SELECT count(*) FROM memecoin_fills WHERE tx_sig='same'").fetchone()[0]==1
    assert xi.read_status(c,second['intent_id'])['state']=='queued'


def test_reentry_cannot_reuse_a_prior_positions_signed_partial_identity():
    c,pid=setup();execute(c,pid,request(c,pid),sig='same')
    m.close_position(c,pid,NOW,0,'manual')
    newer=m.open_position(c,source_user_id='u',source_handle='h',mint='Mint',ticker='M',
        entry_price=1,entry_notional=100,entry_fill_source='chain',entry_lag_s=0,
        their_size_usd=100,opened_at=NOW,strategy='alpha_b',execution_mode='real',network_id='solana')
    req=request(c,newer,'new-source');sent=[]
    result,_=execute(c,newer,req,sig='same',send=sent.append)
    assert result['status']=='partial_pending' and sent==[]
    assert c.execute("SELECT count(*) FROM memecoin_fills WHERE position_id=?",(newer,)).fetchone()[0]==0
    assert xi.read_status(c,req['intent_id'])['state']=='queued'


@pytest.mark.parametrize('kind',['close','derisk'])
def test_a_later_full_or_legacy_partial_cannot_rebook_a_source_partial_signature(kind):
    from sniper.execution import solana_exit_executor as sx
    c,pid=setup();execute(c,pid,request(c,pid,sold='1',pre='2'),sig='same')
    sent=[]
    kw=dict(quote_fn=lambda *a,**k:{'inAmount':'500000','outAmount':'25000000',
            'otherAmountThreshold':'20000000'},sign_fn=lambda _:('bytes','same'),send_fn=sent.append,
            confirm_fn=lambda _:'confirmed',usdc_fn=lambda:100,token_balance_fn=lambda _:100,
            slippage_ladder=[3],sleep_fn=lambda _:None,receipt_first=True,
            receipt_fill_fn=lambda *_:{'sold_atomic':500000,'decimals':4,'received':25})
    if kind=='close':
        kw.update(token_snapshot_fn=lambda _:TokenBalanceSnapshot(500000,4))
        result=sx.execute_exit(c,m.get_position(c,pid),'mirror',NOW,**kw)
    else:
        result=sx.execute_partial_exit(c,m.get_position(c,pid),.5,NOW,**kw)
    assert sent==[] and result['status'] in ('already_closing','already_derisked')
    assert m.get_position(c,pid)['is_open']==1
    assert c.execute("SELECT count(*) FROM memecoin_fills WHERE tx_sig='same'").fetchone()[0]==1


def test_signature_guard_index_migration_tolerates_malformed_legacy_json():
    c,pid=setup()
    c.execute('DROP INDEX IF EXISTS idx_mc_exit_intent_tx_identity')
    c.execute("INSERT INTO memecoin_exit_intents(claim_id,position_id,action,intent_json) VALUES(?,?,?,?)",
              ('legacy',pid,'derisk','{garbage'))
    c.commit();m.ensure_memecoin_tables(c)
    sql="SELECT 1 FROM memecoin_exit_intents WHERE (CASE WHEN json_valid(intent_json) " \
        "THEN json_extract(intent_json,'$.tx_identity') END)=? AND claim_id<>? LIMIT 1"
    plan=c.execute('EXPLAIN QUERY PLAN '+sql,('same','current')).fetchall()
    assert any('idx_mc_exit_intent_tx_identity' in row[3] and 'SEARCH' in row[3] for row in plan)
    assert c.execute(sql,('same','current')).fetchone() is None


def test_recovery_probes_every_active_partial_instead_of_starving_the_twenty_first():
    c,pid=setup()
    for index in range(21):
        if index:
            pid=m.open_position(c,source_user_id='u',source_handle='h',mint='Mint'+str(index),
                ticker='M',entry_price=1,entry_notional=100,entry_fill_source='chain',
                entry_lag_s=0,their_size_usd=100,opened_at=NOW,strategy='alpha_b',
                execution_mode='real',network_id='solana')
        execute(c,pid,request(c,pid,'source'+str(index)),status='pending',sig='ours'+str(index))
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    seen=[]
    def confirm(sig):
        seen.append(sig)
        return 'pending'
    runner.recover_source_partials(c,{'solana':{'source_partial_fn':partial,
                                               'confirm_once_fn':confirm}},LATER)
    assert len(seen)==21 and 'ours20' in seen


def test_chain_pinned_evm_proof_keeps_original_atomic_integer_above_float_precision(monkeypatch):
    from sniper.execution import evm_real_seams as es
    owner='0x'+'11'*20;mint='0x'+'ab'*20;atomic=2**62+19
    calls=[]
    def rpc(url,method,params,**_):
        calls.append((method,params))
        if method=='eth_getBlockByNumber':
            return {'result':{'number':'0x64','timestamp':'0x123'}}
        if method=='eth_getTransactionCount':
            assert params==[owner,'0x64']
            return {'result':'0x2'}
        assert method=='eth_call' and params[0]['to']==mint and params[1]=='0x64'
        return {'result':'0x'+format(atomic,'064x')}
    monkeypatch.setattr(es,'_rpc',rpc)
    snap=es.eth_proof_snapshot(owner,mint,'rpc',decimals_fn=lambda _:4,include_atomic=True)
    assert snap['balance_atomic']==atomic and type(snap['balance_atomic']) is int
    assert snap['decimals']==4 and snap['nonce']==2 and snap['block']==100 and snap['ts']==0x123
    assert len(calls)==3


@pytest.mark.parametrize('balance_slot',[699,700,701,None,True])
def test_solana_dead_proof_requires_bag_context_at_least_the_confirmed_epoch_bank(balance_slot):
    from sniper.execution import solana_real_seams as ss
    atomic=2**62+19;calls=[]
    def http(method,url,body=None,**_):
        call=json.loads(body);calls.append(call['method'])
        assert url=='funded-rpc'
        if call['method']=='getEpochInfo':
            assert call['params']==[{'commitment':'confirmed'}]
            return 200,{'result':{'blockHeight':501,'absoluteSlot':700}},None
        assert call['method']=='getTokenAccountsByOwner'
        assert call['params']==['owner',{'mint':'Mint'},
            {'encoding':'jsonParsed','commitment':'confirmed','minContextSlot':700}]
        return 200,{'result':{'context':{'slot':balance_slot},'value':[{'pubkey':'account','account':{
            'owner':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','data':{
            'parsed':{'type':'account','info':{'owner':'owner','mint':'Mint',
                              'tokenAmount':{'amount':str(atomic),'decimals':4}}}}}}]}},None
    proof=ss.source_partial_proof_snapshot('owner','Mint','funded-rpc',http=http)
    if type(balance_slot) is int and balance_slot>=700:
        assert proof=={'block_height':501,'anchor_slot':700,'balance_slot':balance_slot,
                       'balance_atomic':atomic,'decimals':4}
    else:
        assert proof is None
    assert calls==['getEpochInfo','getTokenAccountsByOwner']


def test_default_solana_balance_snapshot_keeps_existing_rpc_options():
    from sniper.execution import solana_wallet as sw
    def http(method,url,body=None,**_):
        assert json.loads(body)['params']==['owner',{'mint':'Mint'},
            {'encoding':'jsonParsed','commitment':'confirmed'}]
        return 200,{'result':{'value':[]}},None
    snap=sw.token_balance_snapshot('owner','Mint','rpc',http=http)
    assert snap.atomic==0 and snap.decimals is None and snap.context_slot is None


def test_unwatermarked_solana_balance_never_proves_a_missing_partial_dead():
    c,pid=setup();req=request(c,pid)
    execute(c,pid,req,status='pending',sign=lambda _:('bytes','ours1',{'last_valid_height':500}))
    def partial(*_):pass
    partial.recovery_lock=lambda:nullcontext(True)
    ctx={'source_partial_fn':partial,'confirm_once_fn':lambda _:'not_found',
         'block_height_fn':lambda:501,'source_partial_snapshot_fn':lambda _:TokenBalanceSnapshot(1000000,4)}
    runner.recover_source_partials(c,{'solana':ctx},LATER)
    assert m.get_position(c,pid)['derisk_sig']=='ours1'
