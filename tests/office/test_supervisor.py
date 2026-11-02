import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import pytest
from sniper.office import store
from sniper.office import supervisor as S


class Clock:
    def __init__(self): self.now=1000.
    def __call__(self): return self.now


@pytest.fixture
def office(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    clock=Clock()
    conn.execute('INSERT INTO agents VALUES(?,?)',('agent',json.dumps(dict(agent_id='agent',role='Trader',
        state='active',generation=1,policy_hash='p',prompt_hash='q',model='m',provider='codex'))))
    conn.commit()
    lease=S.Lease(conn,clock=clock,ttl_s=100)
    gen=lease.acquire('supervisor-a')
    yield conn,clock,lease,gen
    conn.close()


def make(office,step=None,**kw):
    conn,clock,lease,gen=office
    kw.setdefault('authorize',lambda ctx,action: (ctx.role=='trader' or action.kind=='ticket')
                  and action.payload.get('book_id',ctx.book_id)==ctx.book_id)
    return S.Supervisor(conn,lease=lease,clock=clock,
        step=step or (lambda context,previous,remaining_s:S.Step(done=True,value={'ok':True})),
        freeze_cut=lambda:{'basis':'corrected_utc','receipt_cut':clock(),'knowledge_cut':clock(),'slot_cut':None,
                           'clock_sample':{'corrected_utc':clock(),'offset_s':0}},**kw)


def test_restart_expires_orphan_without_inventing_a_completed_decision(office):
    conn,clock,lease,gen=office
    def crash(*args):raise SystemExit('process died')
    with pytest.raises(SystemExit):
        make(office,crash).run_turn('agent',gen=gen,deadline_s=2,max_steps=1,turn_id='orphan')
    original=S._get(conn,'turn_runs','orphan')
    clock.now+=3
    lease.release(gen);new_gen=lease.acquire('restart')
    supervisor=make(office)
    supervisor.reconcile(gen=new_gen)
    recovered=S._get(conn,'turn_runs','orphan')
    assert (recovered['state'],recovered['reason'])==('partial','deadline')
    for field in ('cut','deadline_at','steps','pending_step','lease_generation'):
        assert recovered[field]==original[field]
    clock.now+=1
    supervisor.run_turn('agent',gen=new_gen,deadline_s=2,max_steps=1,turn_id='fresh')
    frontier=supervisor.progress_frontiers()
    assert frontier['decisions_through']==clock.now
    assert frontier['state']=='healthy_empty' and frontier['partial_count']==1


def test_historical_partial_does_not_permanently_mark_fresh_work_partial(office):
    def late(*args):office[1].now+=3;return S.Step(done=True)
    make(office,late).run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='late')
    office[1].now+=1
    supervisor=make(office)
    supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='fresh')
    assert supervisor.progress_frontiers()['state']=='healthy_empty'
    assert supervisor.progress_frontiers()['partial_count']==1


def test_entrypoint_recovers_expired_running_turns_even_with_no_due_work(office):
    from scripts.office_supervisor import main
    conn,clock,lease,gen=office
    clock.now+=101
    def tasks():
        # Arrives after startup reconciliation; exercise the ordinary idle tick.
        S._put(conn,'turn_runs','late-orphan',dict(state='running',created_at=900.,deadline_at=901.,steps=0))
        return []
    runtime={'supervisor':make(office),'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert S._get(conn,'turn_runs','late-orphan')['state']=='partial'


def test_expiry_recovery_is_bounded_fenced_and_keeps_unknown_actions(office):
    conn,clock,lease,gen=office
    supervisor=make(office)
    for name,deadline in [('expired-a',900.),('expired-b',901.),('live',1001.),('unknown',None),('boolean',True)]:
        S._put(conn,'turn_runs',name,dict(state='running',created_at=700. if name.startswith('expired') else 800.,deadline_at=deadline,steps=0,pending_step={'actions':[]}))
    S._put(conn,'turn_runs','deferred',dict(state='deferred',created_at=800.,deadline_at=900.))
    S._put(conn,'action_requests','unknown-action',dict(state='prepared',created_at=800.))
    assert len(supervisor.reconcile_turns(gen=gen,limit=1))==1
    assert S._get(conn,'turn_runs','expired-b')['state']=='running'
    supervisor.reconcile_turns(gen=gen)
    assert S._get(conn,'turn_runs','live')['state']=='running'
    for name in ('unknown','boolean'):
        assert S._get(conn,'turn_runs',name)['reason']=='deadline_unknown'
    assert S._get(conn,'turn_runs','deferred')['state']=='deferred'
    assert S._get(conn,'action_requests','unknown-action')['state']=='prepared'
    assert supervisor.progress_frontiers()['state']=='stalled'
    before=S._get(conn,'turn_runs','live')
    clock.now+=101
    with pytest.raises(S.StaleGeneration):supervisor.reconcile_turns(gen=gen)
    assert S._get(conn,'turn_runs','live')==before


@pytest.mark.parametrize('failed',[True,False])
def test_returned_late_step_is_recorded_without_running_its_actions(office,failed):
    conn,clock,lease,gen=office
    effects=[]
    def step(*args):
        clock.now+=3
        return S.Step(done=True,actions=[S.Action('paper_submit',{})],
            value={'reason':'provider_error_schema','provider_error_kind':'schema'} if failed else {'memo':'late answer'},
            source_state='off' if failed else 'observed')
    supervisor=make(office,step,handlers={'paper_submit':lambda *a:effects.append(a)})
    result=supervisor.run_turn('agent',gen=gen,deadline_s=2,max_steps=1,turn_id='late')
    record=json.loads(conn.execute("SELECT payload_json FROM turn_runs WHERE id='late'").fetchone()[0])
    assert result.state=='partial' and result.reason=='deadline' and result.steps==1
    assert effects==[] and record['pending_step'] is None
    assert record['last_step']['done'] is True
    assert record['last_step']['actions'][0]['kind']=='paper_submit'
    assert record['value']['reason']==('provider_error_schema' if failed else 'deadline')
    assert record['source_state']==('off' if failed else 'partial')


def test_late_step_cannot_write_through_expired_lease(office):
    conn,clock,lease,gen=office
    def step(*args):
        clock.now+=101
        return S.Step(done=True,value={'reason':'timeout'},source_state='off')
    with pytest.raises(S.StaleGeneration):
        make(office,step).run_turn('agent',gen=gen,deadline_s=2,max_steps=1,turn_id='expired')
    record=json.loads(conn.execute("SELECT payload_json FROM turn_runs WHERE id='expired'").fetchone()[0])
    assert record['steps']==0 and 'last_step' not in record


def test_stale_generation_cannot_commit_after_takeover(office):
    conn,clock,lease,gen=office
    clock.now+=101
    new=S.Lease(conn,clock=clock,ttl_s=100);newgen=new.acquire('supervisor-b')
    assert newgen>gen
    with pytest.raises(S.StaleGeneration):
        with lease.guard(gen): conn.execute("INSERT INTO messages VALUES('forbidden','{}')")
    assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0


def test_live_lease_cannot_be_stolen_or_renewed_after_expiry(office):
    conn,clock,lease,gen=office
    with pytest.raises(S.LeaseBusy): S.Lease(conn,clock=clock).acquire('another')
    clock.now+=101
    with pytest.raises(S.StaleGeneration): lease.renew(gen)


def test_turn_persists_before_model_and_does_not_hold_write_lock(office,tmp_path):
    conn,clock,lease,gen=office
    def step(context,previous,remaining_s):
        row=conn.execute('SELECT payload_json FROM turn_runs WHERE id=?',(context.turn_id,)).fetchone()
        assert json.loads(row[0])['state']=='running'
        assert not conn.in_transaction
        assert context.actor_id=='agent' and context.generation==1
        return S.Step(done=True,value={'ok':True})
    result=make(office,step).run_turn('agent',gen=gen,deadline_s=2,max_steps=1)
    assert result.state=='completed' and result.steps==1


def test_late_replaced_agent_cannot_apply_action(office):
    conn,clock,lease,gen=office
    effects=[]
    def step(context,previous,remaining_s):
        payload=json.loads(conn.execute("SELECT payload_json FROM agents WHERE id='agent'").fetchone()[0])
        payload['generation']=2
        conn.execute("UPDATE agents SET payload_json=? WHERE id='agent'",(json.dumps(payload),));conn.commit()
        return S.Step(done=True,actions=[S.Action('paper_submit',{'mint':'m'})])
    result=make(office,step,handlers={'paper_submit':lambda context,key,request:effects.append(key)}).run_turn(
        'agent',gen=gen,deadline_s=2,max_steps=1)
    assert result.state=='refused' and result.reason=='actor_replaced' and effects==[]


def test_kill_after_admit_before_ack_one_position(office,tmp_path):
    ledger=sqlite3.connect(tmp_path/'ledger.db');ledger.execute('CREATE TABLE positions(order_id TEXT PRIMARY KEY,payload TEXT)');ledger.commit()
    calls=[]
    def admit(context,key,request):
        calls.append(key)
        ledger.execute('INSERT OR IGNORE INTO positions VALUES(?,?)',(key,json.dumps(request)));ledger.commit()
        return {'order_id':key}
    def lookup(context,key,request):
        row=ledger.execute('SELECT order_id FROM positions WHERE order_id=?',(key,)).fetchone()
        return {'order_id':row[0]} if row else None
    first=[True]
    def crash(boundary,key):
        if boundary=='effect_committed' and first.pop(): raise SystemExit('kill')
    step=lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{'mint':'m'})])
    supervisor=make(office,step,handlers={'paper_submit':admit},lookups={'paper_submit':lookup},fault=crash)
    with pytest.raises(SystemExit): supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='stable-turn')
    supervisor.fault=None
    assert supervisor.reconcile(gen=office[3])[0]['state']=='recovered'
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='stable-turn')
    assert result.state=='completed'
    assert ledger.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==1 and len(calls)==1
    ledger.close()


def test_action_key_collision_with_changed_payload_is_refused(office):
    supervisor=make(office,handlers={'paper_submit':lambda context,key,request:{'order_id':key}})
    context=supervisor.context('agent','decision',office[3],{'basis':'corrected_utc','receipt_cut':1000.,'knowledge_cut':1000.,
        'slot_cut':None,'clock_sample':{'corrected_utc':1000.,'offset_s':0}},deadline_at=1002.)
    supervisor.apply_action(context,S.Action('paper_submit',{'mint':'a'}))
    with pytest.raises(ValueError,match='action_identity'):
        supervisor.apply_action(context,S.Action('paper_submit',{'mint':'b'}))


def test_crash_during_ticket_dispatch_is_left_to_durable_outbox(office):
    # Supervisor commits only the local outbox request. Task9 owns transport replay/ACK.
    messages=[]
    def file_ticket(context,key,request):
        messages.append(key);return {'ticket_id':key,'state':'queued'}
    result=make(office,lambda *a:S.Step(done=True,actions=[S.Action('ticket',{'problem':'x'})]),
                handlers={'ticket':file_ticket}).run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='ticket-turn')
    assert result.state=='completed' and len(messages)==1
    assert make(office).run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='ticket-turn').state=='completed'
    assert len(messages)==1


def test_max_steps_is_enforced_by_supervisor(office):
    calls=[]
    result=make(office,lambda *a:calls.append(True) or S.Step()).run_turn('agent',gen=office[3],deadline_s=2,max_steps=3)
    assert result.state=='partial' and result.reason=='step_limit' and len(calls)==3


def test_unknown_clock_cut_does_not_call_model(office):
    supervisor=make(office,lambda *a:pytest.fail('unknown-clock model'))
    supervisor.freeze_cut=lambda:None
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.state=='refused' and result.reason=='clock_unknown'


def test_late_step_cannot_commit_effect(office):
    effects=[]
    def step(*args):
        office[1].now+=3
        return S.Step(done=True,actions=[S.Action('paper_submit',{})])
    result=make(office,step,handlers={'paper_submit':lambda *a:effects.append(True)}).run_turn(
        'agent',gen=office[3],deadline_s=1,max_steps=1)
    assert result.state=='partial' and result.reason=='deadline' and effects==[]


def test_heartbeat_alive_but_queue_frozen_is_stall(office):
    conn,clock,lease,gen=office
    supervisor=make(office)
    conn.execute('INSERT INTO action_requests VALUES(?,?)',('old',json.dumps({'state':'prepared','created_at':900})))
    conn.execute('INSERT INTO heartbeats VALUES(?,?)',('supervisor',json.dumps({'at':1000})));conn.commit()
    front=supervisor.progress_frontiers(stall_after_s=30)
    assert front['state']=='stalled' and front['oldest_pending_s']==100


@pytest.mark.parametrize('source_state,expected',[('observed','healthy_empty'),('off','source_off'),('partial','partial')])
def test_healthy_empty_off_and_partial_are_distinct(office,source_state,expected):
    supervisor=make(office,lambda *a:S.Step(done=True,source_state=source_state))
    supervisor.run_turn('agent',gen=office[3],deadline_s=1,max_steps=1)
    assert supervisor.progress_frontiers()['state']==expected


def test_child_tree_cancelled_on_deadline(tmp_path):
    marker=tmp_path/'child.pid'
    code='import os,time,pathlib; p=os.fork(); pathlib.Path('+repr(str(marker))+').write_text(str(p)) if p else None; time.sleep(60)'
    result=S.run_child([sys.executable,'-c',code],timeout_s=.3,cwd=tmp_path,env={'PATH':os.defpath},max_output_bytes=1024)
    assert result['outcome']=='timeout'
    pid=int(marker.read_text())
    until=time.monotonic()+1
    while time.monotonic()<until:
        status=Path('/proc')/str(pid)/'stat'
        try: state=status.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError): break
        if state=='Z': break
        time.sleep(.01)
    else: pytest.fail('grandchild survived cancellation')


def test_child_output_is_bounded(tmp_path):
    result=S.run_child([sys.executable,'-c','import os; os.write(1,b"x"*1000000)'],timeout_s=1,
                       cwd=tmp_path,env={'PATH':os.defpath},max_output_bytes=1024)
    assert result['outcome']=='output_limit' and len(result['stdout'])<=1024


def test_child_bounded_input_does_not_block_on_a_nonreader(tmp_path):
    result=S.run_child([sys.executable,'-c','import time; time.sleep(60)'],timeout_s=.2,
                       cwd=tmp_path,env=None,input_bytes=b'x'*1_000_000)
    assert result['outcome']=='timeout'


def test_child_reads_input_and_rejects_oversized_input_before_spawn(tmp_path):
    result=S.run_child([sys.executable,'-c','import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                       timeout_s=1,cwd=tmp_path,env=None,input_bytes=b'hello\n')
    assert result['outcome']=='success' and result['stdout']==b'hello\n'
    with pytest.raises(ValueError,match='child_input'):
        S.run_child(['never-run'],timeout_s=1,cwd=tmp_path,env=None,input_bytes=b'x'*1_000_001)


def test_unlabelled_clock_basis_refused_before_model(office):
    supervisor=make(office,lambda *a:pytest.fail('unlabelled clock dispatched'))
    freeze=supervisor.freeze_cut
    def missing_basis():
        cut=freeze();cut.pop('basis');return cut
    supervisor.freeze_cut=missing_basis
    assert supervisor.run_turn('agent',gen=office[3],deadline_s=1,max_steps=1).reason=='clock_unknown'


def test_crash_after_prepared_intent_retries_only_with_successful_absence_lookup(office):
    effects={}
    def handler(context,key,request):
        effects.setdefault(key,{'order_id':key});return effects[key]
    def crash(boundary,key):
        if boundary=='intent_committed': raise SystemExit('kill before effect')
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{'mint':'m'})]),
                    handlers={'paper_submit':handler},lookups={'paper_submit':lambda context,key,request:effects.get(key)},fault=crash)
    with pytest.raises(SystemExit):
        supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='prepared')
    assert effects=={}
    supervisor.fault=None
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='prepared')
    assert result.state=='completed' and len(effects)==1


def test_entrypoint_once_writes_progress_and_releases_lease(office,capsys):
    from scripts.office_supervisor import main
    office[1].now+=101
    supervisor=make(office)
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,
             'tasks':lambda:[{'agent_id':'agent','turn_id':'entrypoint-turn'}]}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0
    output=json.loads(capsys.readouterr().out)
    assert output['state']=='completed'
    assert office[0].execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone()
    start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    assert office[0].execute('SELECT holder FROM supervisor_lease').fetchone()[0]==f'office-{os.getpid()}-{start}'
    assert S.Lease(office[0],clock=office[1]).acquire('restart')>office[3]


def test_entrypoint_initializes_under_lease_then_pauses_on_unready_host(office,capsys):
    from scripts.office_supervisor import main
    office[1].now+=101
    supervisor=make(office);seen=[]
    def tasks():raise ValueError('caps_unapplied')
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,'tasks':tasks,
             'initialize':lambda gen:seen.append(gen)}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0
    assert len(seen)==1 and seen[0]>office[3]
    assert json.loads(capsys.readouterr().out.splitlines()[0])=={'state':'paused','reason':'caps_unapplied'}
    assert office[0].execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone() is None


def test_clock_loss_pauses_without_raising(office):
    from scripts.office_supervisor import main
    office[1].now+=101
    supervisor=make(office)
    def tasks():
        office[1].now=None
        raise ValueError('clock_unknown')
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kwargs:runtime)==0


def test_outer_read_snapshot_cannot_fence_a_new_owner(office,tmp_path):
    conn,clock,lease,gen=office
    other=store.open_store(tmp_path/'office.db')
    conn.execute('BEGIN')
    conn.execute('SELECT * FROM supervisor_lease').fetchone()
    other.execute("UPDATE supervisor_lease SET generation=generation+1,holder='new-owner'")
    other.commit()
    effects=[]
    try:
        with pytest.raises((S.StaleGeneration,sqlite3.OperationalError)):
            with lease.guard(gen): effects.append('would mutate ledger')
        assert effects==[]
    finally:
        conn.rollback();other.close()


def test_missing_authorizer_refuses_before_preparing_any_effect(office):
    effects=[]
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{'book_id':'office'})]),
                    handlers={'paper_submit':lambda *a:effects.append(a)},authorize=None)
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.state=='refused' and result.reason=='action_forbidden'
    assert effects==[] and office[0].execute('SELECT COUNT(*) FROM action_requests').fetchone()[0]==0


@pytest.mark.parametrize('case',['scout','foreign_book'])
def test_untrusted_actions_cannot_bypass_actor_or_book_authority(office,case):
    if case=='scout':
        actor=json.loads(office[0].execute("SELECT payload_json FROM agents WHERE id='agent'").fetchone()[0])
        actor['role']='Scout'
        office[0].execute("UPDATE agents SET payload_json=? WHERE id='agent'",(json.dumps(actor),));office[0].commit()
    effects=[]
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',
        {'book_id':'foreign' if case=='foreign_book' else 'office'})]),handlers={'paper_submit':lambda *a:effects.append(a)})
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.state=='refused' and effects==[]


def test_second_action_does_not_start_after_first_consumes_deadline(office):
    effects=[]
    def first(*args):
        effects.append('first');office[1].now+=3;return {'done':True}
    step=lambda *a:S.Step(done=True,actions=[S.Action('first',{}),S.Action('second',{})])
    result=make(office,step,handlers={'first':first,'second':lambda *a:effects.append('second')}).run_turn(
        'agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.state=='partial' and result.reason=='deadline' and effects==['first']


def test_deadline_is_rechecked_after_lookup_before_effect(office):
    effects=[]
    def lookup(*args): office[1].now+=3;return None
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{})]),
        handlers={'paper_submit':lambda *a:effects.append(a)},lookups={'paper_submit':lookup})
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.reason=='deadline' and effects==[]


def test_authorizer_cannot_mutate_the_bound_request(office):
    effects=[]
    def authorize(context,action): action.payload['book_id']='foreign';return True
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{'book_id':'office'})]),
                    handlers={'paper_submit':lambda *a:effects.append(a)},authorize=authorize)
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1)
    assert result.state=='refused' and effects==[]


def test_frontiers_can_be_read_without_creating_schema(office,tmp_path):
    make(office)
    conn=sqlite3.connect((tmp_path/'office.db').as_uri()+'?mode=ro',uri=True)
    statements=[];conn.set_trace_callback(statements.append)
    try:
        assert S.progress_frontiers(conn,now=1000.)['state']=='healthy_empty'
        assert statements and all(s.lstrip().upper().startswith('SELECT') for s in statements)
    finally:conn.close()


def test_book_binding_comes_from_trusted_agent_resolver(office):
    actor=json.loads(office[0].execute("SELECT payload_json FROM agents WHERE id='agent'").fetchone()[0])
    actor['pod']='pod-A'
    office[0].execute("UPDATE agents SET payload_json=? WHERE id='agent'",(json.dumps(actor),));office[0].commit()
    effects=[]
    def submit(context,key,request):effects.append(context.book_id);return {'order_id':key}
    supervisor=make(office,lambda *a:S.Step(done=True,actions=[S.Action('paper_submit',{'book_id':'pod-A'})]),
        book_id=lambda record:record['pod'],handlers={'paper_submit':submit})
    assert supervisor.run_turn('agent',gen=office[3],deadline_s=2,max_steps=1).state=='completed'
    assert effects==['pod-A']


def test_broker_generation_callback_expires_at_turn_deadline(office):
    supervisor=make(office)
    context=supervisor.context('agent','decision',office[3],supervisor.freeze_cut(),deadline_at=1002.)
    assert supervisor.valid_context(context)
    office[1].now=1003.
    assert supervisor.valid_context(context) is False


@pytest.mark.parametrize('expired', ['utc', 'monotonic'])
def test_authorizer_cannot_start_effect_after_deadline(office, monkeypatch, expired):
    effects=[]
    ticks=[0.]
    from types import SimpleNamespace
    monkeypatch.setattr(S, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    def authorize(context, action):
        if expired=='utc': office[1].now+=.8
        else: ticks[0]+=.8
        return True
    supervisor=make(office, lambda *a:None, authorize=authorize,
                    handlers={'paper_submit':lambda *a:effects.append(a)})
    context=supervisor.context('agent','decision',office[3],supervisor.freeze_cut(),deadline_at=1002.)
    with pytest.raises(S.TurnDeadline):
        supervisor.apply_action(context,S.Action('paper_submit',{}),monotonic_deadline=2.)
    assert effects==[]


def test_entrypoint_flushes_after_turn_and_exposes_health_before_close(office):
    from scripts.office_supervisor import main
    office[1].now+=101
    events=[]
    supervisor=make(office,lambda *a:(events.append('model') or S.Step(done=True)))
    runtime={'supervisor':supervisor,'deadline_s':1,'max_steps':1,
             'tasks':lambda:[{'agent_id':'agent','turn_id':'observed-turn'}],
             'flush_observations':lambda:events.append('flush'),
             'observation_health':lambda:{'state':'observed_only','pending_n':0},
             'close':lambda:events.append('close')}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert events==['model','flush','flush','close']
    heartbeat=json.loads(office[0].execute("SELECT payload_json FROM heartbeats WHERE id='supervisor'").fetchone()[0])
    assert heartbeat['observations']['pending_n']==0


def test_written_work_flags_are_stored_with_the_turn(office):
    fields=dict(dm_context=True,dm_count=1,dm_unread_backlog=2,task_context=True,ref_context=False,refs_count=0)
    result=make(office,lambda *a:S.Step(done=True,value=fields)).run_turn('agent',gen=office[3],deadline_s=2,max_steps=1,turn_id='dm-turn')
    record=json.loads(office[0].execute("SELECT payload_json FROM turn_runs WHERE id='dm-turn'").fetchone()[0])
    assert result.state=='completed' and all(record[k]==v for k,v in fields.items())


def test_pre_dispatch_guard_retry_recovers_with_original_cut_and_deadline(office):
    conn,clock,lease,gen=office;seen=[]
    def step(ctx,previous,remaining):
        seen.append((ctx.cut,ctx.deadline_at))
        if len(seen)==1:
            return S.Step(done=True,source_state='off',value={'reason':'desk_judge_work_unknown',
                'retryable_pre_dispatch':True,'guard_error':{'exception_class':'OperationalError','message':'interrupted'}})
        return S.Step(done=True,value={'ok':True})
    supervisor=make(office,step)
    first=supervisor.run_turn('agent',gen=gen,deadline_s=30,max_steps=2,turn_id='guard-retry')
    assert first.state=='deferred' and first.steps==0
    assert supervisor.run_turn('agent',gen=gen,deadline_s=30,max_steps=2,turn_id='guard-retry').state=='deferred'
    assert len(seen)==1
    clock.now+=5
    second=supervisor.run_turn('agent',gen=gen,deadline_s=30,max_steps=2,turn_id='guard-retry')
    assert second.state=='completed' and second.steps==1 and second.value=={'ok':True}
    assert seen[0]==seen[1]


@pytest.mark.parametrize('prior_step',[True,False])
def test_guard_failure_after_progress_or_without_host_marker_is_not_retried(office,prior_step):
    calls=[]
    def step(ctx,previous,remaining):
        calls.append(1)
        if prior_step and len(calls)==1:return S.Step(done=False,value={'progress':True})
        return S.Step(done=True,source_state='off',value={'reason':'desk_judge_work_unknown',
            'retryable_pre_dispatch':prior_step})
    supervisor=make(office,step)
    result=supervisor.run_turn('agent',gen=office[3],deadline_s=30,max_steps=4,turn_id='not-retryable')
    assert result.state=='completed'
    record=json.loads(office[0].execute('SELECT payload_json FROM turn_runs WHERE id=?',('not-retryable',)).fetchone()[0])
    assert 'guard_attempts' not in record


def test_guard_retry_does_not_extend_near_deadline(office):
    def step(*a):return S.Step(done=True,source_state='off',value={
        'reason':'desk_judge_work_unknown','retryable_pre_dispatch':True})
    result=make(office,step).run_turn('agent',gen=office[3],deadline_s=4,max_steps=4,turn_id='guard-budget')
    assert result.state=='refused' and result.reason=='desk_judge_work_unknown'


def test_final_message_batch_partial_failure_preserves_prefix_and_reentry(office):
    effects=[];failed=[False]
    def send(ctx,key,args):
        if args['to_actor_id']=='b' and not failed[0]:
            failed[0]=True;raise RuntimeError('injected before second effect')
        effects.append(args['to_actor_id']);return {'ok':True}
    def lookup(ctx,key,args):return None
    actions=[S.Action('msg.send',{'to_actor_id':t,'text':'x'}) for t in ('a','b','c')]+[S.Action('memo.commit',{})]
    sup=make(office,lambda *a:S.Step(done=True,actions=actions,value={'request_admission':{'state':'known','requested_n':3,'refused_n':0,'truncated':False,'truncated_n':0}}),authorize=lambda *a:True,
        handlers={'msg.send':send,'memo.commit':lambda *a:effects.append('memo') or {}},
        lookups={'msg.send':lookup})
    first=sup.run_turn('agent',gen=office[3],deadline_s=30,max_steps=2,turn_id='batch')
    assert first.state=='failed' and effects==['a']
    assert first.value['request_admission']['requested_n']==3  # admission is recorded even when later delivery fails
    second=sup.run_turn('agent',gen=office[3],deadline_s=30,max_steps=2,turn_id='batch')
    assert second.state=='completed' and effects==['a','b','c','memo']


@pytest.mark.parametrize('state',['prepared','completed'])
def test_legacy_message_record_is_reused_and_payload_change_refused(office,state):
    effects=[];known={}
    sup=make(office,authorize=lambda *a:True,handlers={'msg.send':lambda *a:effects.append(1) or {'ok':True}},
        lookups={'msg.send':lambda ctx,key,args:known.get(key)})
    ctx=sup.context('agent','legacy',office[3],sup.freeze_cut(),deadline_at=1030.)
    action=S.Action('msg.send',{'to_actor_id':'a','text':'x'})
    sup.apply_action(ctx,action)
    conn=office[0];legacy=S.action_key(ctx.decision_id,'msg.send')
    raw=json.loads(conn.execute('SELECT payload_json FROM action_requests').fetchone()[0]);raw['state']=state
    conn.execute('UPDATE action_requests SET id=?,payload_json=?',(legacy,json.dumps(raw)));conn.commit()
    known[legacy]={'ok':True}
    assert sup.apply_action(ctx,action)=={'ok':True} and effects==[1]
    with pytest.raises(ValueError,match='action_identity'):
        sup.apply_action(ctx,S.Action('msg.send',{'to_actor_id':'a','text':'changed'}))


def test_malformed_running_rows_cannot_starve_expired_recovery(office):
    conn,clock,lease,gen=office
    supervisor=make(office)
    for i in range(3):
        S._put(conn,'turn_runs','bad-%d'%i,dict(state='running',created_at=100.+i,deadline_at=None,steps=0))
    S._put(conn,'turn_runs','expired',dict(state='running',created_at=900.,deadline_at=950.,steps=0))
    assert supervisor.reconcile_turns(gen=gen,limit=2)==['bad-0','bad-1']
    assert supervisor.reconcile_turns(gen=gen,limit=2)==['bad-2','expired']
    assert S._get(conn,'turn_runs','expired')['state']=='partial'
    assert all(S._get(conn,'turn_runs','bad-%d'%i)['reason']=='deadline_unknown' for i in range(3))


@pytest.mark.parametrize('deadline',[None,True,False,'100',{},[],float('inf'),float('-inf')])
def test_unknown_running_deadline_becomes_partial_without_touching_intents(office,deadline):
    conn,clock,lease,gen=office
    original=dict(state='running',created_at=800.,deadline_at=deadline,steps=1,
                  cut={'receipt_cut':800.},pending_step={'actions':[{'kind':'paper_submit'}]})
    # SQLite JSON accepts numeric overflow even though canonical Office writes reject it.
    raw=json.dumps(original).replace('Infinity','1e999')
    conn.execute('INSERT INTO turn_runs VALUES(?,?)',('unknown',raw));conn.commit()
    S._put(conn,'action_requests','unknown-action',dict(state='prepared',created_at=800.))
    supervisor=make(office)
    assert supervisor.reconcile_turns(gen=gen)==['unknown']
    recovered=S._get(conn,'turn_runs','unknown')
    assert (recovered['state'],recovered['reason'])==('partial','deadline_unknown')
    assert recovered['cut']==original['cut'] and recovered['pending_step']==original['pending_step']
    assert recovered['steps']==original['steps']
    if type(deadline) is float:
        assert recovered['deadline_at'] is None and recovered['deadline_unknown_kind']=='nonfinite'
    else: assert recovered['deadline_at']==deadline
    assert S._get(conn,'action_requests','unknown-action')['state']=='prepared'
    assert supervisor.progress_frontiers()['state']=='stalled'


def test_missing_deadline_recovery_is_bounded_and_releases_frontier(office):
    conn,clock,lease,gen=office
    supervisor=make(office)
    for i in range(3):
        S._put(conn,'turn_runs',str(i),dict(state='running',created_at=800.+i,steps=0))
    supervisor.run_turn('agent',gen=gen,deadline_s=2,max_steps=1,turn_id='fresh')
    assert supervisor.progress_frontiers()['decisions_through'] is None
    assert len(supervisor.reconcile_turns(gen=gen,limit=2))==2
    assert S._get(conn,'turn_runs','2')['state']=='running'
    supervisor.reconcile_turns(gen=gen,limit=2)
    assert supervisor.progress_frontiers()['decisions_through']==clock.now
    assert supervisor.progress_frontiers()['state']=='healthy_empty'


def test_entrypoint_clock_loss_during_expiry_recovery_pauses_without_writes(office,capsys):
    from scripts.office_supervisor import main
    conn,clock,lease,gen=office
    clock.now+=101
    S._put(conn,'turn_runs','unknown',dict(state='running',created_at=800.,steps=0))
    def tasks():
        # Lose clock after startup recovery/readiness, before idle reconciliation.
        S._put(conn,'turn_runs','unknown',dict(state='running',created_at=800.,steps=0))
        clock.now=None
        return []
    runtime={'supervisor':make(office),'deadline_s':1,'max_steps':1,'tasks':tasks}
    assert main(['--once'],runtime_factory=lambda **kw:runtime)==0
    assert json.loads(capsys.readouterr().out)=={'state':'paused','reason':'clock_unknown'}
    assert S._get(conn,'turn_runs','unknown')['state']=='running'
    assert conn.execute("SELECT 1 FROM heartbeats WHERE id='supervisor'").fetchone() is None


def test_clock_loss_after_expiry_write_rolls_back_the_batch(office,monkeypatch):
    conn,clock,lease,gen=office
    original=dict(state='running',created_at=800.,steps=0)
    S._put(conn,'turn_runs','unknown',original)
    put=S._put
    def lose_clock(*args):
        put(*args)
        if args[1:3]==('turn_runs','unknown'):clock.now=None
    monkeypatch.setattr(S,'_put',lose_clock)
    with pytest.raises(ValueError,match='clock_unknown'):
        make(office).reconcile_turns(gen=gen)
    assert S._get(conn,'turn_runs','unknown')==original
