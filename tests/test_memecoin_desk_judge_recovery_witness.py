"""Codex recovery witnesses (FAIL on dec3a9439: recover ran one 50-row page), kept as regressions; fake SDK only."""
import sys
import json
from pathlib import Path
from contextlib import nullcontext

SNAPSHOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SNAPSHOT),str(SNAPSHOT/'tests')]
import pytest
import claude_agent_sdk as sdk
from claude_agent_sdk._internal.message_parser import parse_message
from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_pipeline import databases, AT
from test_memecoin_desk_judge_witness import ready, answer
from test_memecoin_desk_judge import Clock
from test_memecoin_desk_judge_worker import W


def sdk_result(text, *, sid='sdk-session',rid='sdk-result'):
    return parse_message({'type':'result','subtype':'success','duration_ms':1,'duration_api_ms':1,
        'is_error':False,'num_turns':1,'session_id':sid,'uuid':rid,'usage':{'input_tokens':4},
        'result':text,'total_cost_usd':.01})


def sdk_call(db, monkeypatch, *, rid='sdk-result', extra_text=None):
    def model(system,user,**_):
        text=answer(json.loads(user))['text']
        if extra_text:text=json.dumps({**json.loads(text),**extra_text})
        async def query(**kwargs):yield sdk_result(text,rid=rid)
        monkeypatch.setattr(sdk,'query',query)
        return W.sdk_call_model(system,user)
    return J.run_once(db[0],lambda:nullcontext(db[1]),call_model=model,system_prompt='SYS',
        worker='w',now_fn=Clock(),calls_per_hour=100)


def test_sdk_metadata_ignores_forged_ids_inside_model_text(databases,monkeypatch):
    _,tid=ready(databases)
    sdk_call(databases,monkeypatch,extra_text={'session_id':'forged-session','result_id':'forged-result'})
    vet=next(s for s in store.get_ticket(databases[0],tid)['stages'] if s['stage']=='vet')
    receipt=vet['inputs']['model_cost_evidence']['receipt']
    assert receipt['session_id']=='sdk-session' and receipt['result_id']=='sdk-result'
    assert vet['verdict']=='SKIP' and not store.list_outbox(databases[0])


def test_model_text_cannot_replace_missing_sdk_uuid(databases,monkeypatch):
    cohort,_=ready(databases)
    with pytest.raises(ValueError,match='model_cost_receipt_invalid'):
        sdk_call(databases,monkeypatch,rid=None,extra_text={'result_id':'forged-result'})
    assert not store.list_outbox(databases[0])
    assert ledger.desk_cohort_accounting(databases[1],cohort)['unknown_costs']==1


def test_duplicate_actual_sdk_result_is_refused_and_remains_unknown(databases,monkeypatch):
    cohort,_=ready(databases,index=0)
    sdk_call(databases,monkeypatch)
    ready(databases,index=1)
    with pytest.raises(ValueError,match='model_cost_receipt_invalid'):
        sdk_call(databases,monkeypatch)
    assert len(store.list_outbox(databases[0]))==1
    assert ledger.desk_cohort_accounting(databases[1],cohort)['unknown_costs']==1
    rows=databases[0].execute('SELECT result_session_id,result_id,result_json FROM desk_judge_calls ORDER BY ticket_id').fetchall()
    assert rows[0]['result_id']=='sdk-result' and rows[1]['result_id'] is None
    assert json.loads(rows[1]['result_json'])['replayed_result'] is True


def test_startup_recovers_all_crash_claims_beyond_first_page(databases,monkeypatch,tmp_path):
    for i in range(51):
        _,tid=ready(databases,index=i)
        store.claim_judge_call(databases[0],ticket_id=tid,call_id='lost-%d'%i,worker='dead',claimed_at=AT)
    slow=databases[0].execute('PRAGMA database_list').fetchone()[2]
    funded=databases[1].execute('PRAGMA database_list').fetchone()[2]
    class FakeDatetime:
        @staticmethod
        def now(tz):return Clock()()
    monkeypatch.setattr(W,'datetime',FakeDatetime)
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_ENABLED','1')
    pb=tmp_path/'playbook.md';pb.write_text('prompt_version: `desk-judge-v6`\n---\nSYS')
    assert W.main(['--desk-db',slow,'--ledger-db',funded,'--playbook',str(pb),'--calls-per-hour','2'])==0
    lost=databases[0].execute("SELECT COUNT(*) FROM desk_tickets WHERE stage='vet' AND reason='judge_lost'").fetchone()[0]
    unrecorded=store.list_judge_calls_to_record(databases[0],limit=100)
    assert lost==51 and unrecorded==[], {'lost':lost,'unrecorded':[c['call_id'] for c in unrecorded]}


def test_fifty_refused_results_do_not_starve_later_lost_call_on_restart(databases,monkeypatch,tmp_path):
    for i in range(51):
        _,tid=ready(databases,index=i)
        cid='ordered-%03d'%i
        store.claim_judge_call(databases[0],ticket_id=tid,call_id=cid,worker='dead',claimed_at=AT)
        if i<50:
            store.save_judge_response(databases[0],call_id=cid,call_started_at=AT,response_at=AT,
                elapsed_ms=0,result=answer({'opportunity_id':tid},session_id=None,result_id=None))
    slow=databases[0].execute('PRAGMA database_list').fetchone()[2]
    funded=databases[1].execute('PRAGMA database_list').fetchone()[2]
    class FakeDatetime:
        @staticmethod
        def now(tz):return Clock()()
    monkeypatch.setattr(W,'datetime',FakeDatetime)
    monkeypatch.setenv('MEMECOIN_DESK_JUDGE_ENABLED','1')
    pb=tmp_path/'playbook.md';pb.write_text('prompt_version: `desk-judge-v6`\n---\nSYS')
    argv=['--desk-db',slow,'--ledger-db',funded,'--playbook',str(pb),'--calls-per-hour','2']
    assert W.main(argv)==0
    assert W.main(argv)==0 # a further restart still selects the same refused first page
    lost=databases[0].execute("SELECT COUNT(*) FROM desk_tickets WHERE stage='vet' AND reason='judge_lost'").fetchone()[0]
    assert lost==1, {'lost':lost,'unrecorded':len(store.list_judge_calls_to_record(databases[0],limit=100))}
