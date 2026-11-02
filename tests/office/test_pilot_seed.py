"""Synthetic launcher pins/prompts only; never a runtime seed."""
import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from sniper.office import roster as R, store as S
from sniper.office.pilot_seed import seed_pilot_org
from sniper.office.report import read_state


@pytest.fixture
def env(tmp_path):
    root=tmp_path/'config';root.mkdir()
    source=Path(__file__).resolve().parents[2]/'config'/'office'
    files=('pilot_org.json','limits_pilot.json','pilot_models.json','book_policy_pilot.json')
    for name in files:shutil.copyfile(source/name,root/name)
    launch=dict(schema='office-pilot-seed-launcher-v1',files={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
        prompts={slot:dict(text='Synthetic fixture prompt '+slot,sha256=hashlib.sha256(('Synthetic fixture prompt '+slot).encode()).hexdigest()) for slot in ('head','scout','judge_claude','judge_gpt')})
    conn=S.open_store(tmp_path/'office.db')
    args=dict(config_root=root,launcher_config=lambda by:copy.deepcopy(launch),launcher_check=lambda by:True,clock=lambda:1000.,by='crew')
    yield conn,args,launch
    conn.close()


def test_fresh_seed_is_four_seats_one_fixed_discovery_epoch_and_retry_is_noop(env,tmp_path):
    conn,args,_=env
    first=seed_pilot_org(conn,**args)
    before=conn.total_changes
    assert seed_pilot_org(conn,**args)==first
    assert conn.total_changes==before
    state=read_state(tmp_path,now=1001.)
    assert sorted(p['role'] for p in state['agents']['items'])==['Head','Judge','Judge','Scout']
    assert sorted(p['model'] for p in state['agents']['items'])==['claude-opus-5-5','claude-opus-5-5','claude-sonnet-5-5','gpt-6-luna']
    epochs=conn.execute('SELECT id,payload_json FROM epochs').fetchall()
    assert len(epochs)==1 and epochs[0][0]=='office-pilot-discovery-1'
    epoch=json.loads(epochs[0][1])
    assert epoch['kind']=='discovery' and epoch['pilot'] is True and epoch['evaluation_allowed'] is False
    assert conn.execute('SELECT count(*) FROM org_changes').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0]==0
    assert first['seed_manifest_sha256']==epoch['seed_manifest_sha256']


@pytest.mark.parametrize('foreign',['agent','epoch'])
def test_foreign_state_refuses_without_clearing(env,foreign):
    conn,args,_=env
    table='agents' if foreign=='agent' else 'epochs'
    conn.execute('INSERT INTO '+table+' VALUES(?,?)',('foreign','{}'));conn.commit()
    with pytest.raises(R.OrgRefused,match='foreign|existing'):
        seed_pilot_org(conn,**args)
    assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==1


def test_different_manifest_or_prompt_pin_refuses(env):
    conn,args,launch=env
    seed_pilot_org(conn,**args)
    launch['prompts']['head']['text']='Different approved fixture prompt'
    launch['prompts']['head']['sha256']=hashlib.sha256(launch['prompts']['head']['text'].encode()).hexdigest()
    with pytest.raises(R.OrgRefused,match='mismatch'):
        seed_pilot_org(conn,**args)
    assert conn.execute('SELECT count(*) FROM agents').fetchone()[0]==4


def test_failure_rolls_back_all_hires_then_retry_succeeds(env,monkeypatch):
    conn,args,_=env
    original=R._put_agent;calls=[]
    def fail(conn,agent,**kw):
        calls.append(agent)
        if len(calls)==2:raise RuntimeError('synthetic failure')
        return original(conn,agent,**kw)
    monkeypatch.setattr(R,'_put_agent',fail)
    with pytest.raises(RuntimeError):seed_pilot_org(conn,**args)
    for table in ('agents','epochs','org_changes'):
        assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    monkeypatch.setattr(R,'_put_agent',original)
    seed_pilot_org(conn,**args)
    assert conn.execute('SELECT count(*) FROM agents').fetchone()[0]==4


@pytest.mark.parametrize('bad',['pin','prompt','caller'])
def test_untrusted_or_unpinned_seed_refuses(env,bad):
    conn,args,launch=env
    if bad=='pin':launch['files']['pilot_org.json']='0'*64
    elif bad=='prompt':launch['prompts']['head']['sha256']='0'*64
    else:args['by']='head'
    with pytest.raises((ValueError,R.OrgRefused)):seed_pilot_org(conn,**args)
    assert conn.execute('SELECT count(*) FROM agents').fetchone()[0]==0


def test_modified_seeded_agent_is_not_an_exact_retry(env):
    conn,args,_=env
    seeded=seed_pilot_org(conn,**args)
    aid=seeded['agent_ids']['head']
    value=json.loads(conn.execute('SELECT payload_json FROM agents WHERE id=?',(aid,)).fetchone()[0])
    value['model']='foreign-model'
    conn.execute('UPDATE agents SET payload_json=? WHERE id=?',(json.dumps(value),aid));conn.commit()
    with pytest.raises(R.OrgRefused,match='mismatch'):
        seed_pilot_org(conn,**args)


def test_later_hire_keeps_normal_discovery_epoch_behavior(env):
    conn,args,_=env
    seeded=seed_pilot_org(conn,**args)
    lim=json.loads((args['config_root']/'limits_pilot.json').read_text())
    R.fire(conn,lim,clock=args['clock'],agent_id=seeded['agent_ids']['judge_gpt'],reason='fixture restaff',by='crew')
    R.hire(conn,lim,clock=args['clock'],role='Judge',pod='pod1',provider='codex',model='gpt-6-luna',family='gpt',idempotency_key='replacement-fixture',persona_fn=lambda *a:dict(name='Fixture',personality='Careful.',backstory='Discovery only.'),policy_hash='a'*64,prompt_hash='b'*64,by='crew')
    open_epoch=json.loads(conn.execute("SELECT payload_json FROM epochs WHERE json_extract(payload_json,'$.closed_at') IS NULL").fetchone()[0])
    assert open_epoch['epoch_id']!=seeded['epoch_id'] and open_epoch['kind']=='discovery'
    old=json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(seeded['epoch_id'],)).fetchone()[0])
    assert old['closed_at']==1000.


@pytest.mark.parametrize('guard',[False,True,None])
def test_seed_cannot_open_evaluated_epoch(env,guard):
    conn,args,_=env
    seed_pilot_org(conn,**args)
    lim=json.loads((args['config_root']/'limits_pilot.json').read_text())
    with pytest.raises(R.OrgRefused):
        R.open_evaluated_epoch(conn,lim,clock=args['clock'],hypothesis_id='fixture',by='crew',reason='fixture',launcher_check=lambda by:True,host_guard_is_pilot=guard)
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0]==0


def test_prompt_hashes_match_exact_alpha_approved_bytes():
    root=Path(__file__).resolve().parents[2]/'config'/'office'/'prompts'/'pilot'
    manifest=json.loads((root/'sha256.json').read_text())
    assert manifest['approval_status']=='approved_alpha'
    assert manifest['approver']=='alpha'
    assert manifest['approved_commit']=='abbf8a464d198115e881453ef7c184d1ae5bccc9'
    assert set(manifest['files'])=={'head.md','scout.md','judge_claude.md','judge_gpt.md'}
    for name,digest in manifest['files'].items():
        raw=(root/name).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==digest
        assert len(raw.decode().split())<1500
    for name,digest in manifest['source_pins'].items():
        assert hashlib.sha256((root.parents[1]/name).read_bytes()).hexdigest()==digest


def test_concurrent_bootstraps_commit_one_org_change(env,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _,args,_=env
    def run(_):
        conn=S.open_store(tmp_path/'office.db')
        try:return seed_pilot_org(conn,**args)
        finally:conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,range(2)))
    assert results[0]==results[1]
    conn=env[0]
    assert conn.execute('SELECT count(*) FROM org_changes').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM agents').fetchone()[0]==4


def test_epoch_book_change_is_not_an_exact_retry(env):
    conn,args,_=env
    seeded=seed_pilot_org(conn,**args)
    epoch=json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(seeded['epoch_id'],)).fetchone()[0])
    epoch['book_id']='FOREIGN_BOOK'
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?',(json.dumps(epoch),seeded['epoch_id']));conn.commit()
    with pytest.raises(R.OrgRefused,match='mismatch'):
        seed_pilot_org(conn,**args)
    assert json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(seeded['epoch_id'],)).fetchone()[0])['book_id']=='FOREIGN_BOOK'


def test_fifo_config_refuses_without_blocking(env,tmp_path):
    import os
    import subprocess
    import sys
    _,args,launch=env
    path=args['config_root']/'pilot_org.json';path.unlink();os.mkfifo(path)
    script='''
import json,sys
from pathlib import Path
from sniper.office import store as S
from sniper.office.pilot_seed import seed_pilot_org
conn=S.open_store(Path(sys.argv[1])/'child.db')
try:
    seed_pilot_org(conn,config_root=Path(sys.argv[1])/'config',launcher_config=lambda by:json.loads(sys.argv[2]),launcher_check=lambda by:True,clock=lambda:1000.,by='crew')
except (ValueError,OSError):
    print('refused')
else:
    raise AssertionError('FIFO accepted')
finally:conn.close()
'''
    try:
        result=subprocess.run([sys.executable,'-B','-c',script,str(tmp_path),json.dumps(launch)],capture_output=True,text=True,timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail('resolver blocked on FIFO')
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='refused'
