"""Finite missing-slot recovery shares the approved research owner and preserves unknowns."""
import json
import sqlite3
from pathlib import Path
import base58
import pytest
from sniper.office import wallet_metadata_jobs as M
from sniper.office.chain_data import ChainData, _Refused

NOW=1793616000.

@pytest.fixture
def owner(tmp_path):
    tape=tmp_path/'tape.db';path=tmp_path/'metadata_jobs.db';clock=[NOW];calls=[]
    with sqlite3.connect(tape) as c:
        c.execute('CREATE TABLE wallet_flow_transactions(flow_id INTEGER PRIMARY KEY,slot INTEGER,signature TEXT,received_at REAL,block_time INTEGER)')
        c.execute('CREATE INDEX wallet_flow_pending_slot ON wallet_flow_transactions(slot) WHERE block_time IS NULL')
        for i in range(1,4):
            sig=base58.b58encode(bytes([i])*64).decode()
            c.execute('INSERT INTO wallet_flow_transactions VALUES(?,?,?,?,NULL)',(i,i,sig,NOW-100))
    def rpc(url,method,params):
        calls.append((method,params))
        if method=='getTransaction':
            slot=base58.b58decode(params[0])[0]
            return 200,dict(result=dict(slot=slot,blockTime=int(NOW-101),transaction=dict(signatures=[params[0]])))
        return 200,dict(result=dict(value='regular'))
    chain=ChainData(tmp_path/'chain.db',lambda:'https://research.invalid',clock=lambda:clock[0],rpc=rpc,page=None)
    class Collector:
        def tick(self,*,max_calls):
            try:chain.call('getTokenSupply',['regular'],ttl=0)
            except _Refused as e:return dict(state='unknown',reason=e.reason,calls_attempted=int(e.dialed))
            return dict(state='partial',reason='regular',calls_attempted=1)
    return path,tape,clock,calls,chain,Collector(),tmp_path/'absent_receipts.db'

def poll(owner,slots=2,max_calls=1):
    path,tape,clock,calls,chain,collector,receipts=owner
    return M.poll(chain,collector,receipts,path,tape,slots=slots,max_calls=max_calls)

def test_disabled_never_creates_queue_or_reads_tape(owner):
    path,tape,*_=owner;tape.unlink()
    regular,receipts,status=poll(owner,slots=0)
    assert regular['reason']=='regular' and not path.exists()
    assert status['reason']=='metadata_recovery_disabled'

@pytest.mark.parametrize('max_calls',[1,4])
def test_real_shared_pacer_and_restart_fairness_recover_only_finite_requested_slots(owner,max_calls):
    path,tape,clock,calls,chain,_,_=owner
    for _ in range(40):poll(owner,max_calls=max_calls);clock[0]+=.25
    assert [method for method,_ in calls[:4]]==['getTransaction','getTokenSupply','getTransaction','getTokenSupply']
    assert sum(method=='getTransaction' for method,_ in calls)==2
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT slot,state,known_at FROM metadata_jobs ORDER BY slot').fetchall()==[(1,'known',NOW),(2,'known',NOW+4)]
    result=M.read_times(path,chain.path,tape,slots=[1,2,3],now=clock[0])
    assert result['items']=={1:int(NOW-101),2:int(NOW-101)}
    assert all(params[1]['commitment']=='finalized' for method,params in calls if method=='getTransaction')
    with sqlite3.connect(tape) as c:assert c.execute('SELECT count(*) FROM wallet_flow_transactions WHERE block_time IS NULL').fetchone()[0]==3

@pytest.mark.parametrize('damage',['null','slot','signature','future','bool_time','missing_transaction'])
def test_bad_receipt_never_grants_timestamp(owner,damage,monkeypatch):
    path,tape,clock,calls,chain,*_=owner
    signature=base58.b58encode(bytes([1])*64).decode()
    tx=dict(slot=1,blockTime=int(NOW-101),transaction=dict(signatures=[signature]))
    if damage=='null':tx=None
    if damage=='slot':tx['slot']=2
    if damage=='signature':tx['transaction']['signatures']=[base58.b58encode(bytes([9])*64).decode()]
    if damage=='future':tx['blockTime']=int(NOW+1)
    if damage=='bool_time':tx['blockTime']=True
    if damage=='missing_transaction':tx.pop('transaction')
    monkeypatch.setattr(chain,'rpc',lambda *args:(200,dict(result=tx)))
    poll(owner,slots=1)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT state,known_at FROM metadata_jobs').fetchone()==('pending',None)
    assert M.read_times(path,chain.path,tape,slots=[1],now=clock[0])['items']=={}

@pytest.mark.parametrize('damage',['expires','changed_doc','changed_at','changed_source','future_known'])
def test_producer_revalidates_cache_and_source_instead_of_trusting_job(owner,damage):
    path,tape,clock,calls,chain,*_=owner;poll(owner,slots=1)
    if damage in ('expires','changed_doc','changed_at'):
        with sqlite3.connect(chain.path) as c:
            if damage=='expires':c.execute('UPDATE cache SET expires=?',(NOW,))
            elif damage=='changed_at':c.execute('UPDATE cache SET at=at+1')
            else:c.execute('UPDATE cache SET doc=?',(json.dumps(dict(slot=999,blockTime=int(NOW-101))),))
    else:
        with sqlite3.connect(path) as c:
            if damage=='changed_source':c.execute("UPDATE metadata_schedule SET identity='wrong'")
            else:c.execute('UPDATE metadata_jobs SET known_at=?',(NOW+1,))
    result=M.read_times(path,chain.path,tape,slots=[1],now=clock[0])
    assert result['state']=='unknown' and result['items']=={}

def test_post_rpc_store_failure_never_calls_regular_with_same_budget(owner,monkeypatch):
    path,tape,clock,calls,chain,*_=owner;real=M._connect;failed=[]
    def connect(p,mode='rw'):
        if calls and mode=='rw' and Path(p)==path and not failed:
            failed.append(True);raise OSError('offline injected failure')
        return real(p,mode)
    monkeypatch.setattr(M,'_connect',connect)
    regular,receipts,status=poll(owner,slots=1)
    assert regular is None and receipts is None and status['calls_attempted']==1
    assert [method for method,_ in calls]==['getTransaction']
    poll(owner,slots=1)
    with sqlite3.connect(path) as c:assert c.execute('SELECT state FROM metadata_jobs').fetchone()==('known',)
    assert len(calls)==1


@pytest.mark.parametrize('error',[OSError,sqlite3.OperationalError])
def test_uncertain_admission_yields_to_regular_without_inventing_attempt_count(owner,monkeypatch,error):
    path,tape,clock,calls,chain,*_=owner;rpc=chain.rpc
    def uncertain(url,method,params):
        if method=='getTransaction':
            calls.append((method,params));raise error('offline admission unknown')
        return rpc(url,method,params)
    monkeypatch.setattr(chain,'rpc',uncertain)
    statuses=[]
    for _ in range(32):
        _,_,status=poll(owner,slots=1);statuses.append(status);clock[0]+=.25
    assert [m for m,_ in calls[:4]]==['getTransaction','getTokenSupply','getTransaction','getTokenSupply']
    assert any(s.get('provider_attempts','missing') is None for s in statuses)
    with sqlite3.connect(path) as c:assert c.execute('SELECT state,known_at FROM metadata_jobs').fetchone()==('pending',None)


@pytest.mark.parametrize('budget',[0,101,True])
def test_invalid_call_budget_never_opens_queue_or_dials(owner,budget):
    path,tape,clock,calls,*_=owner
    with pytest.raises(ValueError,match='bad_metadata_budget'):poll(owner,max_calls=budget)
    assert not path.exists() and calls==[]
