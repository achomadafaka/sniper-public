"""Crew transition contract on synthetic Office DBs; never runs the operator CLI live."""
import copy
import hashlib
import json
import pytest
from sniper.office import store as S,ledger as L,trader,pilot_seed
from sniper.office.supervisor import Lease
from sniper.office import epoch_transition as E
from .test_pilot_seed import env
from .test_runtime import configured

@pytest.fixture
def prepared(env,tmp_path):
    conn,args,launch=env
    pilot_seed.seed_pilot_org(conn,**args)
    old=json.loads(conn.execute('SELECT payload_json FROM org_changes').fetchone()[0])['seed_manifest']
    manifest=copy.deepcopy(old)
    for seat in manifest['seats']:seat['prompt_hash']=hashlib.sha256(('new-'+seat['slot']).encode()).hexdigest()
    ledger=L.open_ledger(tmp_path/'ledger.db')
    trader.bootstrap_pilot(ledger,epoch_id='office-pilot-discovery-1',book_id='pilot-floor',clip_usd=25.,rule_version='fixture',capital_usd=1000.,actor='crew',now=1000.)
    Lease(conn,clock=lambda:1000.,ttl_s=20).acquire('stopped-fixture')
    record=approval_record(conn,ledger,manifest)
    agents={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents')}
    yield conn,ledger,manifest,record,agents
    ledger.close()


def approval_record(conn,ledger,manifest,guard_hash='c'*64):
    agents={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents')}
    newprompts={aid:next(p['prompt_hash'] for p in manifest['seats'] if p['role']==a['role'] and p['family']==a['family']) for aid,a in agents.items()}
    record=dict(epoch={'id':E.NEW,'mode':'discovery'},predecessor={'id':E.OLD,'sealed_at':None,'last_seq':{},'last_row_at':None},
        prompt_hashes={'old':{aid:a['prompt_hash'] for aid,a in agents.items()},'new':newprompts},tool_contract_hash={'old':'a'*64,'new':E.tool_hash()},
        release={'sha':'b'*40,'process_start_at':None,'process_start_source':'ps'},
        roster={'agents':{aid:{**{k:a[k] for k in ('model','provider','family')},'generation':a['generation']+1} for aid,a in agents.items()},'unchanged_from_predecessor':True},
        host_guard={'file':'config/office/host_guard_pilot.json','sha256':guard_hash,'policy':'office-host-guard-pilot-v3'},
        evidence={'discovery_only':True,'evaluated':False,'never_pooled_with':[E.OLD],'counts_as_evidence':False},
        blind_judge_rule={'dm_to_or_from_judge_before_verdict':'flagged','flag_field':'dm_context'},
        decision_fields=['dm_context','dm_count','dm_unread_backlog','ref_context','refs_count'],
        counters={'injection_as_instruction':None,'refused_sends_by_reason':{},'refused_refs_by_reason':{}},
        carryover={'paper_book_preserved':True,'capital_snapshot_sha256':E.capital_hash(ledger)},
        approvals={'alpha':'authenticated-fixture-only','gamma':'a7bbb9bc2-era review 2026-10-29: prompt delta + refs line approved with 5 conditions'})
    return record


def run(prepared,**kw):
    conn,ledger,manifest,record,_=prepared
    return E.transition(conn,ledger,manifest=manifest,record=record,now=1100.,guard_hash='c'*64,**kw)


def test_dry_run_is_nonmutating_and_apply_preserves_book_capital_and_actor_ids(prepared):
    conn,ledger,manifest,record,agents=prepared
    before=conn.total_changes;capital=E.capital_hash(ledger)
    preview=run(prepared)
    assert preview['state']=='dry_run' and conn.total_changes==before
    result=run(prepared,apply=True)
    assert result['state']=='transitioned' and E.capital_hash(ledger)==capital
    assert run(prepared,apply=True)['state']=='already_applied'
    epoch=json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(E.NEW,)).fetchone()[0])
    assert epoch['record']['predecessor']['last_seq']
    for aid,raw in conn.execute('SELECT id,payload_json FROM agents'):
        actor=json.loads(raw)
        assert actor['generation']==agents[aid]['generation']+1
        assert actor['prompt_hash']==record['prompt_hashes']['new'][aid]
        assert actor['model']==agents[aid]['model']
    assert S.verify_chain(conn,'org_changes')[0]


@pytest.mark.parametrize('bad',['lease','prompt','tool','capital','guard','missing'])
def test_bad_binding_refuses_without_partial_seal(prepared,bad):
    conn,ledger,manifest,record,_=prepared
    if bad=='lease':conn.execute('UPDATE supervisor_lease SET expires_at=2000');conn.commit()
    elif bad=='prompt':record['prompt_hashes']['new'][next(iter(record['prompt_hashes']['new']))]='0'*64
    elif bad=='tool':record['tool_contract_hash']['new']='0'*64
    elif bad=='capital':record['carryover']['capital_snapshot_sha256']='0'*64
    elif bad=='guard':record['host_guard']['sha256']='0'*64
    else:record.pop('decision_fields')
    before=conn.total_changes
    with pytest.raises(ValueError):run(prepared,apply=True)
    assert conn.execute('SELECT count(*) FROM epochs').fetchone()[0]==1
    assert json.loads(conn.execute('SELECT payload_json FROM epochs').fetchone()[0])['closed_at'] is None
    assert conn.execute('SELECT count(*) FROM org_changes').fetchone()[0]==1


def test_empty_epoch_rollback_restores_predecessor_and_is_idempotent(prepared):
    conn,ledger,manifest,record,agents=prepared
    run(prepared,apply=True)
    assert run(prepared,apply=True,rollback=True)['state']=='rolled_back'
    assert run(prepared,apply=True,rollback=True)['state']=='already_rolled_back'
    assert {aid:json.loads(raw) for aid,raw in conn.execute('SELECT id,payload_json FROM agents')}==agents
    assert json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(E.OLD,)).fetchone()[0])['closed_at'] is None
    assert S.verify_chain(conn,'org_changes')[0]


def test_rollback_refuses_after_any_new_turn_write(prepared):
    conn,*_=prepared
    run(prepared,apply=True)
    conn.execute("INSERT INTO turn_runs VALUES('new-work','{}')");conn.commit()
    with pytest.raises(ValueError,match='written'):run(prepared,apply=True,rollback=True)


def test_failure_in_rebind_rolls_back_seal_and_roster(prepared,monkeypatch):
    conn,ledger,manifest,record,agents=prepared
    conn.execute("CREATE TRIGGER fixture_fail BEFORE UPDATE ON agents BEGIN SELECT RAISE(ABORT,'fixture failure'); END");conn.commit()
    with pytest.raises(Exception,match='fixture failure'):run(prepared,apply=True)
    assert conn.execute('SELECT count(*) FROM epochs').fetchone()[0]==1
    assert {aid:json.loads(raw) for aid,raw in conn.execute('SELECT id,payload_json FROM agents')}==agents
    assert json.loads(conn.execute('SELECT payload_json FROM epochs').fetchone()[0])['closed_at'] is None


def test_actual_runtime_adopts_epoch2_and_delivers_written_work(configured,monkeypatch):
    import time
    from pathlib import Path
    from sniper.office import runtime as R,broker as B
    config,args,write=configured
    called_near=[]
    monkeypatch.setattr(R,'near_pass_tools',lambda *a,**k:{'desk.near_pass_list':B.Tool(lambda a,c:called_near.append(a) or B.ToolResult({'feed':'near_pass','items':[]},{},None,[],'known'),{},'fixture-near-pass')})
    first=R.build(**args)
    supervisor=first['supervisor'];gen=supervisor.lease.acquire('fixture-one');first['initialize'](gen)
    supervisor.lease.release(gen);first['close']()
    root=Path(config['config_root'])/'prompts/discovery2'
    approvals=json.loads((root/'sha256.json').read_text())
    config['pilot_seed_launcher_config']['prompts']={slot:{'text':(root/(slot+'.md')).read_text(),'sha256':approvals['files'][slot+'.md']} for slot in pilot_seed.SLOTS}
    write()
    conn=S.open_store(args['store_path']);ledger=L.open_ledger(args['ledger_path'])
    manifest,_=pilot_seed.resolve_manifest(config_root=config['config_root'],launcher_config=lambda by:config['pilot_seed_launcher_config'],launcher_check=lambda by:True)
    guard=hashlib.sha256((Path(config['config_root'])/'host_guard_pilot.json').read_bytes()).hexdigest()
    record=approval_record(conn,ledger,manifest,guard_hash=guard)
    capital=E.capital_hash(ledger)
    E.transition(conn,ledger,manifest=manifest,record=record,now=time.time(),guard_hash=guard,apply=True)
    conn.close();ledger.close()
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture-two');runtime['initialize'](gen)
        actors={r[0]:json.loads(r[1]) for r in supervisor.conn.execute('SELECT id,payload_json FROM agents')}
        head=next(k for k,v in actors.items() if v['role']=='Head');scout=next(k for k,v in actors.items() if v['role']=='Scout')
        calls=[]
        def call(adapter,doc,**kw):
            frame=json.loads(doc['user']);ctx=frame['context'];calls.append((ctx,doc['user']))
            asks=ctx['actor_id']==head and len(calls)==1
            near_ask=ctx['actor_id']==scout and len(calls)==3
            requests=[{'tool':'msg.send','args':{'to_actor_id':scout,'text':'Host-verified written message','refs':None}},
                      {'tool':'task.set','args':{'title':'Verify source','note':'unknown'}}] if asks else [{'tool':'desk.near_pass_list','args':{}}] if near_ask else []
            return {'text':json.dumps(dict(done=not (asks or near_ask),task='review',memo='unknown',requests=requests,refs=None)),'model_echo':doc['model']}
        monkeypatch.setattr(runtime['transports']['claude'].client,'call',call)
        assert supervisor.run_turn(head,gen=gen,deadline_s=30,max_steps=2,turn_id='head-work').state=='completed'
        assert supervisor.run_turn(scout,gen=gen,deadline_s=30,max_steps=2,turn_id='scout-work').state=='completed'
        assert called_near==[{}]
        assert 'Host-verified written message' in calls[-1][1]
        assert supervisor.conn.execute('SELECT count(*) FROM office_dm_reads').fetchone()[0]==1
        assert supervisor.conn.execute('SELECT title,status FROM office_task_heads').fetchone()[:]==('Verify source','open')
        runtime['flush_observations']()
        assert {r[0] for r in supervisor.conn.execute('SELECT epoch_id FROM model_calls')}=={E.NEW}
        assert {r[0] for r in supervisor.conn.execute('SELECT epoch_id FROM handoff_events')}=={E.NEW}
        turn=json.loads(supervisor.conn.execute("SELECT payload_json FROM turn_runs WHERE id='scout-work'").fetchone()[0])
        assert turn['epoch_id']==E.NEW and turn['dm_context'] is True
        supervisor.lease.release(gen)
    finally:runtime['close']()
    ledger=L.open_ledger(args['ledger_path']);assert E.capital_hash(ledger)==capital;ledger.close()


def test_operator_cli_default_dry_run_apply_and_empty_rollback(configured,monkeypatch,tmp_path,capsys):
    import importlib.util
    import shutil
    import time
    from pathlib import Path
    from sniper.office import runtime as R
    config,args,_=configured
    runtime=R.build(**args)
    supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture');runtime['initialize'](gen)
    supervisor.lease.release(gen);runtime['close']()
    release=tmp_path/('b'*40);release.mkdir()
    shutil.copytree(config['config_root'],release/'config/office')
    spec=importlib.util.spec_from_file_location('fixture_epoch_cli',Path(__file__).resolve().parents[2]/'scripts/office_epoch_transition.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli);monkeypatch.setattr(cli,'ROOT',release)
    argv=['--store',str(args['store_path']),'--ledger',str(args['ledger_path']),'--clock-file',str(tmp_path/'unused-clock')]
    before=args['store_path'].read_bytes()
    assert cli.main(argv+['--inspect'],clock=time.time)==0
    draft=json.loads(capsys.readouterr().out)['record']
    assert draft['release']['sha']=='b'*40 and draft['tool_contract_hash']['old'] is None
    assert args['store_path'].read_bytes()==before
    draft['tool_contract_hash']['old']='a'*64;draft['approvals']['alpha']='fixture-only-approved'
    record=tmp_path/'record.json';record.write_text(json.dumps(draft));pin=hashlib.sha256(record.read_bytes()).hexdigest()
    bound=argv+['--record',str(record),'--record-sha256',pin]
    assert cli.main(bound,clock=time.time)==0
    assert json.loads(capsys.readouterr().out)['state']=='dry_run'
    assert args['store_path'].read_bytes()==before
    assert cli.main(bound+['--apply'],clock=time.time)==0
    assert json.loads(capsys.readouterr().out)['state']=='transitioned'
    assert cli.main(bound+['--rollback'],clock=time.time)==0
    assert json.loads(capsys.readouterr().out)['state']=='dry_run_rollback'
    assert cli.main(bound+['--rollback','--apply'],clock=time.time)==0
    assert json.loads(capsys.readouterr().out)['state']=='rolled_back'
    assert cli.main(argv+['--apply'],clock=time.time)==2
    assert 'epoch_approved_record_pin_required' in capsys.readouterr().err


def test_adopted_epoch_refuses_changed_tool_contract(prepared,monkeypatch):
    conn,ledger,manifest,record,_=prepared
    run(prepared,apply=True)
    monkeypatch.setattr(E,'tool_hash',lambda:'0'*64)
    with pytest.raises(ValueError,match='tool_contract'):
        E.adopted(conn,manifest)


def test_new_prompt_profile_cannot_seed_without_explicit_transition(configured):
    from pathlib import Path
    from sniper.office import runtime as R
    config,args,write=configured
    root=Path(config['config_root'])/'prompts/discovery2'
    pins=json.loads((root/'sha256.json').read_text())
    config['pilot_seed_launcher_config']['prompts']={slot:{'text':(root/(slot+'.md')).read_text(),'sha256':pins['files'][slot+'.md']} for slot in pilot_seed.SLOTS}
    write();runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture')
        with pytest.raises(R.RuntimeRefused,match='epoch_transition_required'):runtime['initialize'](gen)
        assert supervisor.conn.execute('SELECT count(*) FROM epochs').fetchone()[0]==0
        supervisor.lease.release(gen)
    finally:runtime['close']()


def test_discovery1_cannot_read_new_near_pass_feed(configured,monkeypatch):
    from sniper.office import runtime as R,broker as B
    config,args,write=configured;calls=[]
    monkeypatch.setattr(R,'near_pass_tools',lambda *a,**k:{'desk.near_pass_list':B.Tool(lambda a,c:calls.append(a) or B.ToolResult({'items':[]},{},None,[],'known'),{},'fixture-near-pass')})
    runtime=R.build(**args)
    try:
        supervisor=runtime['supervisor'];gen=supervisor.lease.acquire('fixture');runtime['initialize'](gen)
        actors={r[0]:json.loads(r[1]) for r in supervisor.conn.execute('SELECT id,payload_json FROM agents')}
        scout=next(k for k,a in actors.items() if a['role']=='Scout')
        def call(adapter,doc,**kw):
            return {'text':json.dumps(dict(done=False,task='read',memo='',refs=None,requests=[{'tool':'desk.near_pass_list','args':{}}])),'model_echo':doc['model']}
        monkeypatch.setattr(runtime['transports']['claude'].client,'call',call)
        result=supervisor.run_turn(scout,gen=gen,deadline_s=30,max_steps=1,turn_id='blocked-near-pass')
        assert calls==[]
        assert result.value['receipts'][0]['outcome']=='refused'
        assert result.value['receipts'][0]['result']['reason']=='work_epoch_not_active'
        supervisor.lease.release(gen)
    finally:runtime['close']()
