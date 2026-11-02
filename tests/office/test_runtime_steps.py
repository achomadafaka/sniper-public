"""Protocol fixtures for live scheduling; no providers or runtime services."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import UUID
import pytest
from sniper.office import store
from sniper.office import runtime_steps as R
from sniper.office.scheduler import Scheduler
from sniper.office.supervisor import Context

CADENCE={'role_cadence_s':{'Head':60,'Scout':60,'Judge':60}}
PIN=hashlib.sha256(b'fixture prompt').hexdigest()


def actor():
    return dict(agent_id='head',role='Head',family='claude',provider='claude',model='claude-opus-5-5',
                generation=1,state='active',prompt_hash=PIN,policy_hash='policy')


def context():
    return Context('head','head','pilot-floor',1,1,'turn','turn','policy',PIN,
                   {'basis':'corrected_utc','receipt_cut':1000.,'knowledge_cut':1000.,'slot_cut':None,
                    'clock_sample':{'corrected_utc':1000.,'offset_s':0.}},1030.)


def test_due_work_reads_durable_completion_and_restart_slot(tmp_path):
    path=tmp_path/'office.db';conn=store.open_store(path)
    conn.execute('INSERT INTO agents VALUES(?,?)',('head',json.dumps(actor())));conn.commit()
    due=R.DueWork(path,CADENCE,clock=lambda:1000.)
    first=due.tasks();assert len(first)==1
    conn.execute('INSERT INTO turn_runs VALUES(?,?)',(first[0]['turn_id'],json.dumps({'state':'completed'})));conn.commit()
    assert due.tasks()==[] and R.DueWork(path,CADENCE,clock=lambda:1000.).tasks()==[]
    assert R.DueWork(path,CADENCE,clock=lambda:1060.).tasks()[0]['turn_id']!=first[0]['turn_id']
    conn.close()


def test_running_live_work_is_backlog_even_when_not_due(tmp_path):
    path=tmp_path/'office.db';conn=store.open_store(path)
    conn.execute('INSERT INTO turn_runs VALUES(?,?)',('inflight',json.dumps({'state':'running','deadline_at':1030.})));conn.commit()
    due=R.DueWork(path,CADENCE,clock=lambda:1000.)
    assert due.live_backlogged() is True
    conn.close()


def test_unreadable_live_work_is_unknown_not_empty(tmp_path):
    with pytest.raises(R.RuntimeRefused):R.DueWork(tmp_path/'missing.db',CADENCE,clock=lambda:1000.).tasks()


class Client:
    def __init__(self,result=None):self.calls=[];self.ids=set();self.result=result
    def call(self,adapter,doc,*,deadline_at,request_id):
        self.calls.append((adapter,doc,deadline_at,request_id))
        if request_id in self.ids:return {'call_error':'existing_unknown'}
        self.ids.add(request_id)
        return self.result or {'text':json.dumps({'done':True,'task':'Review','memo':'Unknown data','requests':[],'refs':None}),
                               'model_echo':doc['model']}


def runner(client):
    transport=SimpleNamespace(client=client,adapter='claude_sdk')
    return R.AgentStep(models_manifest={'seats':{'head':{'provider':'claude','model':'claude-opus-5-5'}}},
        prompts={'head':{'text':'fixture prompt','sha256':PIN}},scheduler=Scheduler({'claude':1,'codex':1},2),
        transports={'claude':transport},clock=lambda:1000.,actor=lambda _:actor(),
        observe=lambda _: {'state':'unknown','reason':'fixture_no_candidates'},read_tool=None,
        context_check=lambda ctx:ctx.generation==1)


def test_model_request_is_pinned_and_crash_retry_reuses_request_identity():
    client=Client();step=runner(client)
    first=step(context(),{},10.)
    assert first.done and first.actions[0].kind=='memo.commit'
    assert client.calls[0][1]['model']=='claude-opus-5-5'
    assert str(UUID(client.calls[0][3]))==client.calls[0][3]
    again=step(context(),{},10.)
    assert again.source_state=='partial' and not again.actions
    assert client.calls[0][3]==client.calls[1][3]


def test_unknown_served_model_cannot_commit_memo():
    client=Client({'text':json.dumps({'done':True,'task':'Review','memo':'x','requests':[],'refs':None}), 'model_echo':None})
    result=runner(client)(context(),{},10.)
    assert result.source_state=='off' and not result.actions


def test_provider_error_category_reaches_existing_ui_reason_field():
    result=runner(Client({'call_error':'provider_error','provider_error_kind':'schema',
        'provider_error_detail':'additionalProperties missing'}))(context(),{},10.)
    assert result.done and not result.actions and result.source_state=='off'
    assert result.value['reason']=='provider_error_schema'
    assert result.value['provider_error_kind']=='schema'
    assert result.value['provider_error_detail']=='additionalProperties missing'


def test_replaced_context_refuses_before_provider():
    from dataclasses import replace
    client=Client();result=runner(client)(replace(context(),generation=2),{},10.)
    assert not client.calls and result.source_state=='off'


def test_structured_output_objects_are_closed_and_fields_required():
    def check(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node.get('additionalProperties') is False
                assert set(node.get('required', [])) == set(node.get('properties', {}))
            for value in node.values():check(value)
        elif isinstance(node, list):
            for value in node:check(value)
    check(R.SCHEMA)


def test_request_schema_discriminates_actions_and_rejects_unknown_fields():
    from jsonschema import Draft202012Validator
    validator=Draft202012Validator(R.SCHEMA)
    variants=R.SCHEMA['properties']['requests']['items']['anyOf']
    assert {v['properties']['tool']['const'] for v in variants} == {'org.read','health.read','grade.read','desk.near_pass_list','desk.near_pass_sheet',*R.WRITE_TOOLS}
    def valid(tool,args):
        return validator.is_valid(dict(done=False,task='review',memo='unknown',refs=None,requests=[dict(tool=tool,args=args)]))
    assert valid('org.read',{}) and valid('health.read',{})
    assert valid('ticket.propose',dict(problem='Missing source',expected_impact=None,evidence_refs=['receipt-1'],done_when='Source available'))
    assert valid('ticket.approve',dict(ticket_id='ticket-1'))
    assert valid('org.propose',dict(kind='persona',proposal='A proposed change',rationale='Testable reason'))
    assert not valid('org.read',{'actor_id':'other'})
    assert not valid('ticket.approve',{'ticket_id':'ticket-1','authority':'operator'})
    assert not valid('ticket.propose',{'problem':'Missing fields'})
    assert not valid('shell',{})


def test_written_work_metadata_and_memo_refs_survive_step():
    client=Client();step=runner(client)
    step.observe=lambda _: {'work_context':{'dm_context':True,'dm_count':1,'unread_backlog':2,'task_context':True}}
    step.validate_memo_refs=lambda ctx,refs:refs
    result=step(context(),{},10.)
    assert result.value['dm_context'] is True and result.value['dm_unread_backlog']==2
    assert result.value['ref_context'] is False and result.value['refs_count']==0
    assert result.actions[0].payload['refs']==[]


def test_unsupplied_memo_ref_refuses_before_preparing_action():
    answer=dict(done=True,task='review',memo='unknown',requests=[],refs=[{'kind':'decision','receipt_id':'a'*64}])
    step=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def refuse(ctx,refs):raise ValueError('handoff_reference_not_supplied')
    step.validate_memo_refs=refuse
    result=step(context(),{},10.)
    assert not result.actions and result.value['refused_refs_reason']=='handoff_reference_not_supplied'
    assert 'a'*64 not in json.dumps(result.value)


def test_invalid_send_is_refused_before_action_intent():
    answer=dict(done=False,task='send',memo='x',refs=None,requests=[{'tool':'msg.send','args':{'to_actor_id':'missing','text':'x','refs':None}}])
    step=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def refuse(*a):raise ValueError('work_actor_unknown')
    step.validate_action=refuse
    result=step(context(),{},10.)
    assert not result.actions and result.value['refused_send_reason']=='work_actor_unknown'


def test_desk_read_failure_retains_safe_exception_detail(tmp_path,monkeypatch):
    from sniper.persistence import db_memecoin_desk as desk
    path=tmp_path/'desk.db';sqlite3.connect(path).close()
    def fail(*a,**k):raise sqlite3.OperationalError('interrupted Authorization: Bearer secret-token')
    monkeypatch.setattr(desk,'list_awaiting_judge',fail)
    with pytest.raises(R.RuntimeRefused) as error:
        R.DueWork(path,CADENCE,clock=lambda:1000.,desk_path=path).desk_backlogged()
    assert error.value.detail['exception_class']=='OperationalError'
    assert 'interrupted' in error.value.detail['message']
    assert 'secret-token' not in json.dumps(error.value.detail)


def test_forced_desk_read_error_leaves_same_slot_due_but_bounds_retries(tmp_path,monkeypatch):
    from sniper.office import supervisor as S
    from sniper.office.scheduler import GuardRefused
    from sniper.persistence import db_memecoin_desk as desk
    path=tmp_path/'office.db';conn=store.open_store(path)
    conn.execute('INSERT INTO agents VALUES(?,?)',('head',json.dumps(actor())));conn.commit()
    now=[1000.];clock=lambda:now[0]
    due=R.DueWork(path,CADENCE,clock=clock,desk_path=path)
    def fail(*a,**k):raise sqlite3.OperationalError('interrupted')
    monkeypatch.setattr(desk,'list_awaiting_judge',fail)
    def guard():
        try:due.desk_backlogged()
        except R.RuntimeRefused as exc:raise GuardRefused(str(exc),exc.detail) from None
    client=Client();step=runner(client);step.clock=clock
    step.scheduler=Scheduler({'claude':1,'codex':1},2,external_guard=guard)
    lease=S.Lease(conn,clock=clock,ttl_s=100);gen=lease.acquire('fixture')
    supervisor=S.Supervisor(conn,lease=lease,clock=clock,step=step,
        freeze_cut=lambda:context().cut)
    task=due.tasks()[0]
    for attempt in range(3):
        result=supervisor.run_turn('head',gen=gen,deadline_s=30,max_steps=4,turn_id=task['turn_id'])
        assert result.value['guard_error']['exception_class']=='OperationalError'
        assert result.state==('deferred' if attempt<2 else 'refused')
        assert due.tasks()==[]
        now[0]+=5
        assert bool(due.tasks())==(attempt<2)
    assert client.calls==[]
    again=supervisor.run_turn('head',gen=gen,deadline_s=30,max_steps=4,turn_id=task['turn_id'])
    assert again.state=='refused'
    stored=json.loads(conn.execute('SELECT payload_json FROM turn_runs WHERE id=?',(task['turn_id'],)).fetchone()[0])
    assert stored['guard_attempts']==3
    conn.close()


def test_deferred_turn_survives_cadence_rollover_and_reader_restart(tmp_path):
    from sniper.office import supervisor as S
    path=tmp_path/'office.db';conn=store.open_store(path)
    conn.execute('INSERT INTO agents VALUES(?,?)',('head',json.dumps(actor())));conn.commit()
    now=[1199.];clock=lambda:now[0];due=R.DueWork(path,CADENCE,clock=clock)
    old=due.tasks()[0];lease=S.Lease(conn,clock=clock,ttl_s=100);gen=lease.acquire('fixture')
    step=lambda *a:S.Step(done=True,source_state='off',value={'reason':'desk_judge_work_unknown','retryable_pre_dispatch':True})
    sup=S.Supervisor(conn,lease=lease,clock=clock,step=step,freeze_cut=lambda:context().cut)
    assert sup.run_turn('head',gen=gen,deadline_s=30,max_steps=4,turn_id=old['turn_id']).state=='deferred'
    now[0]=1200.
    assert R.DueWork(path,CADENCE,clock=clock).tasks()==[]  # waiting old slot suppresses new slot
    now[0]=1204.
    tasks=R.DueWork(path,CADENCE,clock=clock).tasks()
    assert len(tasks)==1 and tasks[0]['turn_id']==old['turn_id']
    resumed=S.Supervisor(conn,lease=lease,clock=clock,step=lambda *a:S.Step(done=True,value={'ok':True}),
        freeze_cut=lambda: (_ for _ in ()).throw(AssertionError('must reuse frozen cut')))
    assert resumed.run_turn('head',gen=gen,deadline_s=30,max_steps=4,turn_id=tasks[0]['turn_id']).state=='completed'
    record=json.loads(conn.execute('SELECT payload_json FROM turn_runs WHERE id=?',(old['turn_id'],)).fetchone()[0])
    assert record['deadline_at']==1229. and record['cut']==context().cut
    plan=conn.execute("EXPLAIN QUERY PLAN SELECT id,payload_json FROM turn_runs INDEXED BY turn_runs_deferred_deadline WHERE json_extract(payload_json,'$.state')='deferred' AND json_extract(payload_json,'$.deadline_at')>? ORDER BY json_extract(payload_json,'$.deadline_at'),id LIMIT 5",(now[0],)).fetchall()
    assert any('SEARCH' in row[3] and 'turn_runs_deferred_deadline' in row[3] for row in plan)
    assert not any('TEMP' in row[3] for row in plan)
    now[0]=1230.
    tasks=R.DueWork(path,CADENCE,clock=clock).tasks()
    assert len(tasks)==1 and tasks[0]['turn_id']!=old['turn_id']
    conn.close()


def batch_answer(*targets,done=True):
    return {'done':done,'task':'assign','memo':'Assignments requested; delivery unverified','refs':None,
        'requests':[{'tool':'msg.send','args':{'to_actor_id':t,'text':'assignment','refs':None}} for t in targets]}


def test_head_final_step_accepts_distinct_recipients_and_task_update():
    answer=batch_answer('scout','judge-claude','judge-gpt')
    answer['requests'].append({'tool':'task.update','args':{'task_id':'a'*64,'status':'open','note':'await receipts'}})
    step=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))(context(),{},10.)
    assert step.done and step.source_state=='observed'
    assert [a.kind for a in step.actions]==['msg.send']*3+['task.update','memo.commit']


def test_duplicate_recipient_refuses_entire_final_batch():
    answer=batch_answer('scout','scout')
    step=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))(context(),{},10.)
    assert not step.actions and step.value['reason']=='duplicate_mutation_kind'


def test_host_mutation_budget_and_recipients_survive_steps():
    client=Client({'text':json.dumps(batch_answer('a','b','c','d','e','f',done=False)),
        'model_echo':'claude-opus-5-5'});r=runner(client)
    first=r(context(),{},10.);assert len(first.actions)==6
    client.result={'text':json.dumps(batch_answer('g')),'model_echo':'claude-opus-5-5'}
    second=r(context(),first.value,10.)
    assert not second.actions and second.value['reason']=='mutation_budget'
    assert second.value['request_admission']['requested_n']==7 and second.value['request_admission']['refused_n']==1
    client=Client({'text':json.dumps(batch_answer('a')),'model_echo':'claude-opus-5-5'})
    previous=dict(first.value);previous['_runtime_mutation_keys']=['msg.send:a']
    step=runner(client)(context(),previous,10.)
    assert not step.actions and step.value['reason']=='duplicate_mutation_kind'


@pytest.mark.parametrize('targets,reason',[(['a']*2,'duplicate_mutation_kind'),(list('abcdefg'),'mutation_budget')])
def test_refused_batch_has_typed_request_admission_counts(targets,reason):
    step=runner(Client({'text':json.dumps(batch_answer(*targets)),'model_echo':'claude-opus-5-5'}))(context(),{},10.)
    assert step.value['reason']==reason
    assert step.value['request_admission']=={'state':'known','requested_n':len(targets),
        'refused_n':len(targets),'truncated':False,'truncated_n':0}


def test_validation_refusal_counts_all_requests_before_any_effect():
    r=runner(Client({'text':json.dumps(batch_answer('a','b')),'model_echo':'claude-opus-5-5'}))
    def deny(*a):raise ValueError('work_recipient_unknown')
    r.validate_action=deny
    step=r(context(),{},10.)
    assert not step.actions and step.value['request_admission']['refused_n']==2
    assert step.value['request_admission']['truncated'] is False


def test_unparsed_provider_output_keeps_truncation_and_counts_unknown():
    step=runner(Client({'call_error':'invalid_result'}))(context(),{},10.)
    assert step.value['request_admission']=={'state':'unknown','requested_n':None,
        'refused_n':None,'truncated':None,'truncated_n':None}


def test_missing_read_binding_is_counted_as_refused_not_admitted():
    answer=batch_answer(done=False);answer['requests']=[{'tool':'org.read','args':{}}]
    step=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))(context(),{},10.)
    assert step.value['request_admission']['refused_n']==1


def test_broker_exception_keeps_request_effects_unknown():
    answer=batch_answer(done=False);answer['requests']=[{'tool':'org.read','args':{}}]
    r=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def fail(*a):raise ValueError('read failed')
    r.read_tool=fail
    step=r(context(),{},10.)
    assert step.value['request_admission']['state']=='unknown'
    assert step.value['request_admission']['refused_n'] is None


def test_deadline_after_a_broker_read_keeps_batch_admission_unknown(monkeypatch):
    from sniper.office.broker import ToolResult
    elapsed=[0.];reads=[]
    monkeypatch.setattr(R,'time',SimpleNamespace(monotonic=lambda:elapsed[0]))
    answer=batch_answer('scout',done=False)
    answer['requests'].insert(0,{'tool':'org.read','args':{}})
    r=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def read(ctx,tool,args):
        reads.append(tool);elapsed[0]=11.
        return ToolResult({}, {},None,[],'fixture read')
    r.read_tool=read
    step=r(context(),{},10.)
    assert reads==['org.read'] and not step.actions
    assert step.source_state=='partial' and step.value['reason']=='deadline'
    assert step.value['request_admission']=={'state':'unknown','requested_n':None,
        'refused_n':None,'truncated':None,'truncated_n':None}


@pytest.mark.parametrize('read_first',[False,True])
def test_mixed_final_batch_refuses_before_any_read_or_write(read_first):
    answer=batch_answer('a');read={'tool':'org.read','args':{}}
    answer['requests'].insert(0 if read_first else 1,read)
    events=[]
    r=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def read_tool(*a):
        from sniper.office.broker import ToolResult
        events.append('read');return ToolResult({}, {},None,[],'fixture')
    r.read_tool=read_tool
    step=r(context(),{},10.)
    assert not events and not step.actions
    assert step.value['reason']=='mixed_final_requests'
    assert step.value['request_admission']=={'state':'known','requested_n':2,
        'refused_n':2,'truncated':False,'truncated_n':0}


@pytest.mark.parametrize('tool,args',[('org.read',{}),('health.read',{}),
    ('desk.near_pass_list',{}),('desk.near_pass_sheet',{'ticket_id':'ticket'})])
def test_read_only_final_batch_reads_then_commits_memo(tool,args):
    answer=batch_answer();answer['requests']=[{'tool':tool,'args':args}]
    events=[]
    r=runner(Client({'text':json.dumps(answer),'model_echo':'claude-opus-5-5'}))
    def read_tool(*a):
        from sniper.office.broker import ToolResult
        events.append('read');return ToolResult({}, {},None,[],'fixture')
    r.read_tool=read_tool
    step=r(context(),{},10.)
    assert events==['read'] and [action.kind for action in step.actions]==['memo.commit']
    assert step.done and step.source_state=='observed'
    assert step.value['request_admission']=={'state':'known','requested_n':1,
        'refused_n':0,'truncated':False,'truncated_n':0}


def test_eval_cohort_tickets_are_not_desk_backlog(tmp_path,monkeypatch):
    from sniper.persistence import db_memecoin_desk as desk
    monkeypatch.setattr(desk,'get_ticket',lambda conn,tid:{'ticket_id':tid})   # fixture rows carry no emission
    path=tmp_path/'desk.db';conn=desk.connect(path)
    conn.execute("INSERT INTO desk_cohorts VALUES('C','U','v','{}','2026-10-29T00:00:00Z')")
    conn.execute("INSERT INTO desk_cohorts VALUES('D','U','v','{}','2026-10-29T00:00:00Z')")
    conn.execute("INSERT INTO desk_snapshots VALUES('s','{}')")
    for tid,cohort in (('t1','C'),):
        conn.execute("INSERT INTO desk_ticket_heads(ticket_id,opportunity_id,cohort_id,producer,network_id,mint,pool,snapshot_hash,emitted_at,created_at,last_seen_at,state) "
                     "VALUES(?,?,?,'U','solana',?,NULL,'s','2026-10-29T00:00:00Z','2026-10-29T00:00:00Z','2026-10-29T00:00:00Z','open')",(tid,'o'+tid,cohort,'m'+tid))
        conn.execute("INSERT INTO desk_tickets(ticket_id,opportunity_id,cohort_id,producer,network_id,mint,stage,seat,verdict,reason,inputs_hash,inputs_json,ts) "
                     "VALUES(?,?,?,'U','solana',?,'hard_filter','hf','PASS','ok','h','{}','2026-10-29T00:00:01Z')",(tid,'o'+tid,cohort,'m'+tid))
    conn.commit();conn.close()
    cohort=['C']
    due=R.DueWork(path,CADENCE,clock=lambda:1000.,desk_path=path,exclude_cohort=lambda:cohort[0])
    assert due.desk_backlogged() is False          # eval-1's own cohort never holds the models back
    cohort[0]=None
    assert due.desk_backlogged() is True           # without the exclusion it is backlog (today's live symptom)
    cohort[0]='D'
    assert due.desk_backlogged() is True           # another cohort's PASS ticket still is
