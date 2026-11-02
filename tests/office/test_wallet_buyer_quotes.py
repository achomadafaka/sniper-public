"""Prospective buyer collection never substitutes later eligible buys or fictitious fills."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sniper.office import wallet_buyer_quotes as B

T=1793617200.
MINT='3'*44
SIG='5'*88

class Bucket:
    def take(self,at):return True

class World:
    def __init__(self,tmp):
        self.now=T;self.calls=[];self.bad={};self.delay=0
        self.source=tmp/'source.db'
        with sqlite3.connect(self.source) as c:
            c.execute('CREATE TABLE ps_watch_events(signature TEXT,ordinal INTEGER,pool TEXT,mint TEXT,quote_mint TEXT,slot INTEGER,tx_index INTEGER,is_buy INTEGER,actor TEXT,base_atomic TEXT,quote_atomic TEXT,pre_quote_reserve_atomic TEXT,post_quote_reserve_atomic TEXT,received_at REAL,PRIMARY KEY(signature,ordinal))')
            c.execute('CREATE INDEX ps_watch_events_at ON ps_watch_events(received_at)')
        self.owner=SimpleNamespace(out_dir=tmp,now_fn=lambda:self.now,glob=Bucket(),_today=0,_jupiter=self.quote)
        self.activation=dict(activation_host=T,submitted_digest='a'*64,watched=list(B.BUYERS),capture_healthy=True,capture_sampled_at=T)
        self.probe=B.BuyerProbe(self.owner,self.source,self.activation)
    def buy(self,*,mint=MINT,sig=SIG,at=T,quote=None,actor=None):
        with sqlite3.connect(self.source) as c:
            c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sig,0,'4'*44,mint,quote or B.WSOL,10,1,1,actor or B.BUYERS[0],'1000','100000',str(20*10**9),str(21*10**9),at))
    def quote(self,i,o,n,utc):
        self.calls.append((i,o,n,self.now));self.now+=self.delay
        return dict(inputMint=i,outputMint=o,inAmount=str(n),outAmount='1000' if i==B.USDC else '26000000',otherAmountThreshold='900' if i==B.USDC else '25000000',routePlan=[{'swapInfo':{'ammKey':'pool'}}],contextSlot=11,**self.bad),None,{'route_id':'pool'}
    def tick(self,at):
        self.now=at;return self.probe.tick((0.,at),float('inf'))
    def rows(self,table):
        return [dict(r) for r in self.probe.conn.execute('SELECT * FROM '+table)]

def test_first_refused_buy_is_not_replaced_by_later_eligible_buy(tmp_path):
    w=World(tmp_path);w.buy(quote='2'*44);w.tick(T)
    w.buy(sig='6'*88,at=T+1);w.tick(T+1)
    assert len(w.rows('buyer_studies'))==1
    assert w.rows('buyer_studies')[0]['reason']=='unsupported_quote_mint'
    assert len(w.rows('buyer_events'))==2 and not w.calls

def test_original_receipt_anchor_expired_windows_never_reset_on_detection(tmp_path):
    w=World(tmp_path);w.buy();w.tick(T+20)
    assert not w.calls
    assert all(r['reason']=='queue_or_quota_window_passed' for r in w.rows('buyer_quotes'))
    assert w.rows('buyer_studies')[0]['detected_at']==T+20

def test_exact_expected_and_minimum_quantities_are_separate(tmp_path):
    w=World(tmp_path);w.buy();w.tick(T);w.tick(T+5)
    w.tick(T+65)
    sells=[c for c in w.calls if c[0]==MINT]
    assert {c[2] for c in sells}=={1000,900}
    rows=w.rows('buyer_quotes')
    assert all(r['net_usdc_atomic'] is None and r['execution_state']=='quote_only' for r in rows)
    assert w.probe.quotes_today(T)==len(w.calls)

def test_restart_preserves_denominator_and_admitted_attempts(tmp_path):
    w=World(tmp_path);w.buy();w.tick(T+5);n=len(w.calls)
    w.probe.close();w.probe=B.BuyerProbe(w.owner,w.source,w.activation)
    w.tick(T+5)
    assert len(w.rows('buyer_studies'))==1 and len(w.calls)==n

@pytest.mark.parametrize('delay,reason',[(2.1,'response_late'),(-1,'clock_regressed')])
def test_bad_response_clock_never_becomes_inventory(tmp_path,delay,reason):
    w=World(tmp_path);w.buy();w.delay=delay;w.tick(T+5)
    row=next(r for r in w.rows('buyer_quotes') if r['delay_s']==5 and r['horizon_s']==0)
    assert row['state']=='censored' and row['reason']==reason
    assert not any(r['horizon_s']>0 and r['state']=='known' for r in w.rows('buyer_quotes'))

def test_discovery_mints_and_pre_activation_events_remain_excluded(tmp_path):
    w=World(tmp_path);w.buy(mint=next(iter(B.DISCOVERY)));w.buy(sig='7'*88,at=T-1)
    w.tick(T+5)
    assert not w.calls and len(w.rows('buyer_events'))==1
    assert w.rows('buyer_studies')[0]['reason']=='discovery_mint'

def test_unknown_clock_and_activation_refuse_without_provider(tmp_path):
    w=World(tmp_path);w.buy();w.now=T+5
    assert w.probe.tick((0.,T-61),100)['reason']=='clock_unknown'
    with pytest.raises(ValueError):B.BuyerProbe(w.owner,w.source,{**w.activation,'capture_healthy':False})
    assert not w.calls

def test_controls_frozen_before_outcomes(tmp_path):
    w=World(tmp_path)
    for n in range(4):w.buy(mint=str(n+4)*44,sig=str(n+1)*88,at=T+n,actor='9'*44)
    w.buy(sig=SIG,at=T+5);w.tick(T+5)
    studies=w.rows('buyer_studies');controls=[r for r in studies if r['subject']=='control']
    assert len(controls)==3
    assert all(r['source_received_at']<T+5 and r['anchor_at']==T+5 for r in controls)
    before=[r['study_id'] for r in controls];w.tick(T+10)
    assert before==[r['study_id'] for r in w.rows('buyer_studies') if r['subject']=='control']

@pytest.mark.parametrize('bad',[{'inputMint':'wrong'},{'outAmount':'0'},{'otherAmountThreshold':'1001'},{'routePlan':[]}])
def test_wrong_or_zero_quote_never_spawns_exit_inventory(tmp_path,bad):
    w=World(tmp_path);w.buy();original=w.owner._jupiter
    def malformed(*args):
        q,why,extra=original(*args);q.update(bad);return q,why,extra
    w.owner._jupiter=malformed;w.tick(T+5)
    assert not any(r['horizon_s']>0 for r in w.rows('buyer_quotes'))
    assert next(r for r in w.rows('buyer_quotes') if r['delay_s']==5)['state']=='censored'

def test_non_object_provider_payload_is_typed_failure(tmp_path):
    w=World(tmp_path);w.buy();w.owner._jupiter=lambda *args:([],None,{})
    w.tick(T+5)
    assert next(r for r in w.rows('buyer_quotes') if r['delay_s']==5)['reason']=='quote_payload_unknown'

def test_quotas_expire_without_provider_or_denominator_loss(tmp_path):
    w=World(tmp_path);w.buy();w.now=T+5
    w.probe.tick((0.,w.now),0)
    w.tick(T+20)
    assert len(w.rows('buyer_studies'))==1 and not w.calls
    assert len(w.rows('buyer_quotes'))==3

def test_source_replacement_is_unavailable_not_empty_cohort(tmp_path):
    w=World(tmp_path);w.buy();w.tick(T)
    w.source.rename(tmp_path/'old.db')
    with sqlite3.connect(w.source) as c:c.execute('CREATE TABLE wrong(a)')
    assert w.tick(T+5)['reason']=='source_replaced'
    assert len(w.rows('buyer_studies'))==1


def test_existing_collector_default_off_and_shared_admission_accounting(tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('buyer_owner',Path(__file__).resolve().parents[2]/'scripts/research/dex_boost_collector.py')
    C=importlib.util.module_from_spec(spec);spec.loader.exec_module(C)
    w=World(tmp_path)
    owner=C.Collector(intake_dir=tmp_path/'intake',out_dir=tmp_path/'owner',desk_dir=tmp_path/'desk',clock_fn=lambda:(0.,w.now),jupiter_fn=lambda i,o,n:(w.quote(i,o,n,w.now)[0],None),ps_buy=None,ps_sell=None,paper_throttled_fn=lambda:False,now_fn=lambda:w.now,buyer_probe=True,buyer_source=w.source,buyer_activation=w.activation)
    w.buy();w.now=T+5;result=owner.tick()
    assert result['buyer']['quotes']==1
    assert owner.quotes_today(T+5)==1
    default=C.Collector(intake_dir=tmp_path/'intake2',out_dir=tmp_path/'off',desk_dir=tmp_path/'desk',clock_fn=lambda:(0.,w.now),jupiter_fn=None,ps_buy=None,ps_sell=None,paper_throttled_fn=lambda:False)
    assert default.buyer is None and not (tmp_path/'off'/'buyer-study.db').exists()

def test_primary_new_launch_requires_known_creation_at_first_detection(tmp_path):
    from datetime import datetime,timezone
    w=World(tmp_path);w.owner.desk_dir=tmp_path/'desk';launch_dir=w.owner.desk_dir/'pump';launch_dir.mkdir(parents=True)
    db=launch_dir/(datetime.fromtimestamp(T,timezone.utc).strftime('%Y-%m-%d')+'.db')
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE launches(mint TEXT PRIMARY KEY,curve TEXT,creator TEXT,slot INTEGER,signature TEXT,block_time INTEGER,received_at REAL)')
        c.execute('INSERT INTO launches VALUES(?,?,?,?,?,?,?)',(MINT,'4'*44,B.BUYERS[0],9,'6'*88,int(T+1),T+1))
    w.buy(at=T+2);w.tick(T+2)
    row=w.rows('buyer_studies')[0]
    assert row['launch_state']=='new_launch'
    assert json.loads(row['snapshot_json'])['creation']['read_known_at']==T+2

def test_missing_creation_never_retroactively_becomes_fresh(tmp_path):
    w=World(tmp_path);w.buy();w.tick(T)
    assert w.rows('buyer_studies')[0]['launch_state']=='unknown'
    w.tick(T+5)
    assert w.rows('buyer_studies')[0]['launch_state']=='unknown'

def test_late_detector_control_selection_cannot_read_future_receipts(tmp_path):
    w=World(tmp_path)
    w.buy(mint='4'*44,sig='1'*88,at=T+1,actor='9'*44)
    w.buy(at=T+2)
    w.buy(mint='5'*44,sig='2'*88,at=T+3,actor='9'*44)
    w.tick(T+7)
    controls=[r for r in w.rows('buyer_studies') if r['subject']=='control']
    assert len(controls)==1 and controls[0]['mint']=='4'*44


def test_priority_is_fixed_and_fresh_primary_precedes_unknown_diagnostic():
    base=dict(delay_s=5,subject='target',basis='entry',control_rank=None,due_host=T,study_id='a',horizon_s=0)
    assert B.BuyerProbe._priority({**base,'cohort':'new_launch'})<B.BuyerProbe._priority({**base,'cohort':'unknown'})
    assert B.BuyerProbe._priority({**base,'cohort':'new_launch'})<B.BuyerProbe._priority({**base,'cohort':'new_launch','subject':'control','control_rank':0})


def test_provider_exception_is_persisted_and_never_retried_on_restart(tmp_path):
    w=World(tmp_path);w.buy()
    def bad(*args):raise TimeoutError('private provider message')
    w.owner._jupiter=bad;w.tick(T+5)
    r=next(r for r in w.rows('buyer_quotes') if r['delay_s']==5)
    assert r['reason']=='provider_error' and r['accounted']==1
    assert 'private' not in json.dumps(w.rows('buyer_quotes'))
    w.tick(T+5)
    assert len([r for r in w.rows('buyer_quotes') if r['delay_s']==5])==1

def test_second_owner_cannot_race_same_pending_quotes(tmp_path):
    w=World(tmp_path)
    with pytest.raises(ValueError,match='already_running'):
        B.BuyerProbe(w.owner,w.source,w.activation)
    w.probe.close()
    other=B.BuyerProbe(w.owner,w.source,w.activation)
    other.close()

def test_locked_evidence_store_is_unknown_not_success_or_no_events(tmp_path):
    w=World(tmp_path);w.buy()
    blocker=sqlite3.connect(tmp_path/'buyer-study.db',timeout=0)
    blocker.execute('BEGIN EXCLUSIVE')
    try:assert w.tick(T+5)['reason']=='buyer_store_unavailable'
    finally:blocker.rollback();blocker.close()
    assert not w.calls

@pytest.mark.parametrize('signature',[None,'not-a-signature','7'*88])
def test_unbound_source_signature_never_qualifies_for_quotes(tmp_path,signature):
    w=World(tmp_path);w.buy(sig=signature or SIG)
    with sqlite3.connect(w.source) as c:c.execute('UPDATE ps_watch_events SET signature=?',(signature,))
    w.tick(T+5)
    assert not w.calls and w.rows('buyer_studies')[0]['reason']=='source_identity_unknown'

def test_activation_object_cannot_be_mutated_after_freeze(tmp_path):
    w=World(tmp_path);w.activation['activation_host']=T+100
    w.buy();w.tick(T+5)
    assert len(w.rows('buyer_studies'))==1

@pytest.mark.parametrize('watched',[None,{},[{}]])
def test_malformed_activation_watch_evidence_is_refused(tmp_path,watched):
    w=World(tmp_path)
    with pytest.raises(ValueError):B.BuyerProbe(w.owner,w.source,{**w.activation,'watched':watched})
