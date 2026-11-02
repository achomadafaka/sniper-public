"""Factory fail-closed boundary tests; no real runtime seed or provider calls."""
from pathlib import Path
import pytest
from sniper.office import runtime as R


def test_missing_config_refuses_without_creating_state(tmp_path):
    state=tmp_path/'state'
    with pytest.raises(R.RuntimeRefused):
        R.build(store_path=state/'office.db',ledger_path=state/'ledger.db',
                config_path=tmp_path/'missing.json',guard_path=tmp_path/'guard.json')
    assert not state.exists()


def test_unpinned_config_refuses_before_any_db_write(tmp_path):
    config=tmp_path/'runtime.json';config.write_text('{"version":"office-runtime-pilot-v1"}')
    state=tmp_path/'state'
    with pytest.raises(R.RuntimeRefused):
        R.build(store_path=state/'office.db',ledger_path=state/'ledger.db',
                config_path=config,guard_path=tmp_path/'guard.json')
    assert not state.exists()


@pytest.fixture
def configured(tmp_path,monkeypatch):
    import hashlib,json,os,sqlite3,time
    monkeypatch.setattr(R.claude_compat,'startup_refusal',lambda:None)
    from sniper.persistence import db_memecoin_desk as desk
    root=Path(__file__).resolve().parents[2];configs=root/'config/office'
    state=tmp_path/'state';control=state/'data/office-control';guard=control/'guard/guard.json';guard.parent.mkdir(parents=True)
    guard.write_text(json.dumps(dict(state='ready',reason=None,at=time.time(),guard_policy_version='office-host-guard-pilot-v3')))
    clock=state/'data/clock_offset.json';clock.write_text(json.dumps(dict(at=time.time(),offset_s=0.)))
    cg=tmp_path/'cgroup/user.slice/office.slice';cg.mkdir(parents=True)
    for name,value in {'cpu.max':'200000 100000','memory.max':'4294967296','pids.max':'256'}.items():(cg/name).write_text(value)
    monkeypatch.setattr(R,'CGROUP_ROOT',tmp_path/'cgroup')
    proc=tmp_path/'proc'/str(os.getpid());proc.mkdir(parents=True)
    script=str(root/'scripts/office_supervisor.py')
    (proc/'cgroup').write_text('0::/user.slice/office.slice/office-supervisor.service\n')
    (proc/'cmdline').write_bytes(('python3\0'+script+'\0').encode())
    (proc/'stat').write_text(str(os.getpid())+' (fixture) '+' '.join(['0']*19+['777']))
    monkeypatch.setattr(R,'PROC_ROOT',proc.parent)
    class Flags:f_flag=os.ST_RDONLY
    monkeypatch.setattr(R.os,'statvfs',lambda _:Flags())
    office=state/'data/office'
    transport=tmp_path/'transport.json'
    transport.write_text(json.dumps(dict(socket_path=str(control/'launcher-sock/launcher.sock'),
                                        request_root=str(office/'launcher/requests'),crew_uid=os.getuid())))
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    approvals=json.loads((configs/'prompts/pilot/sha256.json').read_bytes())
    seed=dict(schema='office-pilot-seed-launcher-v1',files=approvals['source_pins'],
              prompts={slot:dict(text=(configs/'prompts/pilot'/(slot+'.md')).read_text(),sha256=approvals['files'][slot+'.md'])
                       for slot in ('head','scout','judge_claude','judge_gpt')})
    desk_path=state/'data/memecoin_desk.db';db=desk.connect(desk_path);db.close()
    config=dict(claude_sdk=R.claude_compat.runtime_pins(root),version=R.VERSION,enabled=True,discovery_only=True,evaluated_epoch_allowed=False,research_enabled=False,
        content_addressed_release_root=str(root),state_root=str(state),config_root=str(configs),
        store_path=str(office/'office.db'),ledger_path=str(office/'ledger/ledger.db'),clock_file=str(clock),guard_file=str(guard),
        office_cgroup=str(cg),supervisor_identity=dict(crew_uid=os.getuid(),expected_cgroup='/user.slice/office.slice/office-supervisor.service',supervisor_script=script),
        transport=dict(path=str(transport),sha256=sha(transport)),cadence=dict(path=str(configs/'cadence.json'),sha256=sha(configs/'cadence.json')),
        model_manifest=dict(path=str(configs/'pilot_models.json'),sha256=sha(configs/'pilot_models.json')),pilot_seed_launcher_config=seed,
        provider_caps={'claude':1,'codex':1},host_children_max=2,turn_deadline_s=30,max_steps=3,lease_ttl_s=90,poll_s=1,desk_db=str(desk_path))
    config_path=tmp_path/'runtime.json'
    def write():
        config_path.write_text(json.dumps(config))
        config_path.with_suffix('.sha256').write_text(sha(config_path))
    write()
    args=dict(store_path=Path(config['store_path']),ledger_path=Path(config['ledger_path']),config_path=config_path,guard_path=guard)
    return config,args,write


def test_actual_seed_lease_book_and_shared_scheduler_are_composed(configured):
    import json
    config,args,write=configured
    runtime=R.build(**args)
    try:
        with pytest.raises(R.RuntimeRefused,match='not_initialized'):runtime['tasks']()
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture')
        runtime['initialize'](gen)
        assert len(runtime['tasks']())==4
        assert runtime['live_backlogged']() is True and runtime['desk_judge_backlogged']() is False
        assert runtime['scheduler'].caps=={'claude':1,'codex':1}
        assert runtime['transports']['claude'].client is runtime['transports']['codex'].client
        assert supervisor.conn.execute('SELECT count(*) FROM agents').fetchone()[0]==4
        assert supervisor.conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0]==0
        runtime['initialize'](gen)
        assert supervisor.conn.execute('SELECT count(*) FROM org_changes').fetchone()[0]==1
        supervisor.lease.release(gen)
    finally:runtime['close']()


@pytest.mark.parametrize('bad',['guard','clock','identity','pin','budget'])
def test_unready_bindings_refuse_before_store_creation(configured,bad):
    import json
    config,args,write=configured
    if bad=='guard':
        args['guard_path'].write_text(json.dumps(dict(state='paused',reason='fixture',at=0,guard_policy_version='office-host-guard-pilot-v3')))
    elif bad=='clock':Path(config['clock_file']).write_text('{}')
    elif bad=='identity':config['supervisor_identity']['crew_uid']+=1;write()
    elif bad=='pin':config['model_manifest']['sha256']='0'*64;write()
    else:config['lease_ttl_s']=30;write()
    with pytest.raises(R.RuntimeRefused):R.build(**args)
    assert not args['store_path'].exists() and not args['ledger_path'].exists()


def test_runtime_wires_off_turn_observations_and_committed_memo_receipts(configured):
    from sniper.office.supervisor import Action
    config,args,write=configured
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture')
        runtime['initialize'](gen)
        task=runtime['tasks']()[0]
        context=supervisor.context(task['agent_id'],task['turn_id'],gen,supervisor.freeze_cut(),deadline_at=supervisor.clock()+30)
        action=Action('memo.commit',{'task':'Review','memo':'No proof','served_model':'fixture'})
        assert supervisor.authorize(context,action)
        result=supervisor.handlers['memo.commit'](context,'memo-fixture',action.payload)
        assert len(result['receipt_id'])==64
        assert supervisor.conn.execute('SELECT count(*) FROM receipts WHERE row_hash=?',(result['receipt_id'],)).fetchone()[0]==1
        assert runtime['observation_health']()['pending_n']==0
        assert runtime['flush_observations']()['last_error'] is None
        supervisor.lease.release(gen)
    finally:runtime['close']()


@pytest.mark.parametrize('provider',['claude','codex'])
def test_real_runtime_turn_buffers_call_then_flushes_metadata_only(configured,monkeypatch,provider):
    import json
    config,args,write=configured
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture')
        runtime['initialize'](gen)
        client=runtime['transports'][provider].client
        def call(adapter,doc,**kwargs):
            assert supervisor.conn.execute('SELECT count(*) FROM model_calls').fetchone()[0]==0
            return {'text':json.dumps({'done':True,'task':'review','memo':'PRIVATE MEMO CONTENT','requests':[],'refs':None}),
                    'model_echo':doc['model'] if provider=='claude' else None,
                    'served_model_basis':'unknown' if provider=='claude' else 'requested_pinned','usage':{'input_tokens':2,'output_tokens':0,'cache_read_input_tokens':0}}
        monkeypatch.setattr(client,'call',call)
        task=next(t for t in runtime['tasks']() if json.loads(supervisor.conn.execute('SELECT payload_json FROM agents WHERE id=?',(t['agent_id'],)).fetchone()[0])['provider']==provider)
        result=supervisor.run_turn(task['agent_id'],gen=gen,deadline_s=30,max_steps=2,turn_id=task['turn_id'])
        assert result.state=='completed'
        assert supervisor.conn.execute('SELECT count(*) FROM model_calls').fetchone()[0]==0
        assert runtime['observation_health']()['pending_n']==1
        assert runtime['flush_observations']()['last_error'] is None
        row=dict(supervisor.conn.execute('SELECT * FROM model_calls').fetchone())
        assert row['turn_id']==task['turn_id'] and row['outcome']=='success'
        assert row['served_model_basis']==('echoed' if provider=='claude' else 'requested_pinned')
        assert row['served_model'] is not None
        assert row['input_tokens']==2 and row['output_tokens']==0 and row['cost_usd'] is None
        assert 'PRIVATE' not in json.dumps(row)
        assert supervisor.conn.execute('SELECT count(*) FROM handoff_events').fetchone()[0]==0
        supervisor.lease.release(gen)
    finally:runtime['close']()


def test_work_handlers_are_registered_but_refuse_before_discovery2(configured):
    from sniper.office.supervisor import Action
    config,args,write=configured
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture');runtime['initialize'](gen)
        task=runtime['tasks']()[0]
        context=supervisor.context(task['agent_id'],task['turn_id'],gen,supervisor.freeze_cut(),deadline_at=supervisor.clock()+30)
        assert {'msg.send','task.set','task.update'} <= set(supervisor.handlers)
        assert supervisor.authorize(context,Action('task.set',{'title':'x','note':''})) is False
        supervisor.lease.release(gen)
    finally:runtime['close']()
