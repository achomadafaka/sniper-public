"""Guard loop and actual rendered bwrap unit, never a real cgroup or user service."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('guard_recovery',ROOT/'scripts/office_watchdog.py')
W=importlib.util.module_from_spec(spec);spec.loader.exec_module(W)


def good(at=100.,**over):
    return dict(ts=at,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=.01,io_psi_some_avg10=.01,
                mem_available_bytes=8*1024**3,base_caps_applied=True,roster_valid=True,
                resources_verified=True,guard_policy_version='office-host-guard-pilot-v3',**over)


class EndLoop(BaseException):pass


def run_loop(tmp_path,monkeypatch,*,prior=None,kill=None,times=(100.,),sample=None,kill_control=True,populations=None):
    latch=tmp_path/'guard.json';health=tmp_path/'health.json';group=tmp_path/'office.slice';group.mkdir()
    if kill_control:(group/'cgroup.kill').write_text('untouched')
    real_terminate=W.terminate_slice
    def population(index):
        value=1 if populations is None else populations[index]
        events=group/'cgroup.events'
        if value is None:events.unlink(missing_ok=True)
        else:events.write_text('populated '+str(value)+'\n')
    population(0)
    if prior is not None:latch.write_text(prior if isinstance(prior,str)else json.dumps(prior))
    at=[times[0]];health.write_text(json.dumps(sample or good(at[0])))
    def sleep(_):
        index=times.index(at[0])+1
        if index>=len(times):raise EndLoop
        at[0]=times[index];health.write_text(json.dumps(good(at[0])));population(index)
    monkeypatch.setattr(W,'time',SimpleNamespace(time=lambda:at[0],monotonic=lambda:at[0],sleep=sleep))
    calls=[]
    def terminate(name,**kw):
        calls.append((name,at[0],json.loads(latch.read_text())))
        if not kill_control:real_terminate(name,**kw)
        if kill:kill(len(calls))
    monkeypatch.setattr(W,'terminate_slice',terminate)
    argv=['--sample',str(health),'--manifest',str(ROOT/'config/office/host_guard_pilot.json'),'--latch',str(latch)]
    argv+=['--cgroup',str(group)]
    with pytest.raises(EndLoop):W.main(argv)
    return json.loads(latch.read_text()),calls


def test_absent_latch_with_healthy_sample_becomes_ready_without_kill(tmp_path,monkeypatch):
    latch,calls=run_loop(tmp_path,monkeypatch)
    assert latch['state']=='ready'and latch['reason']is None and calls==[]


@pytest.mark.parametrize('prior',[None,'broken','[]'])
def test_unhealthy_or_malformed_start_stays_paused(tmp_path,monkeypatch,prior):
    sample=good();sample['resources_verified']=False
    latch,calls=run_loop(tmp_path,monkeypatch,prior=prior,sample=sample)
    assert latch['state']=='paused'and latch['reason']=='resource_identity_unverified'
    assert calls[0][2]['state']=='paused' # durable before termination


def test_failed_kill_is_loud_paused_and_retried_without_process_exit(tmp_path,monkeypatch,capsys):
    def fail_first(n):
        if n==1:raise PermissionError(13,'denied')
    latch,calls=run_loop(tmp_path,monkeypatch,prior=dict(state='paused',reason='cpu_pressure',at=99.),
                         kill=fail_first,times=(100.,101.,105.))
    assert [(c[0],c[1])for c in calls]==[('office.slice',100.),('office.slice',105.)]
    assert latch['state']=='paused'and latch['reason']=='cpu_pressure'
    assert latch['termination']['state']=='sent'
    assert calls[1][2]['termination']['state']=='failed'
    assert 'termination_failed' in capsys.readouterr().err


def test_existing_pause_never_auto_clears_on_good_health(tmp_path,monkeypatch):
    latch,calls=run_loop(tmp_path,monkeypatch,prior=dict(state='paused',reason='cpu_pressure',at=99.))
    assert latch['state']=='paused'and latch['reason']=='cpu_pressure'and len(calls)==1


def test_termination_writes_only_office_kill_control_without_manager(tmp_path,monkeypatch):
    group=tmp_path/'office.slice';group.mkdir();control=group/'cgroup.kill';control.write_text('')
    def forbidden(*a,**kw):raise AssertionError('manager call from watchdog')
    monkeypatch.setattr(W,'subprocess',SimpleNamespace(run=forbidden),raising=False)
    W.terminate_slice('office.slice',cgroup=group)
    assert control.read_text()=='1\n'
    with pytest.raises(ValueError):W.terminate_slice('funded.service',cgroup=group)
    other=tmp_path/'funded.service';other.mkdir();(other/'cgroup.kill').write_text('')
    with pytest.raises(ValueError):W.terminate_slice('office.slice',cgroup=other)
    assert (other/'cgroup.kill').read_text()==''


def test_symlink_kill_target_refuses(tmp_path):
    group=tmp_path/'office.slice';group.mkdir();outside=tmp_path/'outside';outside.write_text('unchanged')
    (group/'cgroup.kill').symlink_to(outside)
    with pytest.raises((ValueError,OSError)):W.terminate_slice('office.slice',cgroup=group)
    assert outside.read_text()=='unchanged'


def test_actual_watchdog_unit_can_write_only_fixture_kill_file_under_userns(tmp_path):
    text=(ROOT/'deploy/office/office-watchdog.service').read_text().replace('%h',str(Path.home())).replace('%U',str(os.getuid()))
    argv=shlex.split(next(l.split('=',1)[1]for l in text.splitlines()if l.startswith('ExecStart=')))
    assert argv[argv.index('--cgroup')+1]==f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/office.slice'
    assert 'Slice=app.slice' in text
    argv=argv[:argv.index('/usr/bin/env')]
    # The legacy slice may not exist on this host; scaffold only the test's private cgroup mount.
    at=argv.index('--bind-try');argv[at:at]=['--tmpfs','/sys/fs/cgroup']
    target=f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/office.slice/cgroup.kill'
    matches=[i for i in range(len(argv))if argv[i:i+3]==['--bind-try',target,target]]
    assert len(matches)==1
    control=tmp_path/'kill-fixture';control.write_text('fixture-only')
    neighbor=tmp_path/'neighbor';neighbor.write_text('read-only')
    # Substitute every writable source before executing; never bind the real kill file writable.
    for i,arg in enumerate(argv):
        if arg in ('--bind','--bind-try'):
            if argv[i+1]==target:argv[i+1]=str(control)
            else:
                fixture=tmp_path/'control';fixture.mkdir(exist_ok=True);argv[i+1]=str(fixture)
    fixture=tmp_path/'control'
    (fixture/'fixture-owned').write_text('fixture-control-only')
    (fixture/'health.json').write_text(json.dumps(good()))
    control_target=str(Path.home()/'Desktop/Sniper/data/office-control')
    events=tmp_path/'events-fixture';events.write_text('populated 1\n')
    argv+=['--ro-bind',str(events),str(Path(target).with_name('cgroup.events')),'--remount-ro','/sys/fs/cgroup']
    code="""import importlib.util,sys
from pathlib import Path
inode,target,neighbor,script,control_dir,events_inode=sys.argv[1:]
assert Path(target).read_text()=='fixture-only'
assert Path(target).stat().st_ino==int(inode)
assert Path(target).with_name('cgroup.events').stat().st_ino==int(events_inode)
try:Path(target).with_name('foreign-write').write_text('forbidden')
except OSError:pass
else:raise AssertionError('fixture cgroup parent must be read-only')
s=importlib.util.spec_from_file_location('w',script);w=importlib.util.module_from_spec(s);s.loader.exec_module(w)
w.terminate_slice('office.slice',cgroup=Path(target).parent)
import os
assert os.statvfs('/').f_flag & os.ST_RDONLY
control_dir=Path(control_dir)
assert (control_dir/'fixture-owned').read_text()=='fixture-control-only'
from types import SimpleNamespace
class EndLoop(BaseException):pass
def stop(_):raise EndLoop
w.time=SimpleNamespace(time=lambda:100.,monotonic=lambda:100.,sleep=stop)
before=Path(target).read_bytes()
try:w.main(['--sample',str(control_dir/'health.json'),'--manifest',str(Path(script).parents[1]/'config/office/host_guard_pilot.json'),'--latch',str(control_dir/'guard.json'),'--cgroup',str(Path(target).parent)])
except EndLoop:pass
else:raise AssertionError('loop did not run')
import json
assert json.loads((control_dir/'guard.json').read_text())['state']=='ready'
assert Path(target).read_bytes()==before
(control_dir/'guard.json').write_text(json.dumps({'state':'paused','reason':'fixture_pause','at':99.}))
at=[100.];writes=[];real_terminate=w.terminate_slice
def terminate(name,**kw):
 real_terminate(name,**kw);writes.append(at[0])
def advance(_):
 if at[0]==105.:raise EndLoop
 at[0]=101. if at[0]==100. else 105.
 sample=json.loads((control_dir/'health.json').read_text());sample['ts']=at[0]
 (control_dir/'health.json').write_text(json.dumps(sample))
w.terminate_slice=terminate
w.time=SimpleNamespace(time=lambda:at[0],monotonic=lambda:at[0],sleep=advance)
try:w.main(['--sample',str(control_dir/'health.json'),'--manifest',str(Path(script).parents[1]/'config/office/host_guard_pilot.json'),'--latch',str(control_dir/'guard.json'),'--cgroup',str(Path(target).parent)])
except EndLoop:pass
assert writes==[100.,105.]
assert json.loads((control_dir/'guard.json').read_text())['state']=='paused'
print('fixture-kill-ok')
"""
    result=subprocess.run(argv+[sys.executable,'-c',code,str(control.stat().st_ino),target,str(neighbor),str(ROOT/'scripts/office_watchdog.py'),control_target,str(events.stat().st_ino)],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert result.stdout=='fixture-kill-ok\n'and control.read_text().startswith('1\n')and neighbor.read_text()=='read-only'


def test_missing_kill_control_pauses_loudly_without_loop_exit(tmp_path,monkeypatch,capsys):
    latch,calls=run_loop(tmp_path,monkeypatch,kill_control=False)
    assert latch['state']=='paused'and latch['reason']=='kill_control_unavailable'
    assert latch['termination']['state']=='failed'and len(calls)==1
    assert 'termination_failed' in capsys.readouterr().err


def test_explicit_initialize_cli_does_not_require_kill_authority(tmp_path,monkeypatch):
    health=tmp_path/'health.json';health.write_text(json.dumps(good()))
    latch=tmp_path/'guard.json'
    monkeypatch.setattr(W,'time',SimpleNamespace(time=lambda:100.))
    assert W.main(['--sample',str(health),'--manifest',str(ROOT/'config/office/host_guard_pilot.json'),'--latch',str(latch),'--initialize'])==0
    assert W.external_guard(latch,now=100.)is None


def test_new_arrival_while_paused_is_killed_again_with_rate_bound(tmp_path,monkeypatch,capsys):
    latch,calls=run_loop(tmp_path,monkeypatch,prior=dict(state='paused',reason='cpu_pressure',at=99.),
                         times=(100.,101.,105.),populations=(1,0,1))
    assert [c[1]for c in calls]==[100.,105.]
    assert latch['state']=='paused'and latch['population']['state']=='populated'
    assert capsys.readouterr().err.count('termination_sent')==2


def test_empty_paused_slice_has_no_kill_attempt(tmp_path,monkeypatch):
    latch,calls=run_loop(tmp_path,monkeypatch,prior=dict(state='paused',reason='cpu_pressure',at=99.),
                         times=(100.,105.),populations=(0,0))
    assert calls==[]and latch['state']=='paused'and latch['population']['state']=='empty'


def test_unknown_population_is_not_empty_or_a_loop_exit(tmp_path,monkeypatch,capsys):
    latch,calls=run_loop(tmp_path,monkeypatch,prior=dict(state='paused',reason='cpu_pressure',at=99.),
                         times=(100.,101.,105.),populations=(None,None,None))
    assert [c[1]for c in calls]==[100.,105.]
    assert latch['population']['state']=='unknown'and latch['state']=='paused'
    assert 'population_unknown' in capsys.readouterr().err


@pytest.mark.parametrize('contents',['','populated 2\n','populated 0\npopulated 1\n','frozen 0\n','populated 0\n'+' '*4096])
def test_bad_population_proof_refuses(contents,tmp_path):
    group=tmp_path/'office.slice';group.mkdir();(group/'cgroup.events').write_text(contents)
    with pytest.raises(ValueError):W.slice_populated(group)


@pytest.mark.parametrize('reason',['funded_heartbeat','cpu_pressure','io_pressure','memory_available'])
def test_self_healing_pause_auto_resumes_after_900_continuous_healthy_seconds(tmp_path,monkeypatch,reason):
    prior=dict(state='paused',reason=reason,at=99.,guard_policy_version='office-host-guard-pilot-v3')
    latch,calls=run_loop(tmp_path,monkeypatch,prior=prior,times=tuple(float(t) for t in range(100,1001,5)),populations=(0,)*181)
    assert latch['state']=='ready'and latch['previous_pause_reason']==reason
    assert calls==[]


@pytest.mark.parametrize('reason',['guard_unknown','manual','telemetry_unknown','kill_control_unavailable'])
def test_manual_or_unknown_pause_never_auto_resumes(tmp_path,monkeypatch,reason):
    prior=dict(state='paused',reason=reason,at=99.,guard_policy_version='office-host-guard-pilot-v3')
    latch,calls=run_loop(tmp_path,monkeypatch,prior=prior,times=tuple(float(t) for t in range(100,1001,5)),populations=(0,)*181)
    assert latch['state']=='paused'and latch['resume_eligible']is True


def test_self_healing_pause_cannot_resume_before_900_seconds(tmp_path,monkeypatch):
    prior=dict(state='paused',reason='funded_heartbeat',at=99.,guard_policy_version='office-host-guard-pilot-v3')
    latch,_=run_loop(tmp_path,monkeypatch,prior=prior,times=tuple(float(t) for t in range(100,1000,5)),populations=(0,)*180)
    assert latch['state']=='paused'and latch['resume_eligible']is False


@pytest.mark.parametrize('prior',['broken','[]',{'state':'paused','reason':'funded_heartbeat','at':99.}, {'state':'paused','reason':'funded_heartbeat','at':-1.,'guard_policy_version':'office-host-guard-pilot-v3'}])
def test_malformed_pause_never_becomes_auto_recoverable(tmp_path,monkeypatch,prior):
    latch,_=run_loop(tmp_path,monkeypatch,prior=prior,times=tuple(float(t) for t in range(100,1001,5)),populations=(0,)*181)
    assert latch['state']=='paused'


def test_missing_telemetry_resets_recovery_clock(tmp_path,monkeypatch):
    prior=dict(state='paused',reason='funded_heartbeat',at=99.,guard_policy_version='office-host-guard-pilot-v3')
    times=tuple(float(t) for t in range(100,551,5))+tuple(float(t)for t in range(560,1001,5))
    latch,_=run_loop(tmp_path,monkeypatch,prior=prior,times=times,populations=(0,)*len(times))
    assert latch['state']=='paused'and latch['resume_eligible']is False and latch['healthy_since']==565.


def test_failed_kill_does_not_auto_resume_on_healthy_telemetry_alone(tmp_path,monkeypatch):
    def denied(n):raise PermissionError(13,'fixture denied')
    prior=dict(state='paused',reason='funded_heartbeat',at=99.,guard_policy_version='office-host-guard-pilot-v3')
    latch,_=run_loop(tmp_path,monkeypatch,prior=prior,kill=denied,times=tuple(float(t) for t in range(100,1001,5)))
    assert latch['state']=='paused'and latch['termination']['state']=='failed'
