"""Offline JUDGE prompt migration and price refusals over temporary desk/ledger databases."""
import copy
import hashlib
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.persistence import db_memecoin as L
from sniper.services import memecoin_desk_judge as J
from sniper.services.memecoin_desk_universe import Field, b58encode
from test_memecoin_desk_contract import AT, NOW, candidate, rehash
from test_memecoin_desk_judge import Clock, model_returning, enter
from test_memecoin_desk_pipeline import api, databases, POLICY  # noqa: F401
from test_desk_ps_judge_preflight import ready as ready_ps, refreshed as ps_refreshed

NEW_PROMPT = 'desk-judge-v18'


def ready(db, name='curve', *, prompt=NEW_PROMPT, no_price=False):
    cohort = api().provision_cohort(*db, producer='U', policy={**POLICY,
        'judge_p_admit': 1., 'select_version': J.SELECT_VERSION, 'model_billing_basis':'subscription_flat'},
        prompt_version=prompt, created_at=AT)
    c = candidate(**({'liq_usd': Field(None, 'pumpfun_grpc:real_quote_reserves', NOW, 7, 'missing')}
                     if no_price else {}))
    c['ticket_id'] = 'desk:U:' + name
    c['trigger_ordinal'] = sum(name.encode())
    c['mint'] = b58encode(hashlib.sha256(name.encode()).digest())
    tid = api().ingest_candidate(db[0], c, cohort_id=cohort, now_iso=AT)['ticket_id']
    D.record_stage(db[0], ticket_id=tid, stage='hard_filter', seat='code', verdict='PASS',
                   reason='offline_fixture', inputs={}, output={}, ts=AT)
    return tid, cohort


def refresh(c, ts):
    c = copy.deepcopy(c)
    c['emitted_at'] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    c['snapshot']['emission_at'] = ts
    return rehash(c), {'fixture': True}


def forbidden(*a, **kw):
    raise AssertionError('preflight refusal reached soft facts, model or funded ledger')


def vet(db, tid):
    return next(s for s in D.get_ticket(db[0], tid)['stages'] if s['stage'] == 'vet')


def test_old_prompt_is_retired_without_blocking_the_new_prompt(databases):
    old, old_cohort = ready(databases, 'old', prompt='desk-judge-v12')
    new, _ = ready(databases, 'new')
    calls=[]; clock=Clock()
    out=J.run_once(databases[0], lambda: nullcontext(databases[1]),
        call_model=model_returning(enter,calls=calls,clock=clock),system_prompt='offline',
        worker='offline',now_fn=clock,calls_per_hour=100,expected_prompt_version=NEW_PROMPT)
    assert {r['ticket_id']:r['action'] for r in out} == {old:'SKIP',new:'ENTER'}
    assert len(calls)==1 and calls[0]['opportunity_id']==new
    assert vet(databases,old)['reason']=='prompt_version_mismatch'
    assert vet(databases,old)['cost_usd']==0
    assert D.get_admission(databases[0],ticket_id=old,arm='F')['decision']=='not_selected'
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls WHERE ticket_id=?',(old,)).fetchone()[0]==0
    assert L.get_desk_cohort_cost(databases[1],old_cohort,'desk-judge:'+old) is None
    assert not D.claim_judge_call(databases[0],ticket_id=old,call_id='late',worker='late',claimed_at=AT)


def test_prompt_refusal_precedes_asof_soft_model_and_ledger(databases):
    old,_=ready(databases,'old',prompt='desk-judge-v12')
    out=J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='offline',
        worker='offline',now_fn=Clock(),calls_per_hour=100,expected_prompt_version=NEW_PROMPT,
        asof_fn=forbidden,soft_fn=forbidden)
    assert out[0]['ticket_id']==old and out[0]['reason']=='prompt_version_mismatch'


@pytest.mark.parametrize('use_asof', [True, False])
def test_curve_without_a_computable_asof_cap_has_no_call_or_claim(databases, use_asof):
    tid,cohort=ready(databases,no_price=True); seen=[]
    def asof(c,ts):
        seen.append(ts);return refresh(c,ts)
    run=lambda:J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='offline',
        worker='offline',now_fn=Clock(),calls_per_hour=100,asof_fn=asof if use_asof else None,soft_fn=forbidden)
    out=run()
    assert [(r['action'],r['reason']) for r in out]==[('SKIP','no_verdict_price')]
    assert len(seen)==int(use_asof) and run()==[]
    v=vet(databases,tid)
    assert v['cost_usd']==0
    if use_asof:
        assert v['inputs']['asof']['candidate']['snapshot']['emission_at']==seen[0]
    else:
        assert 'asof' not in v['inputs']
    assert D.get_admission(databases[0],ticket_id=tid,arm='F')['decision']=='not_selected'
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls').fetchone()[0]==0
    assert L.get_desk_cohort_cost(databases[1],cohort,'desk-judge:'+tid) is None


def test_priceable_curve_reuses_its_preclaim_snapshot(databases):
    tid,_=ready(databases); seen=[];calls=[];clock=Clock()
    def asof(c,ts):
        seen.append(ts);return refresh(c,ts)
    J.run_once(databases[0],lambda:nullcontext(databases[1]),
        call_model=model_returning(enter,calls=calls,clock=clock),system_prompt='offline',
        worker='offline',now_fn=clock,calls_per_hour=100,asof_fn=asof,expected_prompt_version=NEW_PROMPT)
    assert len(seen)==len(calls)==1
    assert vet(databases,tid)['inputs']['asof']['candidate']['snapshot']['emission_at']==seen[0]
    cap=D.list_outbox(databases[0])[0]['payload']['desk_metadata']['verdict_price']
    assert cap==J.desk.priced_verdict(vet(databases,tid)['inputs']['asof']['candidate'],clip_usd=25,basis='asof')


def test_ps_refusal_still_precedes_soft_lookup(databases,monkeypatch):
    ready_ps(databases)
    monkeypatch.setattr(J.desk,'pumpswap_unpriced_reason',lambda c:'virtual_quote_unproven')
    out=J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='offline',
        worker='offline',now_fn=Clock(),calls_per_hour=100,asof_fn=ps_refreshed,soft_fn=forbidden)
    assert out[0]['reason']=='virtual_quote_unproven'


def test_a_racing_claim_wins_prompt_retirement(databases,monkeypatch):
    tid,_=ready(databases,'old',prompt='desk-judge-v12');stale=D.get_ticket(databases[0],tid)
    assert D.claim_judge_call(databases[0],ticket_id=tid,call_id='owner',worker='owner',claimed_at=AT)
    monkeypatch.setattr(D,'list_awaiting_judge',lambda *a,**kw:[stale])
    out=J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='offline',
        worker='offline',now_fn=Clock(),calls_per_hour=100,expected_prompt_version=NEW_PROMPT)
    assert out==[] and D.get_admission(databases[0],ticket_id=tid,arm='F') is None
    assert not any(s['stage']=='vet' for s in D.get_ticket(databases[0],tid)['stages'])


def test_generic_refusal_is_idempotent_and_rolls_back_admission_on_failure(databases):
    tid,_=ready(databases,'one')
    args=dict(ticket_id=tid,reason='no_verdict_price',inputs={'fixture':'same'},ts=AT)
    assert D.record_preflight_skip(databases[0],**args)
    assert D.record_preflight_skip(databases[0],**args)
    with pytest.raises(ValueError,match='stage_conflict'):
        D.record_preflight_skip(databases[0],**{**args,'reason':'other'})
    tid2,_=ready(databases,'two')
    databases[0].execute("CREATE TRIGGER fail_preflight BEFORE INSERT ON desk_tickets WHEN NEW.stage='vet' "
                        "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='fixture failure'):
        D.record_preflight_skip(databases[0],**{**args,'ticket_id':tid2})
    assert D.get_admission(databases[0],ticket_id=tid2,arm='F') is None
    assert D.get_ticket(databases[0],tid2)['state']=='open'


def test_worker_disposes_old_prompt_and_passes_the_loaded_version(databases,tmp_path,monkeypatch):
    from test_memecoin_desk_judge_worker import W
    old,_=ready(databases,'old',prompt='desk-judge-v12')
    new,_=ready(databases,'new')
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_ENABLED','1')
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_ASOF','0')
    monkeypatch.setattr(W,'courier_pass',lambda *a,**kw:[])
    monkeypatch.setattr(J,'ranker_gate',lambda *_:None)
    monkeypatch.setattr(W,'log_latency',lambda *a,**kw:None)
    clock=Clock()
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock()
    monkeypatch.setattr(W,'datetime',FrozenDateTime)
    calls=[]
    monkeypatch.setattr(W,'sdk_call_model',lambda system,user,**kw:
        model_returning(enter,calls=calls,clock=clock)(system,user))
    pb=tmp_path/'playbook.md';pb.write_text('prompt_version: `'+NEW_PROMPT+'`\n---\nOffline fixture\n')
    db_path=databases[0].execute('PRAGMA database_list').fetchone()[2]
    ledger_path=databases[1].execute('PRAGMA database_list').fetchone()[2]
    assert W.main(['--desk-db',db_path,'--ledger-db',ledger_path,'--playbook',str(pb),
                   '--calls-per-hour','100'],env_root=tmp_path)==0
    assert len(calls)==1 and calls[0]['opportunity_id']==new
    assert vet(databases,old)['reason']=='prompt_version_mismatch'
    assert vet(databases,new)['verdict']=='ENTER'


def test_old_v13_saved_soft_facts_recover_unchanged_without_another_call(databases):
    from datetime import timedelta
    tid,cohort=ready(databases,'old-saved',prompt='desk-judge-v13')
    clock=Clock();started=clock().isoformat();done=(clock()+timedelta(seconds=1)).isoformat()
    soft={'cluster':{'state':'ready','check':'pass','reason':'cluster_clear','top_funder_high':False,
                     'top_funder_pct':12.,'clustered_pct':18.},
          'holders':{'state':'ready','check':'pass','reason':'largest_accounts','top10_pct':20.,
                     'creator_pct':3.,'creator_basis':'top20'}}
    result={'text':enter({'opportunity_id':tid}),'is_error':False,'call_error':None,
        'session_id':'prior-session','result_id':'prior-result','model_echo':J.MODEL,
        'usage':{'input_tokens':4,'output_tokens':60,'cache_read_input_tokens':4000,'cache_creation_input_tokens':0},
        'notional_usd':.0004,'billing_basis':'subscription_flat','soft':soft}
    assert D.claim_judge_call(databases[0],ticket_id=tid,call_id='prior',worker='prior',claimed_at=started)
    D.save_judge_response(databases[0],call_id='prior',call_started_at=started,response_at=done,
                         elapsed_ms=1000,result=result)
    out=J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)
    assert out[0]['action']=='ENTER'
    assert vet(databases,tid)['inputs']['sheet']['evidence']==soft
    assert L.get_desk_cohort_cost(databases[1],cohort,'desk-judge:'+tid)['usd']==0
    assert J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)==[]
