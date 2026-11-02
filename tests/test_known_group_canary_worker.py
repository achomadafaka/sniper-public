"""Raw capture -> private observer -> funded queue acceptance; no provider or signer."""
import copy
import importlib
import json
import struct
import time
from types import SimpleNamespace

import base58
import pytest

import test_known_group_canary as C
from test_known_group_canary import ledger, knobs
from test_wallet_flow_capture import W, worker as capture_worker
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.persistence import db_memecoin as db
from sniper.office.wallet_stream_proof import confirmed_receipt


def tail_module():
    return importlib.import_module('sniper.services.memecoin_known_group_tail')


def compile_native(tx):
    """Compile saved RPC evidence into an actual provider protobuf; preserve execution metadata."""
    u=pb.SubscribeUpdate();u.filters.append('wallet_flow');u.transaction.slot=tx['slot']
    info=u.transaction.transaction;info.index=tx.get('transactionIndex',1)
    info.transaction.signatures.extend(base58.b58decode(s) for s in tx['transaction']['signatures'])
    info.signature=info.transaction.signatures[0]
    source=tx['transaction']['message'];msg=info.transaction.message
    rows=copy.deepcopy(source['accountKeys']);keys=[k if isinstance(k,str) else k['pubkey'] for k in rows]
    if rows and isinstance(rows[0],dict):
        for ix in source['instructions']:
            if ix.get('programId') not in keys:
                rows.append(dict(pubkey=ix['programId'],signer=False,writable=False));keys.append(ix['programId'])
    if 'header' in source:
        h=source['header'];n=h['numRequiredSignatures'];rs=h['numReadonlySignedAccounts'];ru=h['numReadonlyUnsignedAccounts']
    else:
        n=sum(k['signer'] for k in rows);rs=sum(k['signer'] and not k.get('writable',True) for k in rows);ru=sum(not k['signer'] and not k.get('writable',True) for k in rows)
    msg.header.num_required_signatures=n;msg.header.num_readonly_signed_accounts=rs;msg.header.num_readonly_unsigned_accounts=ru
    msg.account_keys.extend(base58.b58decode(k) for k in keys);msg.recent_blockhash=base58.b58decode(source.get('recentBlockhash',C.SYSTEM))
    lookups=source.get('addressTableLookups',[]);msg.versioned=bool(lookups)
    for row in lookups:
        item=msg.address_table_lookups.add();item.account_key=base58.b58decode(row['accountKey'])
        item.writable_indexes=bytes(row['writableIndexes']);item.readonly_indexes=bytes(row['readonlyIndexes'])
    def instruction(dst,ix):
        dst.program_id_index=ix.get('programIdIndex',keys.index(ix['programId']) if 'programId' in ix else 0)
        dst.accounts=bytes(keys.index(a) if isinstance(a,str) else a for a in ix.get('accounts',[]))
        if 'data' in ix:dst.data=base58.b58decode(ix['data'])
        else:
            v=ix['parsed']['info'];dst.data=struct.pack('<IQ',2,v['lamports'])
            dst.accounts=bytes([keys.index(v['source']),keys.index(v['destination'])])
        if 'stackHeight' in ix and 'stack_height' in dst.DESCRIPTOR.fields_by_name:dst.stack_height=ix['stackHeight']
    for ix in source['instructions']:instruction(msg.instructions.add(),ix)
    meta=info.meta;raw=tx['meta'];meta.fee=raw.get('fee',5000)
    pre=raw.get('preBalances',[10**9]*len(keys));meta.pre_balances.extend(pre+[0]*(len(keys)-len(pre)))
    post=raw.get('postBalances',[10**9-5000]+[10**9]*(len(keys)-1));meta.post_balances.extend(post+[0]*(len(keys)-len(post)))
    meta.log_messages.extend(raw.get('logMessages',['Program success']))
    for group in raw.get('innerInstructions',[]):
        target=meta.inner_instructions.add();target.index=group['index']
        for ix in group['instructions']:instruction(target.instructions.add(),ix)
    for name,target in [('preTokenBalances',meta.pre_token_balances),('postTokenBalances',meta.post_token_balances)]:
        for row in raw.get(name,[]):
            b=target.add();b.account_index=row['accountIndex'];b.mint=row['mint'];b.owner=row['owner'];b.program_id=row.get('programId',C.TOKEN)
            b.ui_token_amount.amount=row['uiTokenAmount']['amount'];b.ui_token_amount.decimals=row['uiTokenAmount']['decimals']
    loaded=raw.get('loadedAddresses',{})
    meta.loaded_writable_addresses.extend(base58.b58decode(k) for k in loaded.get('writable',[]))
    meta.loaded_readonly_addresses.extend(base58.b58decode(k) for k in loaded.get('readonly',[]))
    return u


def varint(n):
    out=bytearray()
    while n>=128:out.append((n&127)|128);n>>=7
    return bytes(out)+bytes([n])


def wire(field,payload):
    return varint((field<<3)|2)+varint(len(payload))+payload


def explicit_zero(update):
    u=copy.deepcopy(update);u.transaction.transaction.index=0
    info=u.transaction.transaction.SerializeToString()+b'\x28\x00'
    u.transaction.ClearField('transaction');envelope=u.transaction.SerializeToString()+wire(1,info)
    u.ClearField('transaction')
    return u.SerializeToString()+wire(4,envelope)


@pytest.fixture
def source(tmp_path,monkeypatch,ledger):
    clock=[C.R-1]
    real_perf=time.perf_counter
    monkeypatch.setattr(W,'time',SimpleNamespace(time=lambda:clock[0],monotonic=lambda:clock[0],perf_counter=real_perf))
    capture=capture_worker(tmp_path/'desk',monkeypatch)
    capture.wallet_flow_conn(clock[0]).commit()
    path=tmp_path/'desk/pumpswap/tape.db'
    obj=SimpleNamespace(clock=clock,capture=capture,path=path,desk=tmp_path/'desk',watch=tmp_path/'watch.db',ledger=ledger)
    yield obj
    if hasattr(obj,'worker'):obj.worker.close()
    for conn in capture.files._open.values():conn.close()


def started(s,monkeypatch):
    s.k=C.service(monkeypatch);s.module=tail_module()
    s.worker=s.module.KnownGroupTail(s.ledger,capture_path=s.path,desk_dir=s.desk,watch_path=s.watch,clock=lambda:s.clock[0],now_iso_fn=lambda:C.iso(s.clock[0]))
    return s


def heartbeat(s,mode='known'):
    beat=dict(ts=s.clock[0],connected=mode!='disconnected',wallet_flow=dict(enabled=True,storage_initialized=True,
        last_commit_state='known',pending_gaps={'synthetic_gap':1} if mode=='gap' else {},transaction_callback_errors=0,
        desired_selectors=1,submitted_selectors=1,submitted_at=s.clock[0],selector_acknowledged=None))
    path=s.desk/'universe.heartbeat'
    if mode=='missing':path.unlink(missing_ok=True)
    else:path.write_text(json.dumps(beat))


def push(s,tx,receipt,*,metadata=True):
    u=compile_native(tx)
    s.capture.wallet_flow_process(u.SerializeToString(),received_at=receipt)
    if metadata:s.capture.wallet_flow_metadata(tx['slot'],tx['blockTime'],s.clock[0])
    s.capture.commit_files()


def state(s):
    row=db.known_group_tail_state(s.worker.observer,str(s.path.resolve()))
    return row,json.loads(row['state_json'])


def test_initial_tail_baseline_skips_history_and_restart_keeps_original_anchor(source,monkeypatch):
    """Spec102-107: first activation records unknown history; restart cannot replay old launches."""
    s=started(source,monkeypatch);s.clock[0]=C.R
    push(s,C.funding(),C.R-.5)
    main_before=s.ledger.serialize()
    first=s.worker.tick();assert first['state']=='baseline' and first['rows']==0
    row,before=state(s);assert row['cursor']==row['baseline']==1 and before['funding']=={}
    assert s.ledger.serialize()==main_before
    s.worker.close()
    s.clock[0]=C.R+.1;heartbeat(s)
    s.worker=s.module.KnownGroupTail(s.ledger,capture_path=s.path,desk_dir=s.desk,watch_path=s.desk/'unused.db',clock=lambda:s.clock[0],now_iso_fn=lambda:C.iso(s.clock[0]))
    resumed=s.worker.tick();after,_=state(s)
    assert resumed['rows']==0 and (after['baseline'],after['anchor'],after['started_at'])==(row['baseline'],row['anchor'],row['started_at'])
    assert intents_count(s)==0


def intents_count(s):
    return s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]


def test_capture_file_replacement_refuses_without_rebaselining(source,monkeypatch):
    """Spec105-107: a different inode cannot silently become the retained live tail."""
    s=started(source,monkeypatch);assert s.worker.tick()['state']=='baseline'
    old,_=state(s)
    for conn in s.capture.files._open.values():conn.close()
    s.capture.files._open.clear()
    original=s.path.with_name('original.db');s.path.rename(original)
    s.capture.wallet_flow_ready=False
    # Use the existing capture owner to create the replacement schema.
    s.capture.wallet_flow_conn(s.clock[0]).commit()
    s.clock[0]+=.1;heartbeat(s)
    out=s.worker.tick();new,_=state(s)
    assert out['state']=='unknown' and 'replaced' in out['reason']
    assert new['identity']==old['identity'] and new['baseline']==old['baseline'] and intents_count(s)==0


@pytest.mark.parametrize('kind',['absent','explicit_zero','positive','mismatch'])
def test_original_wire_index_presence_distinguishes_zero_from_unknown(kind):
    """Spec104-106: protobuf default zero is never invented chain order."""
    module=tail_module();u=compile_native(C.funding());u.transaction.transaction.index=0
    raw=explicit_zero(u) if kind=='explicit_zero' else u.SerializeToString()
    if kind in ('positive','mismatch'):
        u.transaction.transaction.index=7;raw=u.SerializeToString()
    parsed=pb.SubscribeUpdate.FromString(raw).transaction.transaction
    if kind=='mismatch':
        parsed.index=8
        with pytest.raises(ValueError):module._explicit_index(parsed,raw)
    else:assert module._explicit_index(parsed,raw)=={'absent':None,'explicit_zero':0,'positive':7}[kind]


def test_pending_metadata_restart_retains_exact_flow_receipt_and_expires_original_deadline(source,monkeypatch):
    """Spec43-46,102-109: metadata catch-up never restamps a pending flow's receipt."""
    s=started(source,monkeypatch);assert s.worker.tick()['state']=='baseline'
    s.clock[0]=C.R-.5;heartbeat(s);push(s,C.funding(),C.R-.8)
    s.worker.tick()
    s.clock[0]=C.R+.1;heartbeat(s);push(s,C.creation(),C.R,metadata=False)
    out=s.worker.tick();row,pending=state(s)
    assert out['refusals']['known_group_block_time_pending']==1
    assert pending['pending']=={'2':{'receipt_utc':C.iso(C.R),'received_at':C.R}}
    s.worker.close();s.clock[0]=C.R+2;heartbeat(s)
    s.worker=s.module.KnownGroupTail(s.ledger,capture_path=s.path,desk_dir=s.desk,watch_path=s.desk/'unused.db',clock=lambda:s.clock[0],now_iso_fn=lambda:C.iso(s.clock[0]))
    s.worker.tick();_,again=state(s)
    assert again['pending']==pending['pending']
    s.clock[0]=C.R+5.01;heartbeat(s)
    s.capture.wallet_flow_metadata(100,int(C.R)-1,s.clock[0]);s.capture.commit_files()
    expired=s.worker.tick();_,finished=state(s)
    assert expired['refusals']['known_group_original_deadline_expired']==1
    assert not finished['pending'] and intents_count(s)==0
    # Original raw capture receipt survives late metadata availability.
    page=db.read_known_group_raw_tail(s.path,cursor=0,pending={},anchor=None,limit=2,now_host=s.clock[0])
    assert page['rows'][1]['received_at']==C.R


@pytest.mark.parametrize('health',['missing','disconnected','gap'])
def test_unknown_observer_health_blocks_entry_but_fresh_frozen_member_sell_still_closes(source,monkeypatch,health):
    """Spec118-139: completeness is unnecessary for independently proved protective sells."""
    s=started(source,monkeypatch);sell=C.sell_fixture(monkeypatch);cohort,digest=C.enable(s.k,s.ledger);p=C.proof()
    pid,_=C.open_frozen(s.k,s.ledger,p)
    s.k.set_admission(s.ledger,enabled=False,confirm=True,policy_hash=digest,now_iso=C.iso(C.R+.8))
    s.clock[0]=C.R+.9;assert s.worker.tick()['state']=='baseline'
    s.clock[0]=C.R+1.3;heartbeat(s,health)
    push(s,sell['transaction'],C.R+1)
    out=s.worker.tick()
    assert out['state']=='unknown' and out['eligible']==0
    rows=s.ledger.execute('SELECT position_id,action,request_json FROM memecoin_manual_exit_intents').fetchall()
    assert len(rows)==1 and rows[0]['position_id']==pid
    assert rows[0]['action'] not in ('close','close_any_price','source_partial')
    payload=json.loads(rows[0]['request_json']);assert payload
    assert s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==1


def test_same_inode_anchor_replacement_and_clock_regression_refuse(source,monkeypatch,tmp_path):
    """Spec102-107: a changed committed anchor or regressed clock cannot restart eligibility."""
    s=started(source,monkeypatch);s.clock[0]=C.R;push(s,C.funding(),C.R-.5)
    assert s.worker.tick()['state']=='baseline'
    old,_=state(s);s.clock[0]=C.R-.1
    regressed=s.worker.tick()
    assert regressed['state']=='unknown' and 'clock_regressed' in regressed['reason']
    s.clock[0]=C.R+.1;heartbeat(s)
    other=capture_worker(tmp_path/'other-capture',monkeypatch)
    tx=C.funding();tx['transaction']['signatures']=[base58.b58encode(bytes([12])*64).decode()]
    other.wallet_flow_process(compile_native(tx).SerializeToString(),received_at=C.R-.5)
    other.wallet_flow_metadata(tx['slot'],tx['blockTime'],s.clock[0]);other.commit_files()
    inode=s.path.stat().st_ino
    other.wallet_flow_conn(s.clock[0]).backup(s.capture.wallet_flow_conn(s.clock[0]))
    for conn in other.files._open.values():conn.close()
    assert s.path.stat().st_ino==inode
    out=s.worker.tick();row,_=state(s)
    assert out['state']=='unknown' and 'anchor_replaced' in out['reason']
    assert row['anchor']==old['anchor'] and row['baseline']==old['baseline'] and intents_count(s)==0


def transfer(sender,recipient,*,signature,slot,block_time,amount):
    tx=C.funding();tx.update(slot=slot,blockTime=block_time)
    tx['transaction']['signatures']=[base58.b58encode(bytes([signature])*64).decode()]
    tx['transaction']['message']['accountKeys']=[dict(pubkey=sender,signer=True),dict(pubkey=recipient,signer=False)]
    tx['transaction']['message']['instructions'][0]['parsed']['info'].update(source=sender,destination=recipient,lamports=amount)
    tx['meta'].update(preBalances=[amount+1_000_005_000,0],postBalances=[1_000_000_000,amount])
    return tx


def test_prior_root_to_new_hub_ancestry_survives_twenty_four_minutes_restart_and_live_relay(source,monkeypatch):
    """Spec32-46,108-116: old causal ancestry remains; only final developer funding has the60s filter."""
    from solders.pubkey import Pubkey
    s=started(source,monkeypatch);assert s.worker.tick()['state']=='baseline'
    hub=str(Pubkey.from_bytes(bytes([30])*32));roots=s.k.APPROVED_ROOTS
    s.clock[0]=C.R+.1;heartbeat(s)
    push(s,transfer(C.H,hub,signature=21,slot=99,block_time=int(C.R)-1,amount=1_000_000_000),C.R)
    s.worker.tick();_,first=state(s)
    original=copy.deepcopy(first['funding'][hub][0]);assert original['known_at_host']<C.R+1
    s.clock[0]=C.R+24*60+.1;heartbeat(s);s.worker.tick()
    s.worker.close()
    s.worker=s.module.KnownGroupTail(s.ledger,capture_path=s.path,desk_dir=s.desk,watch_path=s.watch,clock=lambda:s.clock[0],now_iso_fn=lambda:C.iso(s.clock[0]))
    relay_receipt=C.R+24*60+.5;s.clock[0]=relay_receipt+.1;heartbeat(s)
    push(s,transfer(hub,C.DEV,signature=22,slot=101,block_time=int(relay_receipt),amount=10_000_000),relay_receipt)
    s.worker.tick();_,relayed=state(s)
    assert C.DEV in relayed['funding'],'live new-hub relay stopped being tracked when its old ancestry was pruned'
    assert relayed['funding'][C.DEV][0]==original
    creation_receipt=relay_receipt+1;s.clock[0]=creation_receipt+.1;heartbeat(s)
    beat=json.loads((s.desk/'universe.heartbeat').read_text());beat.update(ps_committed_through=s.clock[0]-.01,sol_usd=150.,sol_age_s=0.)
    (s.desk/'universe.heartbeat').write_text(json.dumps(beat))
    tx=C.creation();tx.update(slot=102,blockTime=int(creation_receipt))
    push(s,tx,creation_receipt);s.worker.tick();_,observed=state(s)
    candidate=observed['candidates'][C.CREATE_SIG]
    proof=C.proof();proof['creation']=dict(candidate['creation'],decimals=6,transaction=s.worker._native_profile(candidate['creation']['transaction_ref']))
    proof['funding_path']=[dict(hop,transaction=s.worker._native_profile(hop['transaction_ref'])) for hop in observed['funding'][C.DEV]]
    proof['members']=[dict(address=C.DEV,role='developer',proof_ref=proof['creation']['raw_ref'],known_at_host=candidate['known_at_host'])]
    proof['tradable']['observed_at_host']=creation_receipt+.2
    proof['tradable']['pool_state'].update(slot=103,metadata_slot=102,cut_slot=104,virtual_quote_slot=103,
        metadata_known_at=creation_receipt+.1,reserve_known_at=creation_receipt+.2,virtual_quote_known_at=creation_receipt+.2)
    decision=creation_receipt+.5
    result=s.k.evaluate_candidate(proof,now_host=decision,now_iso=C.iso(decision))
    assert result['eligible'],result
    assert result['request']['ttl_s']==pytest.approx(4.5)
    assert result['request']['desk_metadata']['entry_deadline_utc']==C.iso(creation_receipt+5)
    assert proof['funding_path'][0]['known_at_host']==original['known_at_host']
    assert all(hop['known_at_host']<creation_receipt for hop in proof['funding_path'])
    assert s.k.APPROVED_ROOTS==roots==frozenset({C.H}) and hub not in roots
