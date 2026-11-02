"""Unpriceable PS decisions persist without a model claim, budget charge or funded-ledger handle."""
import copy
import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.persistence import db_memecoin as ledger
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_contract as K
from test_desk_ps_snapshot_contract import enriched
from test_memecoin_desk_contract import AT, rehash
from test_memecoin_desk_judge import Clock, databases, model_returning, enter  # noqa: F401
from test_memecoin_desk_pipeline import api, POLICY


def ready(db):
    cohort=api().provision_cohort(*db,producer='U',policy={**POLICY,'venue':'pumpswap',
        'judge_p_admit':1.0,'select_version':J.SELECT_VERSION},prompt_version='desk-judge-v18',created_at=AT)
    c=enriched()
    ticket=api().ingest_candidate(db[0],c,cohort_id=cohort,now_iso=AT)['ticket_id']
    store.record_stage(db[0],ticket_id=ticket,stage='hard_filter',seat='code',verdict='PASS',
        reason='fixture',inputs={},output={},ts=AT)
    return ticket,cohort


def refreshed(candidate,ts):
    c=copy.deepcopy(candidate)
    c['emitted_at']=datetime.fromtimestamp(ts,timezone.utc).isoformat()
    c['snapshot']['emission_at']=ts
    return rehash(c),{'fixture':True}


def forbidden(*args,**kwargs):
    raise AssertionError('unpriceable PS reached a model or funded-ledger handle')


def test_unpriceable_asof_persists_once_without_a_claim_budget_or_cost(databases,monkeypatch):
    ticket,cohort=ready(databases); clock=Clock();seen=[]
    def asof(c,ts): seen.append(ts);return refreshed(c,ts)
    monkeypatch.setattr(J.desk,'pumpswap_unpriced_reason',lambda c:'virtual_quote_unproven')
    run=lambda:J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='fixture',
        worker='w',now_fn=clock,calls_per_hour=0,asof_fn=asof,soft_fn=forbidden)
    out=run()
    assert [(o['ticket_id'],o['action'],o['reason']) for o in out]==[(ticket,'SKIP','virtual_quote_unproven')]
    assert len(seen)==1 and run()==[]
    vet=next(s for s in store.get_ticket(databases[0],ticket)['stages'] if s['stage']=='vet')
    assert vet['verdict']=='SKIP' and vet['cost_usd']==0 and vet['inputs']['asof']['candidate']['venue']=='pumpswap'
    assert store.get_admission(databases[0],ticket_id=ticket,arm='F')['decision']=='not_selected'
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls').fetchone()[0]==0
    assert store.judge_calls_since(databases[0],since_iso=AT)==0
    assert ledger.get_desk_cohort_cost(databases[1],cohort,'desk-judge:'+ticket) is None


def test_missing_frozen_asof_refuses_even_when_emission_state_prices(databases,monkeypatch):
    ticket,_=ready(databases)
    monkeypatch.setattr(J.desk,'pumpswap_unpriced_reason',lambda c:None)
    out=J.run_once(databases[0],forbidden,call_model=forbidden,system_prompt='fixture',
        worker='w',now_fn=Clock(),calls_per_hour=100)
    assert out[0]['reason']=='ps_asof_required'
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls').fetchone()[0]==0


def test_priceable_ps_refreshes_once_and_reuses_that_snapshot_for_the_model(databases):
    ticket,_=ready(databases);clock=Clock();seen=[];calls=[]
    def asof(c,ts): seen.append(ts);return refreshed(c,ts)
    def skip(sheet):
        return json.dumps({'k':sheet['opportunity_id'],'v':'S','e':None,'r':None,'c':1,
                           'enter_ttl_s':5,'next_check_s':15})
    J.run_once(databases[0],lambda:nullcontext(databases[1]),
        call_model=model_returning(skip,calls=calls,clock=clock),system_prompt='fixture',
        worker='w',now_fn=clock,calls_per_hour=100,asof_fn=asof)
    assert len(seen)==len(calls)==1
    vet=next(s for s in store.get_ticket(databases[0],ticket)['stages'] if s['stage']=='vet')
    assert vet['inputs']['asof']['candidate']['snapshot']['emission_at']==seen[0]
    assert calls[0]==vet['inputs']['sheet']


def refusal(db,ticket,reason='reserve_stale'):
    return store.record_ps_preflight_skip(db,ticket_id=ticket,reason=reason,
        inputs={'fixture':'same'},ts=AT)


def test_refusal_is_idempotent_and_conflicts_cannot_rewrite_it(databases):
    ticket,_=ready(databases)
    assert refusal(databases[0],ticket) is True
    before=store.get_ticket(databases[0],ticket)
    assert refusal(databases[0],ticket) is True
    with pytest.raises(ValueError,match='stage_conflict'):refusal(databases[0],ticket,'fee_unknown')
    assert store.get_ticket(databases[0],ticket)==before
    assert not store.claim_judge_call(databases[0],ticket_id=ticket,call_id='late',worker='late',claimed_at=AT)
    assert databases[0].execute('SELECT COUNT(*) FROM desk_judge_calls').fetchone()[0]==0


def test_racing_model_claim_wins_without_being_reclassified(databases):
    ticket,_=ready(databases)
    assert store.claim_judge_call(databases[0],ticket_id=ticket,call_id='other',worker='other',claimed_at=AT)
    assert refusal(databases[0],ticket) is False
    assert store.get_admission(databases[0],ticket_id=ticket,arm='F') is None
    assert all(s['stage']!='vet' for s in store.get_ticket(databases[0],ticket)['stages'])


def test_racing_claim_with_completed_vet_wins_without_a_conflict(databases):
    ticket,_=ready(databases)
    assert store.claim_judge_call(databases[0],ticket_id=ticket,call_id='other',worker='other',claimed_at=AT)
    store.record_stage(databases[0],ticket_id=ticket,stage='vet',seat='judge',verdict='SKIP',
        reason='other_result',inputs={'other':'completed'},output={},ts=AT)
    before=store.get_ticket(databases[0],ticket)
    assert refusal(databases[0],ticket) is False
    assert store.get_ticket(databases[0],ticket)==before
    assert store.get_admission(databases[0],ticket_id=ticket,arm='F') is None


def test_failed_refusal_insert_rolls_back_its_admission(databases):
    ticket,_=ready(databases)
    databases[0].execute("CREATE TRIGGER fail_ps_vet BEFORE INSERT ON desk_tickets WHEN NEW.stage='vet' "
                        "BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='fixture insert failure'):refusal(databases[0],ticket)
    assert store.get_admission(databases[0],ticket_id=ticket,arm='F') is None
    assert store.get_ticket(databases[0],ticket)['state']=='open'


def test_existing_funded_admission_cannot_be_changed_to_a_refusal(databases):
    ticket,_=ready(databases)
    store.record_admission(databases[0],ticket_id=ticket,arm='F',decision='taken',decided_at=AT)
    with pytest.raises(ValueError,match='admission_conflict'):refusal(databases[0],ticket)
    assert store.get_admission(databases[0],ticket_id=ticket,arm='F')['decision']=='taken'
    assert all(s['stage']!='vet' for s in store.get_ticket(databases[0],ticket)['stages'])


def test_ps_recovery_reuses_frozen_soft_facts_and_price_without_a_new_read(databases):
    ticket,_=ready(databases);clock=Clock();calls=[];reads=[]
    facts=K.soft_facts({'cluster':{'status':'unknown','reason':'not_applicable'},'holders':{
        'status':'kill','reason':'top10_high','top10_pct':95,'creator_pct':25,
        'creator_basis':'top20','exclusions_verified':True}})
    def soft(_):
        reads.append(True);return copy.deepcopy(facts)
    def crash(*args,**kwargs):
        raise RuntimeError('after response save')
    with pytest.raises(RuntimeError,match='after response save'):
        J.run_once(databases[0],lambda:nullcontext(databases[1]),system_prompt='offline',worker='test',
            now_fn=clock,calls_per_hour=100,asof_fn=refreshed,soft_fn=soft,record_fn=crash,
            expected_prompt_version='desk-judge-v18',
            call_model=model_returning(enter,calls=calls,clock=clock))
    facts['holders']['top10_pct']=0
    J.recover(databases[0],lambda:nullcontext(databases[1]),now_fn=clock)
    vet=next(s for s in store.get_ticket(databases[0],ticket)['stages'] if s['stage']=='vet')
    assert len(reads)==len(calls)==1 and calls[0]==vet['inputs']['sheet']
    assert vet['inputs']['sheet']['evidence']['holders']['top10_pct']==95
    assert vet['inputs']['sheet']['evidence']['cluster']['reason']=='not_applicable'
    assert store.list_outbox(databases[0])[0]['payload']['desk_metadata']['verdict_price']==calls[0]['verdict_price']
