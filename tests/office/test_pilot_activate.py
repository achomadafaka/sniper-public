"""Hermetic Git/systemd fixtures. Never installs in the real user unit directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/office_pilot_activate.sh'
UNITS=('office.slice','office-child-launcher.service','office-monitor.service','office-paper-watcher.service','office-supervisor.service','office-watchdog.service','office-supervisor-wake.service','office-supervisor-wake.timer')


def sha(data):return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path,request):
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo,text=True).strip()
    git('init','-b','main');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
    for name in UNITS:
        path=repo/'deploy/office'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('[Unit]\nDescription=Fixture only\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n')
    (repo/'deploy/office/office-supervisor-wake.service').write_text('[Unit]\nDescription=Fixture wake only\n[Service]\nType=oneshot\nExecStart=/bin/true\nSlice=app.slice\n')
    (repo/'deploy/office/office-supervisor-wake.timer').write_text('[Unit]\nDescription=Fixture wake timer\n[Timer]\nOnBootSec=60s\nOnUnitActiveSec=60s\nUnit=office-supervisor-wake.service\n[Install]\nWantedBy=timers.target\n')
    for name in ('cadence.json','pilot_org.json','limits_pilot.json','pilot_models.json','pilot_models_eval1.json','book_policy_pilot.json','prompts/pilot/sha256.json',*[f'prompts/pilot/{slot}.md' for slot in ('head','scout','judge_claude','judge_gpt')]):
        dest=repo/'config/office'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes((SCRIPT.parents[1]/'config/office'/name).read_bytes())
    eval1=repo/'config/office/prompts/office-paper-eval-1';eval1.mkdir(parents=True)
    eval_pins={n:sha((repo/'config/office'/n).read_bytes()) for n in ('pilot_org.json','limits_pilot.json','pilot_models_eval1.json','book_policy_pilot.json')}
    eval_files={}
    for slot in ('head','scout','judge_claude','judge_gpt','postmortem'):
        raw=(SCRIPT.parents[1]/'config/office/prompts/office-paper-eval-1'/(slot+'.md')).read_bytes();(eval1/(slot+'.md')).write_bytes(raw)
        eval_files[slot+'.md']=sha(raw)
    (eval1/'sha256.json').write_text(json.dumps(dict(approval_status='approved_alpha',files=eval_files,source_pins=eval_pins)))
    (repo/'deploy/office/office-supervisor.service').write_bytes((SCRIPT.parents[1]/'deploy/office/office-supervisor.service').read_bytes())
    for name,text in {'sniper/office/runtime.py':'def build(**kw): raise AssertionError("never run")\n',
        'scripts/office_supervisor.py':'# fixture only\n',
        'scripts/office_supervisor_wake.py':'# fixture wake only\n',
        'scripts/office_pilot_caps.py':'import json\nprint(json.dumps(dict(ready_for_child_preexec='+str(bool(getattr(request,'param',False)))+',state="fixture_only")))\n',
        'config/office/host_guard_pilot.json':json.dumps(dict(version='office-host-guard-pilot-v3',mode='pilot',discovery_only=True,evaluation_allowed=False))}.items():
        p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    (repo/'scripts/office_watchdog.py').write_bytes((SCRIPT.parent/'office_watchdog.py').read_bytes())
    git('add','--','deploy','scripts','sniper','config');git('commit','-m','fixture')
    commit=git('rev-parse','HEAD');release=repo/'.runtime/office/releases'/commit
    bundle=tmp_path/'bundle';bundle.mkdir()
    launcher=dict(version='final-fixture',model_roots=[str(release)],guard_file=str(repo/'data/office-control/guard/guard.json'))
    config=release/'config/office'
    group=f'/fixture-user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/office.slice'
    approvals=json.loads((repo/'config/office/prompts/pilot/sha256.json').read_text())
    runtime=dict(version='office-runtime-pilot-v1',enabled=True,discovery_only=True,evaluated_epoch_allowed=False,research_enabled=False,
        content_addressed_release_root=str(release),state_root=str(repo),config_root=str(config),store_path=str(repo/'data/office/office.db'),
        ledger_path=str(repo/'data/office/ledger/ledger.db'),clock_file=str(repo/'data/clock_offset.json'),guard_file=str(repo/'data/office-control/guard/guard.json'),
        desk_db=str(repo/'data/memecoin_desk.db'),office_cgroup='/sys/fs/cgroup'+group,
        supervisor_identity=dict(crew_uid=os.getuid(),expected_cgroup=group+'/office-supervisor.service',supervisor_script=str(repo/'.runtime/office/current/scripts/office_supervisor.py')),
        transport=dict(path=str(repo/'data/office-control/transport.json'),sha256='0'*64),
        cadence=dict(path=str(config/'cadence.json'),sha256=sha((repo/'config/office/cadence.json').read_bytes())),
        model_manifest=dict(path=str(config/'pilot_models.json'),sha256=sha((repo/'config/office/pilot_models.json').read_bytes())),
        pilot_seed_launcher_config=dict(schema='office-pilot-seed-launcher-v1',files=approvals['source_pins'],
            prompts={slot:dict(text=(repo/f'config/office/prompts/pilot/{slot}.md').read_text(),sha256=pin) for slot,pin in ((name[:-3],pin) for name,pin in approvals['files'].items())}),
        provider_caps=dict(claude=1,codex=1),host_children_max=2,turn_deadline_s=60,max_steps=4,lease_ttl_s=122,poll_s=60)
    launcher['adapters']={name:dict(argv=['/opt/python/bin/python3','/opt/office/scripts/office_model_child.py','--adapter',name,'--request','/opt/scratch/request.json','--output','/opt/scratch/result.json'],ro_binds=[[str(release),'/opt/office']]) for name in ('claude_sdk','codex_exec')}
    launcher.update(supervisor_script=runtime['supervisor_identity']['supervisor_script'],supervisor_cgroup=group+'/office-supervisor.service',office_cgroup=runtime['office_cgroup'])
    transport=json.dumps(dict(crew_uid=os.getuid(),request_root=str(repo/'data/office/launcher/requests'),socket_path=str(repo/'data/office-control/launcher-sock/launcher.sock'))).encode()
    runtime['transport']['sha256']=sha(transport)
    files={'runtime.json':json.dumps(runtime).encode(),'transport.json':transport,
           'launcher.json':json.dumps(launcher).encode(),
           'monitor.json':json.dumps(dict(research_enabled=False,sample_out=str(repo/'data/office-control/health.json'))).encode()}
    files['runtime.sha256']=(sha(files['runtime.json'])+'\n').encode()
    files['launcher.sha256']=(sha(files['launcher.json'])+'\n').encode()
    manifest=dict(schema='office-pilot-activation-v1',approved_by='alpha',main_sha=commit,repo_root=str(repo),release_root=str(release),
        guard_manifest_sha256=sha((repo/'config/office/host_guard_pilot.json').read_bytes()),files={k:sha(v) for k,v in files.items()})
    for name,raw in files.items():(bundle/name).write_bytes(raw)
    def seal():
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        return sha((bundle/'manifest.json').read_bytes())
    dropin=tmp_path/'user-config/systemd/user/office.slice.d/10-office-pilot.conf'
    dropin.parent.mkdir(parents=True)
    dropin.write_text('# Generated from the SHA-pinned office-host-guard-pilot-v2 manifest.\n[Slice]\nCPUAccounting=yes\nMemoryAccounting=yes\nTasksAccounting=yes\nIOAccounting=yes\nCPUQuota=200%\nMemoryMax=4294967296\nTasksMax=256\nIOWeight=10\n')
    dropin.chmod(0o644)
    manifest['office_slice_dropin_sha256']=sha(dropin.read_bytes())
    pin=seal()
    commands=tmp_path/'bin';commands.mkdir();log=tmp_path/'calls.jsonl';state=tmp_path/'systemd.json'
    (commands/'systemctl').write_text('#!'+sys.executable+'\n'+r'''
import json,os,sys
from pathlib import Path
args=sys.argv[1:]; path=Path(os.environ['FIXTURE_STATE'])
with open(os.environ['FIXTURE_LOG'],'a') as f:f.write(json.dumps(args)+'\n')
s=json.loads(path.read_text()) if path.exists() else {}
command=args[1]
if command=='show':
    if len(args)==3 and args[2]=='--property=ControlGroup':
        print(f'ControlGroup=/fixture-user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service');raise SystemExit(0)
    unit=args[2]; unit_file=Path(os.environ['XDG_CONFIG_HOME'])/'systemd/user'/unit
    fragment=os.environ.get('FIXTURE_FOREIGN_FRAGMENT') or (str(unit_file) if unit_file.exists() else '')
    drops=os.environ.get('FIXTURE_DROPIN','') if unit=='office.slice' else ''
    print('FragmentPath='+fragment+'\nDropInPaths='+drops)
    if unit.endswith('.service'):print('MainPID=0\nControlPID=0')
    raise SystemExit(0)
if command=='is-active':print('active' if os.environ.get('FIXTURE_ACTIVE') else 'inactive');raise SystemExit(0 if os.environ.get('FIXTURE_ACTIVE') else 3)
if command=='is-enabled':print('enabled' if s.get(args[2]) else 'disabled');raise SystemExit(0 if s.get(args[2]) else 1)
if command in ('enable','disable'):
    for unit in args[2:]:s[unit]=command=='enable'
    path.write_text(json.dumps(s))
    if command=='enable' and os.environ.get('FIXTURE_FAIL_ENABLE'):raise SystemExit(1)
if command in ('start','restart','stop') or '--now' in args:raise AssertionError('forbidden service action')
''')
    (commands/'systemctl').chmod(0o755)
    (commands/'systemd-analyze').write_text('#!/bin/sh\nexit 0\n');(commands/'systemd-analyze').chmod(0o755)
    child_env=dict(os.environ,PATH=str(commands)+os.pathsep+os.environ['PATH'],XDG_CONFIG_HOME=str(tmp_path/'user-config'),FIXTURE_LOG=str(log),FIXTURE_STATE=str(state),FIXTURE_DROPIN=str(dropin))
    def run(*args,expected_pin=None):
        return subprocess.run(['bash',str(SCRIPT),'--repo',str(repo),'--sha',commit,'--bundle',str(bundle),'--bundle-sha256',expected_pin or seal(),*args],env=child_env,capture_output=True,text=True,timeout=15)
    return dict(repo=repo,bundle=bundle,manifest=manifest,run=run,pin=pin,release=release,log=log,state=state,child_env=child_env,units=tmp_path/'user-config/systemd/user',git=git)


def test_default_is_side_effect_free_dry_run(env):
    result=env['run']()
    assert result.returncode==0,result.stderr+result.stdout
    assert json.loads(result.stdout)['state']=='dry_run'
    assert not (env['repo']/'.runtime').exists()
    assert not (env['units']/'office-supervisor.service').exists()
    assert not env['state'].exists()


def test_bad_bundle_hash_refuses_without_install(env):
    result=env['run']('--apply',expected_pin='0'*64)
    assert result.returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_incomplete_draft_refuses_without_install(env):
    p=env['bundle']/'runtime.json';v=json.loads(p.read_text());v['content_addressed_release_root']=None;p.write_text(json.dumps(v))
    env['manifest']['files']['runtime.json']=sha(p.read_bytes())
    result=env['run']('--apply')
    assert result.returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_apply_is_enable_only_idempotent_and_pending_then_rolls_back(env):
    first=env['run']('--apply')
    assert first.returncode==3,first.stderr+first.stdout
    assert json.loads(first.stdout)['state']=='verification_pending'
    assert env['release'].is_dir()
    assert env['run']('--apply').returncode==3
    commands=[json.loads(line) for line in env['log'].read_text().splitlines()]
    assert not any(word in ('start','restart','stop','--now') for command in commands for word in command)
    assert not (env['repo']/'data/office/office.db').exists()
    rollback=env['run']('--rollback','--apply')
    assert rollback.returncode==0,rollback.stderr+rollback.stdout
    assert not env['release'].exists()
    assert not (env['units']/'office-supervisor.service').exists()
    assert not (env['repo']/'.runtime/office/current').exists()


def test_foreign_file_refuses_untouched(env):
    p=env['repo']/'config/office/runtime.json';p.write_text('foreign')
    result=env['run']('--apply')
    assert result.returncode==2
    assert p.read_text()=='foreign'
    assert not env['release'].exists()


def test_rollback_tamper_refuses_before_changing_other_files(env):
    assert env['run']('--apply').returncode==3
    p=env['units']/'office-monitor.service';p.write_text('foreign')
    result=env['run']('--rollback','--apply')
    assert result.returncode==2
    assert p.read_text()=='foreign'
    assert (env['units']/'office-supervisor.service').exists()


def test_non_main_sha_refuses(env):
    git=env['git'];git('switch','-c','foreign')
    (env['repo']/'foreign').write_text('x');git('add','--','foreign');git('commit','-m','foreign')
    result=env['run']('--sha',git('rev-parse','HEAD'),'--apply')
    assert result.returncode==2 and 'release_not_on_main' in result.stdout
    assert not (env['repo']/'.runtime').exists()


def test_wrong_state_mode_or_symlink_refuses(env,tmp_path):
    state=env['repo']/'data/office';state.mkdir(parents=True,mode=0o755)
    assert env['run']('--apply').returncode==2
    state.rmdir();target=tmp_path/'foreign-state';target.mkdir();state.symlink_to(target,target_is_directory=True)
    assert env['run']('--apply').returncode==2
    assert list(target.iterdir())==[]


def test_active_unit_rollback_refuses_without_mutation(env):
    assert env['run']('--apply').returncode==3
    env['child_env']['FIXTURE_ACTIVE']='1'
    assert env['run']('--rollback','--apply').returncode==2
    assert env['release'].is_dir() and (env['units']/'office-monitor.service').exists()


def test_failed_enable_restores_owned_files_and_enable_state(env):
    env['child_env']['FIXTURE_FAIL_ENABLE']='1'
    result=env['run']('--apply')
    assert result.returncode==2,result.stdout
    assert not env['release'].exists()
    assert not (env['units']/'office-supervisor.service').exists()
    assert not any(json.loads(env['state'].read_text()).values())


def test_rollback_preserves_identical_preexisting_bytes_modes_and_enable_state(env):
    prior=env['units']/'office-monitor.service';prior.parent.mkdir(parents=True,exist_ok=True)
    raw=(env['repo']/'deploy/office/office-monitor.service').read_bytes();prior.write_bytes(raw);prior.chmod(0o644)
    inode=prior.stat().st_ino
    env['state'].write_text(json.dumps({'office-monitor.service':True}))
    assert env['run']('--apply').returncode==3
    assert env['run']('--rollback','--apply').returncode==0
    assert prior.read_bytes()==raw and prior.stat().st_mode&0o777==0o644 and prior.stat().st_ino==inode
    assert json.loads(env['state'].read_text())['office-monitor.service'] is True


def test_repeated_rollback_is_noop(env):
    assert env['run']('--apply').returncode==3
    assert env['run']('--rollback','--apply').returncode==0
    assert env['run']('--rollback','--apply').returncode==0


def test_journal_cannot_target_non_office_units_even_with_recomputed_checksum(env):
    assert env['run']('--apply').returncode==3
    p=env['repo']/'.runtime/office/activation'/env['manifest']['main_sha']/'journal.json'
    value=json.loads(p.read_text());value.pop('journal_sha256');value['enabled']['funded.service']=False
    value['journal_sha256']=sha(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode());p.write_text(json.dumps(value))
    manager=json.loads(env['state'].read_text());manager['funded.service']=True;env['state'].write_text(json.dumps(manager))
    result=env['run']('--rollback','--apply')
    assert result.returncode==2
    assert json.loads(env['state'].read_text())['funded.service'] is True
    assert (env['units']/'office-supervisor.service').exists()


def test_health_pressure_cannot_be_verified_from_boolean_flags_alone(env):
    import time
    assert env['run']('--apply').returncode==3
    root=env['repo']/'data/office-control'
    (root/'health.json').write_text(json.dumps(dict(ts=time.time(),guard_policy_version='office-host-guard-pilot-v3',base_caps_applied=True,roster_valid=True,resources_verified=True,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=.99,io_psi_some_avg10=0.,mem_available_bytes=8*1024**3)))
    (root/'guard/guard.json').write_text(json.dumps(dict(at=time.time(),guard_policy_version='office-host-guard-pilot-v3',state='ready',reason=None)))
    result=env['run']('--verify')
    assert result.returncode==3
    assert 'health:cpu_pressure' in json.loads(result.stdout)['pending']


def test_apply_can_retry_after_clean_rollback(env):
    assert env['run']('--apply').returncode==3
    assert env['run']('--rollback','--apply').returncode==0
    result=env['run']('--apply')
    assert result.returncode==3,result.stdout


def test_guard_pin_checks_raw_git_bytes_including_line_endings(env):
    repo=env['repo'];policy=repo/'config/office/host_guard_pilot.json'
    policy.write_bytes(policy.read_bytes()+b'\r\n')
    git=env['git'];git('add','--','config/office/host_guard_pilot.json');git('commit','-m','raw guard bytes')
    commit=git('rev-parse','HEAD');release=repo/'.runtime/office/releases'/commit
    manifest=env['manifest'];manifest.update(main_sha=commit,release_root=str(release),guard_manifest_sha256=sha(policy.read_bytes()))
    for name in ('runtime.json','launcher.json'):
        p=env['bundle']/name;value=json.loads(p.read_text())
        if name=='runtime.json':
            old=value['content_addressed_release_root']
            value=json.loads(json.dumps(value).replace(old,str(release)))
        else:value=json.loads(json.dumps(value).replace(value['model_roots'][0],str(release)))
        p.write_text(json.dumps(value));manifest['files'][name]=sha(p.read_bytes())
    pin=env['bundle']/'launcher.sha256';pin.write_text(sha((env['bundle']/'launcher.json').read_bytes())+'\n');manifest['files']['launcher.sha256']=sha(pin.read_bytes())
    pin=env['bundle']/'runtime.sha256';pin.write_text(sha((env['bundle']/'runtime.json').read_bytes())+'\n');manifest['files']['runtime.sha256']=sha(pin.read_bytes())
    result=env['run']('--sha',commit)
    assert result.returncode==0,result.stdout


@pytest.mark.parametrize('env',[True],indirect=True)
def test_verify_is_readonly_and_reports_preparation_only(env):
    import time
    assert env['run']('--apply').returncode==3
    root=env['repo']/'data/office-control'
    (root/'health.json').write_text(json.dumps(dict(ts=time.time(),guard_policy_version='office-host-guard-pilot-v3',base_caps_applied=True,roster_valid=True,resources_verified=True,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=0.,io_psi_some_avg10=0.,mem_available_bytes=8*1024**3)))
    (root/'guard/guard.json').write_text(json.dumps(dict(at=time.time(),guard_policy_version='office-host-guard-pilot-v3',state='ready',reason=None)))
    before=env['log'].read_text().splitlines()
    result=env['run']('--verify')
    assert result.returncode==3,result.stdout
    value=json.loads(result.stdout)
    assert 'office_cgroup:missing' in value['pending']
    assert value['state']=='verification_pending' and value['manual_steps']==['canary','seed','supervisor_start']
    new=env['log'].read_text().splitlines()[len(before):]
    assert not any(json.loads(line)[1] in ('enable','disable','daemon-reload','start','restart','stop') for line in new)


def test_missing_preexisting_directory_blocks_rollback(env):
    prior=env['repo']/'data/office-control/auth-private';prior.mkdir(parents=True,mode=0o700)
    prior.parent.chmod(0o700)
    assert env['run']('--apply').returncode==3
    prior.rmdir()
    result=env['run']('--rollback','--apply')
    assert result.returncode==2
    assert (env['units']/'office-supervisor.service').exists()


def test_foreign_loaded_unit_fragment_refuses_without_install(env):
    env['child_env']['FIXTURE_FOREIGN_FRAGMENT']='/unreviewed/office-supervisor.service'
    result=env['run']('--apply')
    assert result.returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_model_mount_may_not_use_future_current_alias(env):
    p=env['bundle']/'launcher.json';value=json.loads(p.read_text());value['model_roots'].append(str(env['repo']/'.runtime/office/current'))
    p.write_text(json.dumps(value));env['manifest']['files']['launcher.json']=sha(p.read_bytes())
    pin=env['bundle']/'launcher.sha256';pin.write_text(sha(p.read_bytes())+'\n');env['manifest']['files']['launcher.sha256']=sha(pin.read_bytes())
    result=env['run']('--apply')
    assert result.returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_only_approved_existing_slice_dropin_is_preserved(env):
    dropin=env['units']/'office.slice.d/10-office-pilot.conf'
    original=dropin.read_bytes()
    env['manifest'].pop('office_slice_dropin_sha256')
    assert env['run']('--apply').returncode==2
    env['manifest']['office_slice_dropin_sha256']=sha(original)
    assert env['run']('--apply').returncode==3
    assert env['run']('--rollback','--apply').returncode==0
    assert dropin.read_bytes()==original


def rewrite_runtime(env,change):
    p=env['bundle']/'runtime.json';value=json.loads(p.read_text());change(value);p.write_text(json.dumps(value))
    env['manifest']['files']['runtime.json']=sha(p.read_bytes())
    pin=env['bundle']/'runtime.sha256';pin.write_text(sha(p.read_bytes())+'\n')
    env['manifest']['files']['runtime.sha256']=sha(pin.read_bytes())


def eval1_runtime(env):
    def change(v):
        eval1=env['repo']/'config/office/prompts/office-paper-eval-1'
        doc=json.loads((eval1/'sha256.json').read_text());files=doc['files']
        v.update(discovery_only=False,evaluated_epoch_allowed=True)
        v['pilot_seed_launcher_config']['files']=doc['source_pins']
        v['model_manifest']=dict(path=str(Path(v['config_root'])/'pilot_models_eval1.json'),sha256=doc['source_pins']['pilot_models_eval1.json'])
        v['pilot_seed_launcher_config']['prompts']={n[:-3]:dict(text=(eval1/n).read_text(),sha256=p) for n,p in files.items()}
    return change


def test_eval1_runtime_is_accepted_only_with_its_five_slot_profile(env):
    rewrite_runtime(env,eval1_runtime(env))
    assert env['run']('--apply').returncode==3


@pytest.mark.parametrize('change',[
    lambda v:v.update(discovery_only=False,evaluated_epoch_allowed=True),           # eval flags on the pilot profile
    lambda v:v.update(discovery_only=False),lambda v:v.update(evaluated_epoch_allowed=True),
])
def test_eval_flags_need_the_eval1_profile_and_both_flags(env,change):
    rewrite_runtime(env,change)
    assert env['run']('--apply').returncode==2
    assert not (env['repo']/'.runtime').exists()


def test_eval1_profile_refused_on_discovery_flags(env):
    def change(v):
        eval1_runtime(env)(v);v.update(discovery_only=True,evaluated_epoch_allowed=False)
    rewrite_runtime(env,change)
    assert env['run']('--apply').returncode==2


@pytest.mark.parametrize('change',[
    lambda v:v.update(version='wrong'),lambda v:v.update(enabled=False),lambda v:v.update(extra=1),
    lambda v:v.update(turn_deadline_s=121),lambda v:v.update(poll_s=True),lambda v:v.update(lease_ttl_s=120),
    lambda v:v.update(max_steps=17),lambda v:v.update(host_children_max=0),lambda v:v.update(provider_caps={'claude':1,'codex':5}),
    lambda v:v.update(ledger_path=v['store_path']),lambda v:v.update(config_root=v['state_root']),
    lambda v:v.update(office_cgroup='/sys/fs/cgroup/wrong/office.slice'),
    lambda v:v['supervisor_identity'].update(crew_uid=-1),
    lambda v:v['cadence'].update(sha256='0'*64),lambda v:v['model_manifest'].update(path='/wrong/pilot_models.json'),
    lambda v:v['pilot_seed_launcher_config']['files'].update(pilot_org_json='0'*64),
    lambda v:v['pilot_seed_launcher_config']['prompts']['head'].update(text='altered'),
])
def test_invalid_runtime_contract_refuses_before_install(env,change):
    rewrite_runtime(env,change)
    result=env['run']('--apply')
    assert result.returncode==2,result.stdout
    assert not (env['repo']/'.runtime').exists()


def test_runtime_pin_is_installed_and_restored(env):
    assert env['run']('--apply').returncode==3
    assert (env['repo']/'config/office/runtime.sha256').read_bytes()==(env['bundle']/'runtime.sha256').read_bytes()
    assert env['run']('--rollback','--apply').returncode==0
    assert not (env['repo']/'config/office/runtime.sha256').exists()


def test_real_supervisor_unit_keeps_only_transport_readonly_after_control_tmpfs():
    import shlex
    text=(SCRIPT.parents[1]/'deploy/office/office-supervisor.service').read_text()
    argv=shlex.split(next(line.split('=',1)[1] for line in text.splitlines() if line.startswith('ExecStart=')))
    control='%h/Desktop/Sniper/data/office-control'
    transport=control+'/transport.json'
    visible=False;readonly=False
    for index,arg in enumerate(argv):
        if arg=='--tmpfs' and (transport==argv[index+1] or transport.startswith(argv[index+1]+'/')):
            visible=False;readonly=False
        if arg in ('--ro-bind','--bind'):
            source,target=argv[index+1:index+3]
            if source==target and (transport==target or transport.startswith(target+'/')):
                visible=True;readonly=arg=='--ro-bind'
            assert not (target==control and index>argv.index('--tmpfs')), 'never expose the whole protected control directory'
    assert visible and readonly, 'runtime transport must survive masking as a read-only file bind'
    assert ['--ro-bind',transport,transport]==argv[argv.index(transport)-1:argv.index(transport)+2]



def test_alias_identity_matches_real_rendered_unit_before_and_after_apply(env):
    import shlex
    assert env['run']().returncode==0
    assert not (env['repo']/'.runtime').exists()
    result=env['run']('--apply')
    assert result.returncode==3,result.stdout
    current=env['repo']/'.runtime/office/current'
    assert current.is_symlink() and current.resolve()==env['release']
    raw=(env['units']/'office-supervisor.service').read_text()
    argv=shlex.split(next(p.split('=',1)[1] for p in raw.splitlines() if p.startswith('ExecStart=')))
    runtime=json.loads((env['bundle']/'runtime.json').read_text())
    launcher=json.loads((env['bundle']/'launcher.json').read_text())
    assert runtime['supervisor_identity']['supervisor_script']==launcher['supervisor_script']==str(current/'scripts/office_supervisor.py')
    assert argv.count(launcher['supervisor_script'])==1
    from sniper.office.child_launcher import validate_peer
    proc=env['repo']/'fake-proc/123';proc.mkdir(parents=True)
    (proc/'cgroup').write_text('0::'+launcher['supervisor_cgroup']+'\n')
    (proc/'cmdline').write_bytes(b'python3\0'+launcher['supervisor_script'].encode()+b'\0')
    (proc/'stat').write_text('123 (fixture) '+' '.join(['0']*19+['77']))
    assert validate_peer(123,os.getuid(),proc_root=proc.parent,expected_cgroup=launcher['supervisor_cgroup'],supervisor_script=runtime['supervisor_identity']['supervisor_script'],expected_uid=os.getuid())=='77'
    (proc/'stat').unlink();(proc/'cmdline').unlink();(proc/'cgroup').unlink();proc.rmdir();proc.parent.rmdir()
    assert env['run']('--apply').returncode==3
    assert env['run']('--rollback','--apply').returncode==0
    assert not current.is_symlink()


@pytest.mark.parametrize('state',['exact','wrong','symlink_parent','non_symlink'])
def test_existing_current_identity_is_exact_or_refused(env,tmp_path,state):
    current=env['repo']/'.runtime/office/current';current.parent.mkdir(parents=True)
    if state=='exact':current.symlink_to(env['release'],target_is_directory=True)
    elif state=='wrong':current.symlink_to(tmp_path/'different-release',target_is_directory=True)
    elif state=='non_symlink':current.mkdir()
    else:
        redirected=tmp_path/'redirected';redirected.mkdir()
        current.parent.rmdir();current.parent.symlink_to(redirected,target_is_directory=True)
        current.symlink_to(env['release'],target_is_directory=True)
    result=env['run']()
    assert result.returncode==(0 if state=='exact' else 2),result.stdout
    assert not env['release'].exists()


@pytest.mark.parametrize('bad',['arbitrary_alias','release_literal','not_supervisor','launcher_mismatch'])
def test_only_exact_unit_argv_and_matching_launcher_identity_are_accepted(env,bad):
    if bad=='launcher_mismatch':
        p=env['bundle']/'launcher.json';v=json.loads(p.read_text());v['supervisor_script']='/other/supervisor.py';p.write_text(json.dumps(v))
        env['manifest']['files']['launcher.json']=sha(p.read_bytes())
        pin=env['bundle']/'launcher.sha256';pin.write_text(sha(p.read_bytes())+'\n');env['manifest']['files']['launcher.sha256']=sha(pin.read_bytes())
    else:
        value={'arbitrary_alias':str(env['repo']/'.runtime/office/other/scripts/office_supervisor.py'),
            'release_literal':str(env['release']/'scripts/office_supervisor.py'),
            'not_supervisor':str(env['repo']/'.runtime/office/current/scripts/office_monitor.py')}[bad]
        rewrite_runtime(env,lambda v:v['supervisor_identity'].update(supervisor_script=value))
    result=env['run']('--apply')
    assert result.returncode==2,result.stdout
    assert not (env['repo']/'.runtime').exists()



def test_real_adapter_descriptors_preserve_payload_bytes_during_prepare(env):
    expected={name:(env['bundle']/name).read_bytes() for name in ('runtime.json','launcher.json','transport.json','monitor.json')}
    assert set(json.loads(expected['launcher.json'])['adapters'])=={'claude_sdk','codex_exec'}
    result=env['run']()
    assert result.returncode==0,result.stdout
    result=env['run']('--apply')
    assert result.returncode==3,result.stdout
    targets={'runtime.json':'config/office/runtime.json','launcher.json':'data/office-control/launcher.json','transport.json':'data/office-control/transport.json','monitor.json':'data/office-control/monitor.json'}
    for name,target in targets.items():assert (env['repo']/target).read_bytes()==expected[name]


def test_unexpected_exception_reports_traceback_location(env):
    path=env['bundle']/'launcher.json';value=json.loads(path.read_text());value['adapters']['claude_sdk']='wrong-shape';path.write_text(json.dumps(value))
    env['manifest']['files']['launcher.json']=sha(path.read_bytes())
    pin=env['bundle']/'launcher.sha256';pin.write_text(sha(path.read_bytes())+'\n');env['manifest']['files']['launcher.sha256']=sha(pin.read_bytes())
    result=env['run']()
    assert result.returncode==2
    assert 'Traceback (most recent call last)' in result.stderr
    assert 'in plan' in result.stderr and 'AttributeError' in result.stderr
    assert not (env['repo']/'.runtime').exists()
