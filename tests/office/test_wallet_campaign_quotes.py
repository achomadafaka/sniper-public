"""Campaign quotes share the existing owner and never imply fills or net profit."""
import importlib.util
import json
import sqlite3
from pathlib import Path
import pytest

S=importlib.util.spec_from_file_location('campaign_quote_owner',Path(__file__).resolve().parents[2]/'scripts/research/dex_boost_collector.py')
C=importlib.util.module_from_spec(S);S.loader.exec_module(C)
T=1793550000.;MINT='3'*40;SIG='5'*88

class World:
    def __init__(self,tmp,enabled=True):
        self.now=T+2;self.calls=[];self.bad=None;self.delay=0;self.tmp=tmp
        self.source=tmp/'network.db'
        with sqlite3.connect(self.source) as c:
            c.execute('CREATE TABLE wallet_campaign_launches(launch_id TEXT PRIMARY KEY,campaign_id TEXT,mint TEXT,source_signature TEXT,source_receipt REAL,known_at REAL,emitted_at REAL,curve TEXT,coverage_json TEXT,version TEXT DEFAULT "wallet-campaign-launch-v1")')
            c.execute('INSERT INTO wallet_campaign_launches(launch_id,campaign_id,mint,source_signature,source_receipt,known_at,emitted_at,curve,coverage_json) VALUES(?,?,?,?,?,?,?,?,?)',('L','P',MINT,SIG,T,T+1,T+1,None,json.dumps(dict(creation_verified=True,launch_proof_ref='fixture',launch_method='wallet-watch-launch-v1',phase='observed_preparation'))))
        self.owner=self.make(enabled)
    def make(self,enabled=True):
        return C.Collector(intake_dir=self.tmp/'intake',out_dir=self.tmp/'out',desk_dir=self.tmp/'desk',clock_fn=lambda:(0.,self.now),jupiter_fn=self.quote,ps_buy=None,ps_sell=None,paper_throttled_fn=lambda:False,now_fn=lambda:self.now,campaign_probe=enabled,campaign_db=self.source)
    def quote(self,i,o,n):
        self.calls.append((i,o,n));self.now+=self.delay
        q=dict(inputMint=i,outputMint=o,inAmount=str(n),outAmount=str(2**63 if i==C.FAMILY_USDC else 30_000_000),otherAmountThreshold=str(2**63-1 if i==C.FAMILY_USDC else 29_000_000),contextSlot=100,routePlan=[dict(swapInfo=dict(ammKey='pool'))])
        if self.bad:q.update(self.bad)
        return q,None
    def rows(self,table):
        with sqlite3.connect(self.tmp/'out'/f'{C.utc_day(T)}.db') as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM '+table)]
    def at(self,t):self.now=t;return self.owner.tick()

def test_default_off_has_no_campaign_tables_or_quotes(tmp_path):
    w=World(tmp_path,False);w.owner.tick();assert not w.calls
    for p in (tmp_path/'out').glob('????-??-??.db'):
        with sqlite3.connect(p) as c:assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'campaign_%'").fetchone()

def test_launch_without_buy_produces_exact_25usdc_own_quantity_quotes(tmp_path):
    w=World(tmp_path);w.owner.tick();w.at(T+7)
    q=w.rows('campaign_quotes');buy=next(r for r in q if r['arm']=='ready' and r['offset_s']==0)
    sell=next(r for r in q if r['arm']=='ready' and r['offset_s']==5)
    assert buy['input_atomic']=='25000000' and buy['acquired_qty_atomic']==str(2**63)
    assert sell['input_atomic']==buy['acquired_qty_atomic'] and sell['output_mint']==C.FAMILY_USDC
    assert sell['net_usdc_atomic'] is None and sell['execution_state']=='quote_only'
    assert w.rows('campaign_studies')[0]['source_receipt']==T

@pytest.mark.parametrize('column,value',[('source_receipt',None),('known_at',T+3),('mint','bad'),('emitted_at',T-1)])
def test_bad_identity_or_future_evidence_is_censored(tmp_path,column,value):
    w=World(tmp_path)
    with sqlite3.connect(w.source) as c:c.execute('UPDATE wallet_campaign_launches SET '+column+'=?',(value,))
    w.owner.tick();assert not w.calls
    assert w.rows('campaign_studies')[0]['state']=='censored'

def test_wrong_quote_identity_never_becomes_inventory(tmp_path):
    w=World(tmp_path);w.bad={'inputMint':MINT};w.owner.tick()
    q=w.rows('campaign_quotes')[0];assert q['state']=='censored' and q['acquired_qty_atomic'] is None

def test_restart_no_duplicate_entries_and_counts_shared_day_budget(tmp_path):
    w=World(tmp_path);w.owner.tick();assert len(w.calls)==1
    w.owner=w.make();w.owner.tick();assert len(w.calls)==1
    assert w.owner.quotes_today(T+2)==1

def test_response_late_and_dailycap_censor_without_success(tmp_path,monkeypatch):
    w=World(tmp_path);w.delay=6;w.owner.tick()
    assert w.rows('campaign_quotes')[0]['reason']=='response_late'
    other=tmp_path/'cap';other.mkdir();w=World(other);monkeypatch.setattr(C,'DAILY_CAP',0);w.owner.tick()
    assert not w.calls and w.rows('campaign_quotes')[0]['reason']=='cap_reached'

def test_report_never_turns_missing_costs_or_landing_into_net_profit():
    from sniper.office.wallet_campaign_quotes import evaluate_round_trip
    entry=dict(state='known',input_mint=C.FAMILY_USDC,input_atomic='25000000',output_mint=MINT,acquired_qty_atomic=str(2**63),response_host=T+1)
    exit=dict(state='known',input_mint=MINT,input_atomic=str(2**63),output_mint=C.FAMILY_USDC,expected_output_atomic='30000000',response_host=T+10)
    r=evaluate_round_trip(entry,exit)
    assert r['quoted_gross_usdc_atomic']=='5000000' and r['net_usdc_atomic'] is None
    assert r['execution_state']=='quote_only' and r['cost_state']=='unknown'
    exit['input_atomic']='1';assert evaluate_round_trip(entry,exit)['state']=='censored'


def test_cap_censor_finishes_study_instead_of_leaving_phantom_pending(tmp_path,monkeypatch):
    w=World(tmp_path);monkeypatch.setattr(C,'DAILY_CAP',0);w.owner.tick();w.at(T+12)
    assert w.rows('campaign_studies')[0]['state']=='censored'
    assert w.rows('campaign_pending')==[]


def test_interrupted_emission_is_revisited_and_missing_source_is_not_empty(tmp_path):
    w=World(tmp_path)
    with sqlite3.connect(w.source) as c:c.execute('UPDATE wallet_campaign_launches SET emitted_at=NULL')
    w.owner.tick();assert not w.calls
    with sqlite3.connect(w.source) as c:c.execute('UPDATE wallet_campaign_launches SET emitted_at=?',(T+2,))
    w.at(T+3);assert len(w.calls)==1
    w.source.rename(tmp_path/'old.db');sqlite3.connect(w.source).close()
    assert w.at(T+4)['campaign']['source_reason']=='campaign_source_replaced'


def test_campaign_quote_budget_stays_counted_when_new_feature_is_disabled(tmp_path):
    w=World(tmp_path);w.owner.tick();w.owner=w.make(False)
    assert w.owner.quotes_today(T+2)==1


def test_same_launch_with_second_campaign_does_not_duplicate_quote(tmp_path):
    w=World(tmp_path);w.owner.tick()
    with sqlite3.connect(w.source) as c:c.execute('INSERT INTO wallet_campaign_launches(launch_id,campaign_id,mint,source_signature,source_receipt,known_at,emitted_at,curve,coverage_json) VALUES(?,?,?,?,?,?,?,?,?)',('L2','OTHER',MINT,SIG,T,T+1,T+1,None,json.dumps(dict(creation_verified=True,launch_proof_ref='fixture',launch_method='wallet-watch-launch-v1',phase='observed_preparation'))))
    w.at(T+3);assert len(w.calls)==1 and len(w.rows('campaign_studies'))==1


def test_same_inode_source_reset_is_a_gap_not_silent_empty(tmp_path):
    w=World(tmp_path);w.owner.tick()
    with sqlite3.connect(w.source) as c:c.execute('DELETE FROM wallet_campaign_launches')
    assert w.at(T+3)['campaign']['source_reason']=='campaign_source_row_changed'


def test_quote_records_frozen_clock_sample_and_actual_scheduling_delay(tmp_path):
    w=World(tmp_path);w.owner.tick();q=w.rows('campaign_quotes')[0]
    assert q['clock_offset_s']==0 and q['clock_sampled_at']==T+2
    assert q['requested_host']-q['due_host']==1

@pytest.mark.parametrize('change',[dict(creation_verified=False),dict(launch_proof_ref=None),dict(launch_method='claimed'),dict(phase='invented')])
def test_only_versioned_creation_proof_contract_is_quote_eligible(tmp_path,change):
    w=World(tmp_path)
    with sqlite3.connect(w.source) as c:
        coverage=json.loads(c.execute('SELECT coverage_json FROM wallet_campaign_launches').fetchone()[0]);coverage.update(change)
        c.execute('UPDATE wallet_campaign_launches SET coverage_json=?',(json.dumps(coverage),))
    w.owner.tick();assert not w.calls
    assert w.rows('campaign_studies')[0]['reason']=='creation_proof_unknown'


def test_interrupted_study_commit_clock_is_recovered_without_backdating(tmp_path):
    w=World(tmp_path);original=w.owner.day
    def rollback_clock(day):
        conn=original(day);w.now=T+.5;return conn
    w.owner.day=rollback_clock;w.owner.tick();assert not w.calls
    w.owner.day=original;w.at(T+3)
    assert len(w.calls)==1
    study=w.rows('campaign_studies')[0]
    assert study['recorded_at']==T+3


def test_solana_quote_amount_above_unsigned_64bit_is_not_inventory(tmp_path):
    w=World(tmp_path);w.bad={'outAmount':str(2**64),'otherAmountThreshold':str(2**64-1)};w.owner.tick()
    assert w.rows('campaign_quotes')[0]['state']=='censored'


def test_request_clock_becomes_nan_after_selection_never_calls_provider(tmp_path):
    w=World(tmp_path);w.owner.campaign.ingest(w.now,(0.,w.now))
    c=w.owner.day(C.utc_day(T))
    row=c.execute('SELECT p.*,s.mint,s.recorded_at FROM campaign_pending p JOIN campaign_studies s USING(study_id) ORDER BY due_host LIMIT 1').fetchone()
    w.owner.now_fn=lambda:float('nan')
    assert w.owner.campaign.one(c,row,(0.,T+2),100)==0
    assert not w.calls and w.owner._today==0


def test_completed_study_status_is_derived_from_all_eight_quote_results(tmp_path):
    w=World(tmp_path)
    for t in (T+2,T+7,T+12,T+17,T+32,T+42,T+302,T+312):w.at(t)
    assert len(w.rows('campaign_quotes'))==8
    assert w.rows('campaign_studies')[0]['state']=='quoted'


@pytest.mark.parametrize('restart',[False,True])
def test_quote_ingest_clock_floor_survives_other_launches_and_restart(tmp_path,restart):
    w=World(tmp_path);w.owner.tick()
    with sqlite3.connect(w.source) as c:
        c.execute('INSERT INTO wallet_campaign_launches(launch_id,campaign_id,mint,source_signature,source_receipt,known_at,emitted_at,curve,coverage_json) VALUES(?,?,?,?,?,?,?,?,?)',('L2','P2','4'*40,'6'*88,T,T+.5,T+1,None,json.dumps(dict(creation_verified=True,launch_proof_ref='second',launch_method='wallet-watch-launch-v1',phase='unknown'))))
    w.now=T+1.5
    if restart:w.owner=w.make()
    result=w.owner.tick()
    assert result['campaign']['source_reason']=='campaign_clock_regressed'
    assert len(w.rows('campaign_studies'))==1


def test_response_below_an_intermediate_observed_clock_is_not_known_inventory(tmp_path):
    w=World(tmp_path);w.owner.campaign.ingest(w.now,(0.,w.now));c=w.owner.day(C.utc_day(T))
    row=c.execute('SELECT p.*,s.mint,s.recorded_at FROM campaign_pending p JOIN campaign_studies s USING(study_id) ORDER BY due_host LIMIT 1').fetchone()
    samples=iter([T+2,T+2.2,T+2.1,T+2.1]);w.owner.now_fn=lambda:next(samples,T+2.1)
    w.owner.campaign.one(c,row,(0.,T+2),100)
    quote=w.rows('campaign_quotes')[0]
    assert quote['state']=='censored' and quote['acquired_qty_atomic'] is None
