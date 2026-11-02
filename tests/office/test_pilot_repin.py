"""Hermetic repin transactions: fake units/releases, real private config files; no services or models."""
import argparse
import ast
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import ModuleType

import pytest
from .test_epoch_transition import prepared
from .test_pilot_seed import env

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/office_pilot_activate.sh'


@pytest.fixture
def helper():
    code=SCRIPT.read_text().split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
    tree=ast.parse(code)
    assert isinstance(tree.body[-1],ast.Try)
    module=ModuleType('repin_fixture')
    exec(compile(ast.Module(body=tree.body[:-1],type_ignores=[]),str(SCRIPT),'exec'),module.__dict__)
    return module


@pytest.fixture
def repin(tmp_path,helper,monkeypatch):
    m=helper;repo=tmp_path/'repo';repo.mkdir();old='a'*40;new='b'*40
    runtime=repo/'.runtime/office';runtime.mkdir(parents=True)
    (runtime/'activation.lock').write_bytes(b'');(runtime/'activation.lock').chmod(0o600)
    releases=[runtime/'releases'/sha for sha in (old,new,'c'*40)]
    for r in releases:r.mkdir(parents=True)
    current=runtime/'current';current.symlink_to(releases[0])
    bundles=[tmp_path/'old-bundle',tmp_path/'new-bundle',tmp_path/'third-bundle'];plans={}
    for sha,release,bundle in zip((old,new,'c'*40),releases,bundles):
        bundle.mkdir()
        docs={'runtime.json':{'content_addressed_release_root':str(release),'config_root':str(release/'config/office'),
                              'pilot_seed_launcher_config':{'prompts':{s:dict(text='unchanged',sha256=m.digest(b'unchanged')) for s in ('head','scout','judge_claude','judge_gpt')}}},
              'launcher.json':{'model_roots':[str(release)]},'monitor.json':{'research_enabled':False},
              'transport.json':{'crew_uid':1}}
        raws={n:json.dumps(v).encode() for n,v in docs.items()}
        for name in ('runtime','launcher'):raws[name+'.sha256']=(m.digest(raws[name+'.json'])+'\n').encode()
        manifest={'main_sha':sha,'files':{n:m.digest(v) for n,v in raws.items()}}
        for n,v in raws.items():(bundle/n).write_bytes(v)
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        files={repo/m.CONFIGS[n]:(raw,0o600) for n,raw in raws.items()}
        plans[sha]=(repo,release,files,{},current,{},None)
    for path,(raw,mode) in plans[old][2].items():path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);path.chmod(mode)
    def plan(args,**kw):
        assert kw.get('repin') is True
        return plans[args.sha]
    monkeypatch.setattr(m,'plan',plan)
    calls=[]
    def git(*args,**kwargs):
        calls.append(('git',args[1:]));return None
    monkeypatch.setattr(m,'git',git)
    monkeypatch.setattr(m,'release_valid',lambda *a:True)
    monkeypatch.setattr(m,'inactive',lambda keep=():calls.append(('inactive',tuple(sorted(keep)))))
    monkeypatch.setattr(m,'keep_units',lambda repo,old,new:frozenset())
    monkeypatch.setattr(m,'ctl',lambda *a,**kw:pytest.fail('repin must not control services'))
    monkeypatch.setattr(m,'repin_state',lambda *a,**kw:{'epoch':'office-pilot-discovery-2','generation':2,'capital':'unchanged'})
    code=b"_REFS={}\n_REQUEST_ARGS={}\nSCHEMA={'type':'object'}\n"
    monkeypatch.setattr(m,'blob',lambda repo,sha,name:code if name.endswith('runtime_steps.py') else b'unchanged')
    args=argparse.Namespace(repo=repo,sha=new,bundle=bundles[1],bundle_sha256=m.digest((bundles[1]/'manifest.json').read_bytes()),
        repin_from_bundle=bundles[0],repin_from_bundle_sha256=m.digest((bundles[0]/'manifest.json').read_bytes()),
        apply=False,rollback=False,verify=False)
    return m,args,plans,calls


def test_default_dry_run_does_not_write_or_switch(repin):
    m,a,plans,calls=repin
    before={p:p.read_bytes() for p in plans['a'*40][2]}
    assert m.repin(a)['state']=='dry_run'
    assert plans['a'*40][4].resolve()==plans['a'*40][1]
    assert {p:p.read_bytes() for p in before}==before
    assert not (a.repo/'.runtime/office/repin.json').exists()


def test_apply_adopts_existing_state_is_idempotent_and_rolls_back(repin):
    m,a,plans,calls=repin;a.apply=True
    assert m.repin(a)['state']=='repinned'
    assert plans['b'*40][4].resolve()==plans['b'*40][1]
    for p,(raw,_) in plans['b'*40][2].items():assert p.read_bytes()==raw
    assert m.repin(a)['state']=='already_repinned'
    a.rollback=True
    assert m.repin(a)['state']=='rolled_back'
    for p,(raw,_) in plans['a'*40][2].items():assert p.read_bytes()==raw
    assert plans['a'*40][4].resolve()==plans['a'*40][1]
    assert not list(a.repo.rglob('*.db'))


@pytest.mark.parametrize('field',['bundle_sha256','repin_from_bundle_sha256'])
def test_both_external_seals_are_required(repin,field):
    m,a,plans,_=repin;setattr(a,field,'0'*64);a.apply=True
    with pytest.raises(ValueError):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]


def test_semantic_configuration_change_refuses(repin):
    m,a,plans,_=repin
    p=a.repo/'config/office/runtime.json';new=json.loads(plans['b'*40][2][p][0]);new['enabled']=False
    plans['b'*40][2][p]=(json.dumps(new).encode(),0o600)
    with pytest.raises(ValueError,match='repin_config_changed'):m.repin(a)


def test_guard_or_sdk_bundle_metadata_change_refuses(repin):
    m,a,_,_=repin;p=a.bundle/'manifest.json';doc=json.loads(p.read_text());doc['guard_manifest_sha256']='0'*64
    p.write_text(json.dumps(doc));a.bundle_sha256=m.digest(p.read_bytes())
    with pytest.raises(ValueError,match='repin_bundle_policy_changed'):m.repin(a)


def test_activation_lock_contention_refuses_without_writes(repin):
    import fcntl
    m,a,_,_=repin;a.apply=True
    with (a.repo/'.runtime/office/activation.lock').open('r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):m.repin(a)
    assert not (a.repo/'.runtime/office/repin.json').exists()


@pytest.mark.parametrize('changed',['runtime_steps.py','head.md'])
def test_tool_or_prompt_change_requires_new_epoch(repin,monkeypatch,changed):
    m,a,_,_=repin;original=m.blob
    def blob(repo,sha,name):
        raw=original(repo,sha,name)
        return raw.replace(b"'object'",b"'string'") if sha==a.sha and name.endswith(changed) and changed.endswith('.py') else (
            b'changed' if sha==a.sha and name.endswith(changed) else raw)
    monkeypatch.setattr(m,'blob',blob)
    with pytest.raises(ValueError,match='repin_epoch_change_required'):m.repin(a)


def test_active_office_or_unknown_state_refuses(repin,monkeypatch):
    m,a,plans,_=repin;a.apply=True
    monkeypatch.setattr(m,'repin_state',lambda *a,**kw:(_ for _ in ()).throw(ValueError('epoch_or_lease_unknown')))
    with pytest.raises(ValueError):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]


def test_partial_config_failure_restores_every_old_byte_and_link(repin,monkeypatch):
    m,a,plans,_=repin;a.apply=True;original=m.atomic;failed=False
    def atomic(path,raw,mode):
        nonlocal failed
        if path==a.repo/'data/office-control/launcher.json' and not failed:
            failed=True;raise OSError('injected write failure')
        return original(path,raw,mode)
    monkeypatch.setattr(m,'atomic',atomic)
    with pytest.raises(OSError,match='injected'):m.repin(a)
    for p,(raw,_) in plans['a'*40][2].items():assert p.read_bytes()==raw
    assert plans['a'*40][4].resolve()==plans['a'*40][1]


def test_rollback_refuses_foreign_config_without_overwriting_it(repin):
    m,a,plans,_=repin;a.apply=True;assert m.repin(a)['state']=='repinned'
    p=a.repo/'config/office/runtime.json';p.write_bytes(b'foreign');a.rollback=True
    with pytest.raises(ValueError):m.repin(a)
    assert p.read_bytes()==b'foreign'
    assert plans['b'*40][4].resolve()==plans['b'*40][1]


def test_post_switch_verification_failure_restores_link(repin,monkeypatch):
    m,a,plans,_=repin;a.apply=True;n=0
    def state(*args,**kwargs):
        nonlocal n;n+=1
        if plans['b'*40][4].resolve()==plans['b'*40][1]:raise ValueError('witness_changed')
        return {'epoch':'office-pilot-discovery-2','generation':2,'capital':'unchanged'}
    monkeypatch.setattr(m,'repin_state',state)
    with pytest.raises(ValueError,match='witness_changed'):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]


def test_link_rename_failure_after_switch_rolls_back(repin,monkeypatch):
    m,a,plans,_=repin;a.apply=True;original=m.repin_link;failed=False
    def link(path,target):
        nonlocal failed
        original(path,target)
        if not failed:failed=True;raise OSError('after rename')
    monkeypatch.setattr(m,'repin_link',link)
    with pytest.raises(OSError,match='after rename'):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]
    for p,(raw,_) in plans['a'*40][2].items():assert p.read_bytes()==raw


def test_crashed_journal_requires_explicit_rollback(repin):
    m,a,plans,_=repin;a.apply=True;assert m.repin(a)['state']=='repinned'
    path=a.repo/'.runtime/office/repin.json';doc=m.journal_load(path);doc['phase']='applying';m.save(path,doc)
    m.repin_link(plans['a'*40][4],plans['a'*40][1])
    with pytest.raises(ValueError,match='repin_recovery_required'):m.repin(a)
    a.rollback=True;assert m.repin(a)['state']=='rolled_back'


def test_foreign_current_link_is_never_replaced(repin,tmp_path):
    m,a,plans,_=repin;a.apply=True;current=plans['a'*40][4]
    foreign=tmp_path/'foreign';foreign.mkdir();current.unlink();current.symlink_to(foreign)
    with pytest.raises(ValueError,match='repin_current_unknown'):m.repin(a)
    assert current.resolve()==foreign


def test_stopped_units_are_required_before_journal(repin,monkeypatch):
    m,a,_,_=repin;a.apply=True
    monkeypatch.setattr(m,'inactive',lambda *_a,**_k:(_ for _ in ()).throw(ValueError('unit_active')))
    with pytest.raises(ValueError,match='unit_active'):m.repin(a)
    assert not (a.repo/'.runtime/office/repin.json').exists()


def test_config_journal_retention_is_two_files(repin):
    m,a,plans,_=repin;a.apply=True;assert m.repin(a)['state']=='repinned'
    a.repin_from_bundle=a.bundle;a.repin_from_bundle_sha256=a.bundle_sha256
    a.sha='c'*40;a.bundle=a.bundle.parent/'third-bundle';a.bundle_sha256=m.digest((a.bundle/'manifest.json').read_bytes())
    assert m.repin(a)['state']=='repinned'
    journals=list((a.repo/'.runtime/office').glob('repin*.json'))
    assert {j.name for j in journals}=={'repin.json','repin.previous.json'}
    assert all(j.stat().st_mode & 0o777==0o600 for j in journals)


def test_recovery_accepts_approved_mixed_files_but_never_foreign(repin):
    m,a,plans,_=repin;a.apply=True;assert m.repin(a)['state']=='repinned'
    p=a.repo/'config/office/runtime.json';p.write_bytes(plans['a'*40][2][p][0])
    a.rollback=True
    assert m.repin(a)['state']=='rolled_back'
    for p,(raw,_) in plans['a'*40][2].items():assert p.read_bytes()==raw


@pytest.fixture
def state_store(tmp_path,helper):
    m=helper;clock=tmp_path/'clock.json';clock.write_text(json.dumps({'offset_s':0,'at':time.time()}))
    prompts={slot:{'text':slot,'sha256':m.digest(slot.encode())} for slot in ('head','scout','judge_claude','judge_gpt')}
    actors={slot:dict(role=role,family=family,generation=2,state='active',prompt_hash=prompts[slot]['sha256']) for slot,role,family in
            (('head','Head','claude'),('scout','Scout','claude'),('judge_claude','Judge','claude'),('judge_gpt','Judge','gpt'))}
    tool=m.digest(m.canonical({'type':'object'}));epoch={'record':{'tool_contract_hash':{'new':tool}}}
    event={'new_actors':actors,'new_epoch':epoch,'note':'découverte'}
    raw=json.dumps(event,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode();receipt=m.digest(raw)
    store=tmp_path/'office.db';c=sqlite3.connect(store)
    c.executescript('CREATE TABLE _store_info(id INTEGER PRIMARY KEY,purpose TEXT);'
        'CREATE TABLE supervisor_lease(id INTEGER PRIMARY KEY,expires_at REAL);'
        'CREATE TABLE office_epoch_control(id INTEGER PRIMARY KEY,payload_json TEXT);'
        'CREATE TABLE org_changes(row_hash TEXT PRIMARY KEY,payload_json TEXT,prev_hash TEXT);'
        'CREATE TABLE agents(id TEXT PRIMARY KEY,payload_json TEXT);CREATE TABLE epochs(id TEXT PRIMARY KEY,payload_json TEXT);')
    c.execute("INSERT INTO _store_info VALUES(1,'office')");c.execute('INSERT INTO supervisor_lease VALUES(1,?)',(time.time()-10,))
    c.execute('INSERT INTO office_epoch_control VALUES(1,?)',(json.dumps(dict(active_epoch='office-pilot-discovery-2',receipt_id=receipt)),))
    c.execute('INSERT INTO org_changes VALUES(?,?,?)',(receipt,raw.decode(),''))
    c.executemany('INSERT INTO agents VALUES(?,?)',[(k,json.dumps(v)) for k,v in actors.items()])
    c.executemany('INSERT INTO epochs VALUES(?,?)',[('office-pilot-discovery-1','{}'),('office-pilot-discovery-2',json.dumps(epoch))]);c.commit();c.close()
    ledger=tmp_path/'ledger.db';c=sqlite3.connect(ledger)
    c.executescript('CREATE TABLE books(book_id TEXT PRIMARY KEY,kind TEXT);CREATE TABLE capital_flows(id INTEGER PRIMARY KEY,book_id TEXT,usd REAL);'
                    "INSERT INTO books VALUES('pilot-floor','discovery');INSERT INTO capital_flows VALUES(1,'pilot-floor',1000);");c.close()
    runtime=dict(clock_file=str(clock),store_path=str(store),ledger_path=str(ledger),pilot_seed_launcher_config={'prompts':prompts})
    return m,runtime,tool,store,ledger


def test_state_witness_is_readonly_and_keeps_exact_epoch_generation_capital(state_store,tmp_path):
    m,runtime,tool,store,ledger=state_store
    before=[p.read_bytes() for p in (store,ledger)]
    result=m.repin_state(tmp_path,runtime,tool)
    assert result['epoch']=='office-pilot-discovery-2' and result['generation']==2
    assert [p.read_bytes() for p in (store,ledger)]==before
    assert m.repin_state(tmp_path,runtime,tool)==result


def test_state_witness_matches_real_epoch_adoption(helper,prepared,tmp_path):
    from sniper.office import epoch_transition as E
    conn,ledger,manifest,record,_=prepared
    E.transition(conn,ledger,manifest=manifest,record=record,now=1100.,guard_hash='c'*64,apply=True)
    before=E.adopted(conn,manifest);capital=E.capital_hash(ledger)
    clock=tmp_path/'clock.json';clock.write_text(json.dumps(dict(offset_s=0,at=time.time())))
    runtime=dict(clock_file=str(clock),store_path=conn.execute('PRAGMA database_list').fetchone()[2],
                 ledger_path=ledger.execute('PRAGMA database_list').fetchone()[2],
                 pilot_seed_launcher_config={'prompts':{slot:dict(text='new-'+slot,sha256=helper.digest(('new-'+slot).encode()))
                       for slot in ('head','scout','judge_claude','judge_gpt')}})
    witness=helper.repin_state(tmp_path,runtime,E.tool_hash())
    assert witness['capital_sha256']==capital
    assert E.adopted(conn,manifest)==before and E.capital_hash(ledger)==capital


@pytest.mark.parametrize('mutation', ['lease','roster','epoch','capital','receipt','clock','tool','prompt'])
def test_unknown_or_changed_state_refuses(state_store,tmp_path,mutation):
    m,runtime,tool,store,ledger=state_store
    c=sqlite3.connect(ledger if mutation=='capital' else store)
    if mutation=='lease':c.execute('UPDATE supervisor_lease SET expires_at=?',(time.time()+60,))
    if mutation=='roster':c.execute("UPDATE agents SET payload_json='{}' WHERE id='head'")
    if mutation=='epoch':c.execute("UPDATE epochs SET payload_json='{}' WHERE id='office-pilot-discovery-2'")
    if mutation=='capital':c.execute('DELETE FROM capital_flows')
    if mutation=='receipt':c.execute("UPDATE org_changes SET payload_json='{}'")
    c.commit();c.close()
    if mutation=='clock':Path(runtime['clock_file']).write_text(json.dumps(dict(offset_s=0,at=time.time()-4000)))
    if mutation=='tool':tool='0'*64
    if mutation=='prompt':runtime['pilot_seed_launcher_config']['prompts']['head']['sha256']='0'*64
    with pytest.raises((ValueError,KeyError,TypeError)):m.repin_state(tmp_path,runtime,tool)


EVAL_PREFIX='config/office/prompts/office-paper-eval-1/'


@pytest.fixture
def eval1(repin,monkeypatch):
    """New bundle = office-paper-eval-1: new tool contract, 5-slot approved profile, the 4 runtime fields flipped."""
    m,a,plans,calls=repin
    prompts={s:dict(text='eval1 '+s,sha256=m.digest(('eval1 '+s).encode())) for s in m.EVAL1_SLOTS}
    pins={n:m.digest(('file '+n).encode()) for n in m.EVAL1_FILES}
    approval={'approval_status':'approved_alpha','files':{s+'.md':p['sha256'] for s,p in prompts.items()},'source_pins':pins}
    old_code=b"_REFS={}\n_REQUEST_ARGS={}\nSCHEMA={'type':'object'}\n"
    state={'approval':approval}
    def blob(repo,sha,name):
        if name.endswith('runtime_steps.py'):return old_code.replace(b"'object'",b"'object','x':1") if sha==a.sha else old_code
        if sha==a.sha and name.startswith(EVAL_PREFIX):
            leaf=name[len(EVAL_PREFIX):]
            return json.dumps(state['approval']).encode() if leaf=='sha256.json' else prompts[leaf[:-3]]['text'].encode()
        if sha==a.sha and name.startswith('config/office/') and name.rsplit('/',1)[1] in m.EVAL1_FILES:
            return ('file '+name.rsplit('/',1)[1]).encode()
        return b'unchanged'
    monkeypatch.setattr(m,'blob',blob)
    p=a.repo/'config/office/runtime.json'
    def set_runtime(change=None):
        doc=json.loads(plans['b'*40][2][p][0])
        doc.update(discovery_only=False,evaluated_epoch_allowed=True,
                   model_manifest={'path':str(plans['b'*40][1]/'config/office/pilot_models_eval1.json'),'sha256':pins['pilot_models_eval1.json']},
                   pilot_seed_launcher_config={'schema':'office-pilot-seed-launcher-v1','files':pins,'prompts':prompts})
        if change:change(doc)
        plans['b'*40][2][p]=(json.dumps(doc).encode(),0o600)
    set_runtime()
    a.epoch_change='eval1'
    return m,a,plans,calls,set_runtime,state


def test_eval1_epoch_change_repins_and_rolls_back(eval1):
    m,a,plans,calls,_,_=eval1
    assert m.repin(a)['state']=='dry_run'
    a.apply=True
    assert m.repin(a)['state']=='repinned'
    assert plans['b'*40][4].resolve()==plans['b'*40][1]
    assert json.loads((a.repo/'.runtime/office/repin.json').read_text())['epoch_change']=='eval1'
    assert m.repin(a)['state']=='already_repinned'
    a.rollback=True
    assert m.repin(a)['state']=='rolled_back' and plans['a'*40][4].resolve()==plans['a'*40][1]


def test_eval1_change_without_the_flag_still_refuses(eval1):
    m,a,*_=eval1;a.epoch_change=None
    with pytest.raises(ValueError,match='repin_epoch_change_required'):m.repin(a)


@pytest.mark.parametrize('bad',['other_runtime_field','unapproved','prompt_text','discovery_flags','model_pin','launcher','same_tool'])
def test_eval1_epoch_change_refuses_anything_beyond_the_eval1_delta(eval1,monkeypatch,bad):
    m,a,plans,_,set_runtime,state=eval1
    if bad=='other_runtime_field':set_runtime(lambda d:d.update(enabled=False))
    elif bad=='unapproved':state['approval']=dict(state['approval'],approval_status='pending_review')
    elif bad=='prompt_text':set_runtime(lambda d:d['pilot_seed_launcher_config']['prompts']['postmortem'].update(text='other'))
    elif bad=='discovery_flags':set_runtime(lambda d:d.update(discovery_only=True,evaluated_epoch_allowed=False))
    elif bad=='model_pin':set_runtime(lambda d:d['model_manifest'].update(sha256='0'*64))
    elif bad=='launcher':
        p=a.repo/'data/office-control/launcher.json';doc=json.loads(plans['b'*40][2][p][0]);doc['extra']=1
        plans['b'*40][2][p]=(json.dumps(doc).encode(),0o600)
    else:
        orig=m.blob;monkeypatch.setattr(m,'blob',lambda repo,sha,name:orig(repo,'a'*40,name) if name.endswith('runtime_steps.py') else orig(repo,sha,name))
    a.apply=True
    with pytest.raises(ValueError):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]
    assert not (a.repo/'.runtime/office/repin.json').exists()


@pytest.fixture
def eval1_same(repin,monkeypatch):
    """Same-epoch repin inside office-paper-eval-1: both releases carry the same approved eval-1 profile and tool."""
    m,a,plans,calls=repin
    prompts={s:dict(text='eval1 '+s,sha256=m.digest(('eval1 '+s).encode())) for s in m.EVAL1_SLOTS}
    pins={n:m.digest(('file '+n).encode()) for n in m.EVAL1_FILES}
    state={'approval':{'approval_status':'approved_alpha','files':{s+'.md':p['sha256'] for s,p in prompts.items()},'source_pins':pins},
           'new_text':{}}
    code=b"_REFS={}\n_REQUEST_ARGS={}\nSCHEMA={'type':'object','x':1}\n"
    def blob(repo,sha,name):
        if name.endswith('runtime_steps.py'):return state.get('new_code',code) if sha==a.sha else code
        if name.startswith(EVAL_PREFIX):
            leaf=name[len(EVAL_PREFIX):]
            if leaf=='sha256.json':return json.dumps(state['approval']).encode()
            return state['new_text'].get(leaf,prompts[leaf[:-3]]['text']).encode() if sha==a.sha else prompts[leaf[:-3]]['text'].encode()
        if name.startswith('config/office/') and name.rsplit('/',1)[1] in m.EVAL1_FILES:return ('file '+name.rsplit('/',1)[1]).encode()
        return b'unchanged'
    monkeypatch.setattr(m,'blob',blob)
    p=a.repo/'config/office/runtime.json'
    for sha in ('a'*40,'b'*40):
        doc=json.loads(plans[sha][2][p][0])
        doc.update(discovery_only=False,evaluated_epoch_allowed=True,
                   model_manifest={'path':str(plans[sha][1]/'config/office/pilot_models_eval1.json'),'sha256':pins['pilot_models_eval1.json']},
                   pilot_seed_launcher_config={'schema':'office-pilot-seed-launcher-v1','files':pins,'prompts':prompts})
        raw=json.dumps(doc).encode();plans[sha][2][p]=(raw,0o600)
        s=a.repo/'config/office/runtime.sha256';plans[sha][2][s]=((m.digest(raw)+'\n').encode(),0o600)
    for path,(raw,mode) in plans['a'*40][2].items():path.write_bytes(raw)
    return m,a,plans,calls,state


def test_same_epoch_eval1_repin_applies_and_rolls_back(eval1_same):
    m,a,plans,calls,_=eval1_same
    assert m.repin(a)['state']=='dry_run'
    a.apply=True
    assert m.repin(a)['state']=='repinned' and plans['b'*40][4].resolve()==plans['b'*40][1]
    a.rollback=True
    assert m.repin(a)['state']=='rolled_back'


@pytest.mark.parametrize('bad',['prompt','tool','epoch_change_again','runtime_field'])
def test_same_epoch_eval1_repin_refuses_changes(eval1_same,bad):
    m,a,plans,_,state=eval1_same
    if bad=='prompt':state['new_text']['judge_gpt.md']='changed'
    elif bad=='tool':state['new_code']=b"_REFS={}\n_REQUEST_ARGS={}\nSCHEMA={'type':'object','x':2}\n"
    elif bad=='epoch_change_again':a.epoch_change='eval1'
    else:
        p=a.repo/'config/office/runtime.json';doc=json.loads(plans['b'*40][2][p][0]);doc['max_steps']=5
        plans['b'*40][2][p]=(json.dumps(doc).encode(),0o600)
    a.apply=True
    with pytest.raises(ValueError):m.repin(a)
    assert plans['a'*40][4].resolve()==plans['a'*40][1]


def test_eval1_state_witness_reads_the_live_eval1_shape(helper,prepared,tmp_path,monkeypatch):
    from sniper.office import eval_transition as X, ledger as L, store as S
    from .test_epoch_transition import run as run_discovery
    from .test_eval_transition import _manifest, BP, CO
    monkeypatch.setattr(X,'PREREG','docs/superpowers/specs/2026-10-29-office-paper-eval-1-epoch.md')
    conn,ledger,manifest,record,_=prepared
    run_discovery(prepared,apply=True)
    m_=_manifest(manifest);rec=X.approval_draft(conn,m_,book_policy=BP,near_pass_cohort=CO);rec['approvals']={'alpha':'f','gamma':'f'}
    X.transition(conn,manifest=m_,record=rec,now=1200.,book_policy=BP,near_pass_cohort=CO,apply=True)
    eval_ledger=tmp_path/'eval-ledger.db';el=L.open_ledger(eval_ledger);X.provision_ledger(el,now=1300.);el.close()
    store_path=Path(conn.execute('PRAGMA database_list').fetchone()[2])
    clock=tmp_path/'clock.json';clock.write_text(json.dumps({'offset_s':0.,'at':time.time()}))
    prompts={s['slot']:dict(text='p-'+s['slot'],sha256=s['prompt_hash']) for s in m_['seats']}
    runtime={'discovery_only':False,'evaluated_epoch_allowed':True,'clock_file':str(clock),'store_path':str(store_path),
             'ledger_path':str(eval_ledger),'pilot_seed_launcher_config':{'prompts':prompts}}
    monkeypatch.setattr(helper,'digest',lambda raw:hashlib.sha256(raw).hexdigest() if not raw.startswith(b'p-') else
                        next(s['prompt_hash'] for s in m_['seats'] if raw==('p-'+s['slot']).encode()))
    tool=rec['tool_contract_hash']
    out=helper.repin_state(tmp_path,runtime,tool)
    assert out['epoch']=='office-paper-eval-1' and out['roster']==5
    with pytest.raises(ValueError,match='repin_tool_not_adopted'):helper.repin_state(tmp_path,runtime,'0'*64)
    conn.execute("UPDATE agents SET payload_json=json_set(payload_json,'$.generation',9) WHERE id=?",(X.POSTMORTEM_ID,));conn.commit()
    with pytest.raises(ValueError,match='repin_roster_changed'):helper.repin_state(tmp_path,runtime,tool)


def test_byte_identical_monitor_and_watchdog_stay_running(repin,monkeypatch):
    m,a,plans,calls=repin;a.apply=True
    monkeypatch.setattr(m,'keep_units',lambda repo,old,new:frozenset({'office-monitor.service','office-watchdog.service'}))
    seen=[]
    real=m.plan
    monkeypatch.setattr(m,'plan',lambda args,**kw:seen.append(kw.get('keep')) or real(args,**kw))
    assert m.repin(a)['state']=='repinned'
    assert all(k=={'office-monitor.service','office-watchdog.service'} for k in seen)
    assert ('inactive',('office-monitor.service','office-watchdog.service')) in calls


def _git_repo(tmp_path,files_by_commit):
    import subprocess
    repo=tmp_path/'g';repo.mkdir()
    g=lambda *x:subprocess.check_output(['git','-C',str(repo),*x],text=True).strip()
    g('init','-q','-b','main');g('config','user.email','f@x.invalid');g('config','user.name','f')
    shas=[]
    for files in files_by_commit:
        for name,text in files.items():
            p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        g('add','-A');g('commit','-q','-m','c');shas.append(g('rev-parse','HEAD'))
    return repo,shas


BASE={'deploy/office/office-monitor.service':'u1','deploy/office/office-watchdog.service':'u2',
      'scripts/office_host_monitor.py':'from office_measure_headroom import pressure\nimport json\n',
      'scripts/office_measure_headroom.py':'from sniper.office import clock\n','sniper/__init__.py':'','sniper/office/__init__.py':'',
      'sniper/office/clock.py':'X=1\n','scripts/office_watchdog.py':'import os\n','config/office/host_guard_pilot.json':'{}',
      'sniper/office/runtime.py':'A=1\n'}


@pytest.mark.parametrize('change,kept',[
    ({'sniper/office/runtime.py':'A=2\n'},{'office-monitor.service','office-watchdog.service'}),   # unrelated module
    ({'sniper/office/clock.py':'X=2\n'},{'office-watchdog.service'}),                             # monitor's transitive import
    ({'config/office/host_guard_pilot.json':'{"a":1}'},{'office-monitor.service'}),
    ({'deploy/office/office-monitor.service':'u9'},{'office-watchdog.service'}),
])
def test_keep_units_requires_every_loaded_file_identical(helper,tmp_path,change,kept):
    repo,(old,new)=_git_repo(tmp_path,[BASE,change])
    assert helper.keep_units(repo,old,new)==kept


def test_epoch_change_never_keeps_units(eval1,monkeypatch):
    m,a,plans,calls,_,_=eval1
    monkeypatch.setattr(m,'keep_units',lambda *x:pytest.fail('epoch change must stop every unit'))
    a.apply=True
    assert m.repin(a)['state']=='repinned'


@pytest.mark.parametrize('code',['import importlib\n','x=__import__("os")\n','from importlib import import_module\n'])
def test_a_dynamic_import_in_a_kept_unit_keeps_nothing(helper,tmp_path,code):
    files=dict(BASE,**{'scripts/office_watchdog.py':code})
    repo,(old,new)=_git_repo(tmp_path,[files,{'sniper/office/runtime.py':'A=3\n'}])
    assert helper.keep_units(repo,old,new)=={'office-monitor.service'}
