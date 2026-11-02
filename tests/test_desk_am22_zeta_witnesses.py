"""Zeta FAIL 7e04cc470 witnesses (verbatim from ~/.cache/codex/zeta-chart-7e04cc470/tests/test_zeta_am22.py)."""
import sqlite3,json
from datetime import datetime,timezone
import pytest
from test_desk_ps_model_facts import tape,facts,NOW
from test_desk_decision_lag_report import M
from test_memecoin_desk_intake_collected import IA,intake_candidate,collected,pol,COLLECTED,decide
from sniper.services import memecoin_desk_ps_facts as F,memecoin_desk as desk

def test_validator_refuses_unbounded_future_age(tmp_path):
    c,_=tape(tmp_path); f=facts(c,tmp_path)
    p=f['ages']['provider_reported']
    p.update(state='known',reason=None,age_s=600.,occurred_at=NOW-600,known_at=NOW+10**9,source='pumpfun',observed_at=NOW+10**9,clock_offset_s=0.,age_clock='decision')
    with pytest.raises(ValueError): F.validate(f)

def test_report_reads_real_pass_provenance_not_outer_reason():
    c=sqlite3.connect(':memory:')
    c.execute('create table desk_tickets(ts,reason,stage,opportunity_id,cohort_id,inputs_json)')
    c.execute('create table desk_candidate_emissions(opportunity_id,candidate_json)')
    cand=intake_candidate(); evidence=collected(cand,chart={'status':'pass','reason':'chart_complete_ageclock_decision'})
    assert decide(cand,evidence,require_collected=COLLECTED)=='pass'
    cut=cand['snapshot']['emission_at']; ts=datetime.fromtimestamp(cut+5,timezone.utc)
    c.execute('insert into desk_candidate_emissions values(?,?)',('o',json.dumps(cand)))
    c.execute('insert into desk_tickets values(?,?,?,?,?,?)',(ts.isoformat(),'pass','hard_filter','o','D',json.dumps({'evidence':evidence})))
    result=M.report(c,now=ts)
    assert result[0]['age_clock']=='decision'

def test_persisted_decision_time_is_after_collect(monkeypatch,tmp_path):
    t=[NOW]; seen={}; cand=intake_candidate()
    monkeypatch.setattr(IA,'read_mint',lambda *a,**k:(None,None))
    def collect(e):
        t[0]+=5
        seen['accepted_at']=t[0]
        return {'chart':{'status':'pass','reason':'chart_complete_ageclock_decision'}}
    def evaluate(*a,**k):
        seen['persisted_at']=datetime.fromisoformat(k['now_iso']).timestamp()
        return {'verdict':'PASS','reason':'pass'}
    monkeypatch.setattr(desk,'evaluate_candidate',evaluate)
    IA.evaluate(cand,'t',desk_conn=None,desk_dir=tmp_path,rpc=type('RPC',(),{'timeout_s':1})(),clip_usd=25,hard_policy={},now_fn=lambda:t[0],evidence_fn=lambda *a,**k:{'checks':{}},collect=collect)
    assert seen['persisted_at']>=seen['accepted_at']
