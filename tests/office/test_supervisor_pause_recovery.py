import importlib.util,json
from pathlib import Path
import pytest
from scripts.office_supervisor import main
from sniper.office.runtime import RuntimeRefused
ROOT=Path(__file__).resolve().parents[2]

def test_typed_startup_refusal_returns_nonrestart_code(capsys):
 def factory(**kw):raise RuntimeRefused('funded_heartbeat')
 assert main(['--once'],runtime_factory=factory)==75
 assert json.loads(capsys.readouterr().out)=={'state':'paused','reason':'funded_heartbeat'}

@pytest.mark.parametrize('payload',[{'state':'paused','reason':'funded_heartbeat','at':100.}, {'state':'ready','reason':None,'at':90.}, [], 'broken'])
def test_condition_refuses_unready_or_malformed_latch(tmp_path,payload):
 spec=importlib.util.spec_from_file_location('condition',ROOT/'scripts/office_supervisor_wake.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 path=tmp_path/'guard.json';path.write_text(json.dumps(payload)if payload!='broken'else payload)
 assert module.ready(path,now=100.)is False

def test_condition_accepts_only_fresh_pilot_ready(tmp_path):
 spec=importlib.util.spec_from_file_location('condition',ROOT/'scripts/office_supervisor_wake.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 p=tmp_path/'guard.json';p.write_text(json.dumps(dict(state='ready',reason=None,at=100.,guard_policy_version='office-host-guard-pilot-v3')))
 assert module.ready(p,now=101.)is True

def test_real_units_skip_pause_and_wake_from_host():
 unit=(ROOT/'deploy/office/office-supervisor.service').read_text()
 assert 'ExecCondition='not in unit
 assert 'RestartPreventExitStatus=75'in unit
 wake=(ROOT/'deploy/office/office-supervisor-wake.service').read_text()
 timer=(ROOT/'deploy/office/office-supervisor-wake.timer').read_text()
 assert 'office_supervisor_wake.py --guard'in wake
 assert 'bwrap'not in wake and 'Slice=app.slice'in wake
 assert 'OnUnitActiveSec=60s'in timer and 'WantedBy=timers.target'in timer


def test_host_condition_cli_never_builds_runtime_and_skips_missing_latch(tmp_path):
 import subprocess,sys,time
 script=ROOT/'scripts/office_supervisor_wake.py';p=tmp_path/'guard.json'
 assert subprocess.run([sys.executable,str(script),'--guard',str(p)],timeout=5).returncode==0
 p.write_text(json.dumps(dict(state='paused',reason='funded_heartbeat',at=time.time(),guard_policy_version='office-host-guard-pilot-v3')))
 assert subprocess.run([sys.executable,str(script),'--guard',str(p)],timeout=5).returncode==0


@pytest.mark.parametrize('state',['missing','paused','malformed','stale','ready'])
def test_app_slice_wake_calls_manager_only_for_fresh_pilot_ready(tmp_path,monkeypatch,state):
 import time
 from types import SimpleNamespace
 spec=importlib.util.spec_from_file_location('wake',ROOT/'scripts/office_supervisor_wake.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 p=tmp_path/'guard.json'
 if state=='malformed':p.write_text('[]')
 elif state!='missing':p.write_text(json.dumps(dict(state='paused'if state=='paused'else 'ready',reason='funded_heartbeat'if state=='paused'else None,at=time.time()-(10 if state=='stale'else 0),guard_policy_version='office-host-guard-pilot-v3')))
 calls=[]
 def run(argv,**kw):calls.append((argv,kw));return SimpleNamespace(returncode=0)
 monkeypatch.setattr(module,'subprocess',SimpleNamespace(run=run))
 assert module.main(['--guard',str(p)])==0
 assert len(calls)==(1 if state=='ready'else 0)
 if calls:assert calls[0]==(['/usr/bin/systemctl','--user','start','office-supervisor.service'],dict(timeout=10,check=False))


@pytest.mark.parametrize('error',[OSError('fixture missing manager'),TimeoutError('fixture')])
def test_wake_manager_error_is_loud_without_restart_loop(tmp_path,monkeypatch,error,capsys):
 import time,subprocess
 from types import SimpleNamespace
 spec=importlib.util.spec_from_file_location('wake',ROOT/'scripts/office_supervisor_wake.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 p=tmp_path/'guard.json';p.write_text(json.dumps(dict(state='ready',reason=None,at=time.time(),guard_policy_version='office-host-guard-pilot-v3')))
 def run(*a,**k):
  if isinstance(error,TimeoutError):raise subprocess.TimeoutExpired('fixture',10)
  raise error
 monkeypatch.setattr(module,'subprocess',SimpleNamespace(run=run,TimeoutExpired=subprocess.TimeoutExpired))
 assert module.main(['--guard',str(p)])==2
 assert json.loads(capsys.readouterr().err)['state']=='wake_failed'
