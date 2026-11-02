"""Native economic transfers through known services cannot become group ancestry."""
import copy

import pytest

import test_known_group_canary as C
from test_known_group_canary_worker import transfer
from sniper.services.wallet_identity import verified_sol_funding

SERVICE='4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey'


def service_path(address):
    proof=C.proof();template=proof['funding_path'][0]
    first=transfer(C.H,address,signature=31,slot=90,block_time=int(C.R)-30,amount=1_000_000)
    last=transfer(address,C.DEV,signature=32,slot=99,block_time=int(C.R)-10,amount=10_000_000)
    assert verified_sol_funding(first,C.iso(C.R))[0]['amount_atomic']=='1000000'
    assert verified_sol_funding(last,C.iso(C.R))[0]['amount_atomic']=='10000000'
    proof['funding_path']=[dict(template,transaction=first,slot=90,proof_ref='native:root-service',recipient=address,known_at_host=C.R-20),
        dict(template,transaction=last,slot=99,proof_ref='native:service-developer',known_at_host=C.R-1)]
    return proof


def test_native_tip_service_both_sides_of_funding_hops_refuses_without_rejecting_nonservice(monkeypatch):
    """Prior root funding a tip sink does not identify that sink's later customer as group developer."""
    k=C.service(monkeypatch)
    assert k.evaluate_candidate(C.proof(),now_host=C.R+.5,now_iso=C.iso(C.R+.5))['eligible']
    out=k.evaluate_candidate(service_path(SERVICE),now_host=C.R+.5,now_iso=C.iso(C.R+.5))
    assert out['eligible'] is False and out['reason']=='known_group_service_hop',out


@pytest.mark.parametrize('kind',['exchange','program'])
def test_known_exchange_and_executable_program_hops_refuse(monkeypatch,kind):
    k=C.service(monkeypatch)
    if kind=='exchange':
        from sniper.services.memecoin_cluster_rpc import known_funders
        address=sorted(known_funders()['addresses'])[0]
    else:address=C.PUMP
    out=k.evaluate_candidate(service_path(address),now_host=C.R+.5,now_iso=C.iso(C.R+.5))
    assert out['eligible'] is False and out['reason']=='known_group_service_hop',out


@pytest.mark.parametrize('role',['funded','genesis'])
def test_service_address_cannot_enter_extra_frozen_member_roles(monkeypatch,role):
    import base58
    from test_known_group_canary_worker import compile_native
    from sniper.office.wallet_stream_proof import confirmed_receipt
    k=C.service(monkeypatch);proof=C.proof()
    payment=transfer(C.H,SERVICE,signature=33,slot=98,block_time=int(C.R)-5,amount=10_000_000)
    if role=='funded':
        funding=dict(proof['funding_path'][0],transaction=payment,slot=98,proof_ref='native:service-member')
        member=dict(address=SERVICE,role=role,proof_ref=funding['proof_ref'],known_at_host=C.R-1,funding=funding)
    else:
        genesis=C.creation();keys=genesis['transaction']['message']['accountKeys']
        keys.insert(2,dict(pubkey=SERVICE,signer=True,writable=True))
        genesis['transaction']['signatures']=[base58.b58encode(bytes([n])*64).decode() for n in (37,38,39)]
        del genesis['transaction']['message']['instructions'][0]
        genesis['transactionIndex']=2
        index=next(i for i,key in enumerate(keys) if key['pubkey']==C.MINT_ACCOUNT)
        balance=dict(accountIndex=index,mint=C.MINT,owner=SERVICE,programId=C.TOKEN,
            uiTokenAmount=dict(amount='600000000000000',decimals=6))
        genesis['meta'].update(preTokenBalances=[balance],postTokenBalances=[dict(balance,uiTokenAmount=dict(amount='1200000000000000',decimals=6))])
        native=confirmed_receipt(compile_native(genesis),commitment='confirmed',block_time=int(C.R)-1)
        member=dict(address=SERVICE,role=role,proof_ref=native['transaction']['signatures'][0],known_at_host=C.R+.2,
            transaction=native,commitment='confirmed')
        proof['tradable']['pool_state']['supply_atomic']='1200000000000000'
    proof['members'].append(member)
    out=k.evaluate_candidate(proof,now_host=C.R+.5,now_iso=C.iso(C.R+.5))
    assert not out['eligible'] and out['reason'].startswith('known_group_service'),out


def seed_old_service_preparation(s,tx,receipt,flow_id):
    """Simulate a prior observer version's persisted native path at its actual pre-create commit cut."""
    import hashlib
    from sniper.persistence import db_memecoin as db
    from sniper.office.wallet_stream_proof import confirmed_receipt
    from test_known_group_canary_worker import compile_native,state
    update=compile_native(tx);parsed=confirmed_receipt(update,commitment='confirmed',block_time=tx['blockTime'])
    signature=parsed['transaction']['signatures'][0];ref=f'{s.path.resolve()}:{flow_id}:{signature}'
    clue,=verified_sol_funding(parsed,C.iso(s.clock[0]))
    db.store_known_group_evidence(s.worker.observer,ref,dict(version='known-group-native-profile-v1',parsed=parsed,
        compiled=s.module._compiled_profile(update,parsed)))
    row,snapshot=state(s)
    path=snapshot['funding'].get(clue['funder'],[])
    hop=dict(source='host',proof_state='verified',proof_kind='research_stream',method='wallet-watch-stream-v1',proof_ref=ref,
        known_at_host=s.clock[0],transaction_ref=ref,commitment='confirmed',slot=tx['slot'],tx_index=tx['transactionIndex'],
        funder=clue['funder'],recipient=clue['recipient'],amount_atomic=clue['amount_atomic'],
        raw_sha256=hashlib.sha256(update.SerializeToString()).hexdigest(),block_time=tx['blockTime'])
    snapshot['funding'][clue['recipient']]=path+[hop]
    db.save_known_group_tail(s.worker.observer,source=str(s.path.resolve()),identity=row['identity'],cursor=row['cursor'],
        anchor=row['anchor'],baseline=row['baseline'],started_at=row['started_at'],last_host=s.clock[0],state=snapshot)


# Public fixtures remain shared with the native-flow contract; all DBs are temporary.
from test_known_group_canary import ledger,knobs
from test_known_group_canary_worker import source,started,push,heartbeat,state


def test_persisted_native_service_path_cannot_qualify_after_observer_restart(source,monkeypatch):
    from test_known_group_canary_live_flow import healthy_market_and_source,close_market
    s=healthy_market_and_source(source,monkeypatch,relay=SERVICE,on_funding=seed_old_service_preparation,restart=True)
    try:
        out=s.worker.tick()
        assert out['eligible']==0,out
        assert s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0
        _,saved=state(s)
        assert all(not any(hop['funder']==SERVICE or hop['recipient']==SERVICE for hop in path) for path in saved['funding'].values())
    finally:close_market(s)



def prior_format_metadata(conn,iid):
    """Consistent pre-fix candidate, queue metadata and hashes; no hash-mismatch refusal shortcut."""
    import json
    from sniper.services.memecoin_funded_policy import canonical_funded_contract
    row=conn.execute('SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?',(iid,)).fetchone()
    meta=json.loads(row[0]);meta['known_group_proof'].pop('service_catalogue',None)
    proof_json,proof_hash=canonical_funded_contract(meta['known_group_proof']);meta['snapshot_hash']=proof_hash
    candidate=meta['opportunity_id']
    request=json.loads(conn.execute('SELECT request_json FROM memecoin_known_group_candidates WHERE candidate_id=?',(candidate,)).fetchone()[0])
    request['desk_metadata']=meta
    request_json,_=canonical_funded_contract(request);metadata_json,_=canonical_funded_contract(meta)
    conn.execute('UPDATE memecoin_known_group_candidates SET proof_json=?,proof_hash=?,request_json=? WHERE candidate_id=?',
        (proof_json,proof_hash,request_json,candidate))
    conn.execute('UPDATE memecoin_manual_intents SET desk_metadata_json=? WHERE intent_id=?',(metadata_json,iid));conn.commit()
    assert C.canonical_hash(meta['known_group_proof'])==meta['snapshot_hash']
    return meta


def test_unknown_catalogue_blocks_entries_and_legacy_frozen_position_still_observes_native_sell(source,monkeypatch):
    import json
    s=started(source,monkeypatch);sell=C.sell_fixture(monkeypatch);C.enable(s.k,s.ledger);proof=C.proof()
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_funded_policy import canonical_funded_contract
    iid=C.enqueue(s.k,s.ledger,proof);meta=prior_format_metadata(s.ledger,iid)
    body={'source':{'desk':{**meta,'ticket_id':meta['opportunity_id'],'rails':{'tp_pct':40.,'sl_pct':30.}}}}
    payload,_=canonical_funded_contract(body)
    pid=db.open_position(s.ledger,source_user_id=None,source_handle='legacy-fixture',mint=C.MINT,ticker='KG',entry_price=.0001,
        entry_notional=25,entry_fill_source='chain',entry_lag_s=0,their_size_usd=0,opened_at=C.iso(C.R+.6),execution_mode='real',
        network_id='solana',signal_source='desk',funded_contract_json=payload)
    def unavailable():raise OSError('synthetic frozen local catalogue unavailable')
    monkeypatch.setattr(s.k,'_known_services',unavailable,raising=False)
    out=s.k.evaluate_candidate(proof,now_host=C.R+.5,now_iso=C.iso(C.R+.5))
    assert not out['eligible'] and out['reason']=='known_group_service_catalogue_unknown',out
    with pytest.raises(ValueError):s.k.enqueue_candidate(s.ledger,proof,now_host=C.R+.5,now_iso=C.iso(C.R+.5))
    s.clock[0]=C.R+.9;assert s.worker.tick()['state']=='baseline'
    s.clock[0]=C.R+1.3;heartbeat(s);push(s,sell['transaction'],C.R+1)
    tick=s.worker.tick();assert tick['eligible']==0
    close=s.ledger.execute('SELECT position_id,action FROM memecoin_manual_exit_intents').fetchall()
    assert len(close)==1 and close[0]['position_id']==pid and close[0]['action']=='known_group_close'


def test_pre_fix_compact_without_catalogue_witness_cannot_reserve_even_when_policy_enabled(monkeypatch,ledger):
    import json
    from sniper.persistence import db_memecoin as db
    k=C.service(monkeypatch);C.enable(k,ledger);proof=C.proof();iid=C.enqueue(k,ledger,proof)
    meta=prior_format_metadata(ledger,iid)
    contract=json.dumps({'source':{'desk':{**meta,'ticket_id':iid,'rails':{'tp_pct':40.,'sl_pct':30.}}}})
    assert db.reserve_pending(ledger,mint=C.MINT,source_user_id=None,strategy='control',notional=25,reserved_at=C.iso(C.R+.6),
        network_id='solana',signal_source='desk',funded_contract_json=contract) is None
