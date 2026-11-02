import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('pilot_watchdog',ROOT/'scripts/office_watchdog.py')
W=importlib.util.module_from_spec(s);s.loader.exec_module(W)

def good(at=100.,**over):
    x=dict(ts=at,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=.01,io_psi_some_avg10=.01,
           mem_available_bytes=8*1024**3,base_caps_applied=True,guard_policy_version="office-host-guard-pilot-v3",roster_valid=True,resources_verified=True)
    x.update(over);return x

def test_manifest_accepts_only_gamma_pinned_pilot(tmp_path):
    manifest=W.load_pilot(ROOT/'config/office/host_guard_pilot.json')
    assert manifest['evaluation_allowed'] is False
    p=tmp_path/'manifest.json';p.write_text(json.dumps(dict(manifest,status='approved')))
    with pytest.raises(ValueError):W.load_pilot(p)
    with pytest.raises((OSError,ValueError)):W.load_pilot(tmp_path/'missing')

def test_heartbeat_needs_two_distinct_two_second_samples():
    g=W.PilotGuard();assert g.check(good(100.,funded_heartbeat_age_s=6.),now=100.) is None
    assert g.check(good(101.,funded_heartbeat_age_s=7.),now=101.) is None
    assert g.check(good(102.,funded_heartbeat_age_s=8.),now=102.)=='funded_heartbeat'

def test_heartbeat_recovery_resets_consecutive_count():
    g=W.PilotGuard();g.check(good(100.,funded_heartbeat_age_s=6.),now=100.)
    assert g.check(good(102.),now=102.) is None
    assert g.check(good(104.,funded_heartbeat_age_s=6.),now=104.) is None

@pytest.mark.parametrize('fields,reason',[
 ({'cpu_psi_some_avg10':.21},'cpu_pressure'),({'io_psi_some_avg10':.21},'io_pressure'),
 ({'mem_available_bytes':4294967295},'memory_available'),({'funded_heartbeat_age_s':None},'telemetry_unknown'),
 ({'cpu_psi_some_avg10':float('nan')},'telemetry_unknown'),({'telemetry_error':True},'telemetry_unknown'),
 ({'base_caps_applied':False},'caps_unapplied'),({'roster_valid':False},'roster_invalid'),
 ({'resources_verified':False},'resource_identity_unverified')])
def test_pilot_hard_failures(fields,reason):
    assert W.PilotGuard().check(good(**fields),now=100.)==reason

def test_stale_future_and_duplicate_samples_are_not_new_health():
    g=W.PilotGuard();assert g.check(good(),now=100.) is None
    assert g.check(good(),now=101.) is None
    assert g.check(good(),now=106.)=='telemetry_stale'
    assert W.PilotGuard().check(good(110.),now=100.)=='telemetry_future'

def test_resume_requires_900_continuous_healthy_seconds():
    g=W.PilotGuard()
    for t in range(100,1000,2):
        assert g.check(good(float(t)),now=float(t)) is None
    assert not g.resume_eligible
    assert g.check(good(1000.),now=1000.) is None and g.resume_eligible
    assert g.check(good(1002.,telemetry_error=True),now=1002.)=='telemetry_unknown'
    assert not g.resume_eligible

def test_applied_caps_are_kernel_values_not_config_claims(tmp_path):
    c=tmp_path/'office.slice';c.mkdir()
    for name,value in {'cpu.max':'200000 100000','memory.max':'4294967296','pids.max':'256','io.weight':'default 10'}.items():
        (c/name).write_text(value)
    assert W.caps_applied(c)
    (c/'cpu.max').write_text('max 100000');assert not W.caps_applied(c)
    (c/'cpu.max').unlink();assert not W.caps_applied(c)

def test_monitor_pilot_reads_required_sources_without_ledger(tmp_path,monkeypatch):
    import sys
    sys.path.insert(0,str(ROOT/'scripts'))
    import office_host_monitor as M
    beat=tmp_path/'beat';beat.write_text(json.dumps({'ts':99.,'auth_ok':True}))
    cg=tmp_path/'office.slice';cg.mkdir()
    for name,value in {'cpu.max':'200000 100000','memory.max':'4294967296','pids.max':'256','io.weight':'default 10'}.items():
        (cg/name).write_text(value)
    cfg=dict(funded_heartbeat=str(beat),office_cgroup=str(cg),research_enabled=False,roster_seats=['head','scout','judge_claude','judge_gpt'])
    monkeypatch.setattr(M,'pinned_roster',lambda config:True)
    monkeypatch.setattr(M,'pressure',lambda kind:.01)
    monkeypatch.setattr(M,'_available',lambda:8*1024**3)
    got=M.health_sample(cfg,now_host=100.)
    assert W.PilotGuard().check(got,now=100.) is None
    assert got['funded_heartbeat_age_s']==1.
    assert 'caps_applied' not in got and got['base_caps_applied'] is True
    beat.unlink()
    assert W.PilotGuard().check(M.health_sample(cfg,now_host=101.),now=101.)=='telemetry_unknown'

def test_initialization_checks_health_and_cannot_bypass_paused_recovery(tmp_path):
    latch=tmp_path/'guard.json'
    W.start_or_resume(latch,good(),now=100.)
    assert W.external_guard(latch,now=100.) is None
    W.pause(latch,reason='cpu_pressure',now=101.,terminate=lambda _:None)
    with pytest.raises(ValueError):W.start_or_resume(latch,good(102.),now=102.)
    latch.write_text(json.dumps(dict(state='paused',reason='cpu_pressure',at=1001.,
                                    healthy_since=100.,resume_eligible=True)))
    W.start_or_resume(latch,good(1002.),now=1002.)
    assert W.external_guard(latch,now=1002.) is None
    assert json.loads(latch.read_text())['previous_pause_reason']=='cpu_pressure'

def test_control_authority_is_outside_supervisor_writable_tree():
    text=(ROOT/'deploy/office/office-supervisor.service').read_text()
    assert '--ro-bind / /' in text
    assert '--bind %h/Desktop/Sniper/data/office-control ' not in text
    assert '--guard %h/Desktop/Sniper/data/office-control/guard/guard.json' in text
    assert '--ro-bind %h/Desktop/Sniper/data/office-control/guard %h/Desktop/Sniper/data/office-control/guard' in text
    assert 'ReadWritePaths=%h/Desktop/Sniper/data/office-control' not in text


def test_free_roster_claim_cannot_pass_monitor(tmp_path,monkeypatch):
    import sys
    sys.path.insert(0,str(ROOT/'scripts'))
    import office_host_monitor as M
    assert M.pinned_roster({'roster_seats':['head','scout','judge_claude','judge_gpt']}) is False
    p=tmp_path/'roster';p.write_text('{}')
    assert M.pinned_roster({'roster_file':str(p)}) is False


def test_only_exact_reviewed_startup_roster_is_accepted(tmp_path):
    import sys
    sys.path.insert(0,str(ROOT/'scripts'))
    import office_host_monitor as M
    p=tmp_path/'roster.json'
    p.write_bytes(b'{\n  "version": "pilot-org-1",\n  "pilot": true,\n  "shared_seats": [\n    {"role": "Head", "count": 1, "family": "claude", "tier": "opus"}\n  ],\n  "deterministic_seats": ["Risk", "Trader", "Postmortem"],\n  "pod_template": {\n    "scouts": [{"lane": "intake_lists"}],\n    "scouts_per_pod": 1,\n    "analysts": [],\n    "analysts_per_pod": 0,\n    "judges_per_pod": 2,\n    "judge_families": ["claude", "gpt"]\n  },\n  "pods": 1\n}\n')
    assert M.pinned_roster({'roster_file':str(p)})
    p.write_bytes(p.read_bytes()+b' ')
    assert not M.pinned_roster({'roster_file':str(p)})

def test_user_units_use_explicit_bwrap_filesystem_enforcement():
    for name in ['office-supervisor.service','office-watchdog.service','office-monitor.service']:
        text=(ROOT/'deploy/office'/name).read_text()
        assert '/usr/bin/bwrap --unshare-user --unshare-pid' in text
        assert '--ro-bind / /' in text and '--cap-drop ALL' in text
        assert 'NoNewPrivileges=no' in text
    supervisor=(ROOT/'deploy/office/office-supervisor.service').read_text()
    assert '--bind %h/Desktop/Sniper/data/office-control' not in supervisor


def test_actual_user_unit_cannot_write_control_and_payload_has_no_capabilities(tmp_path):
    import os,shutil,subprocess,uuid,sys
    if not shutil.which('bwrap') or not shutil.which('systemd-run'):
        pytest.skip('Linux containment dependencies absent')
    writable=tmp_path/'ledger';writable.mkdir()
    control=tmp_path/'control';control.mkdir();(control/'guard').write_text('canary')
    code="""from pathlib import Path
import sys
rw,control=map(Path,sys.argv[1:])
(rw/'ok').write_text('ok')
assert (control/'guard').read_text()=='canary'
for action in [lambda:(control/'guard').write_text('forge'),lambda:(control/'new').write_text('forge'),lambda:(control/'guard').unlink()]:
 try:action()
 except OSError:pass
 else:raise AssertionError('control writable')
status=Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'CapEff:\t0000000000000000' in status
"""
    cmd=['systemd-run','--user','--wait','--pipe','--collect','--unit=delta-guard-check-'+uuid.uuid4().hex,
         '--property=NoNewPrivileges=no','--property=MemoryMax=128M','--property=CPUQuota=5%',
         '/usr/bin/bwrap','--unshare-user','--unshare-pid','--die-with-parent','--new-session',
         '--ro-bind','/','/','--proc','/proc','--dev','/dev','--tmpfs','/tmp',
         '--ro-bind',str(control),str(control),'--bind',str(writable),str(writable),'--cap-drop','ALL',sys.executable,'-c',code,str(writable),str(control)]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert (control/'guard').read_text()=='canary'

def test_supervisor_uses_approved_separate_ledger_directory():
    text=(ROOT/'deploy/office/office-supervisor.service').read_text()
    assert '--ledger %h/Desktop/Sniper/data/office/ledger/ledger.db' in text


@pytest.mark.parametrize('value,verified',[('missing',False),(None,False),(0,False),('false',False),(True,False),(False,True)])
def test_monitor_resource_identity_requires_explicit_false(tmp_path,monkeypatch,value,verified):
    import sys
    sys.path.insert(0,str(ROOT/'scripts'))
    import office_host_monitor as M
    beat=tmp_path/'beat';beat.write_text(json.dumps({'ts':99.,'auth_ok':True}))
    cfg=dict(funded_heartbeat=str(beat),office_cgroup='fixture')
    if value!='missing':cfg['research_enabled']=value
    monkeypatch.setattr(M,'pinned_roster',lambda _:True)
    monkeypatch.setattr(M,'pressure',lambda _:.01)
    monkeypatch.setattr(M,'_available',lambda:8*1024**3)
    monkeypatch.setattr('office_watchdog.caps_applied',lambda _:True)
    sample=M.health_sample(cfg,now_host=100.)
    assert sample['resources_verified']is verified
    assert W.PilotGuard().check(sample,now=100.)==(None if verified else'resource_identity_unverified')
