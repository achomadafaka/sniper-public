"""Real rendered launcher boundary, fake credentials and uniquely owned transient units only."""
import json
import os
from pathlib import Path
import shlex
import socket
import select
import sys
import time
import subprocess
import tempfile
import uuid

import pytest
from office.fixture_units import fixture_slice, isolated_command

UNIT=Path(__file__).resolve().parents[2]/'deploy/office/office-child-launcher.service'


def service(home):
    section=None;properties=[];command=None
    for line in UNIT.read_text().splitlines():
        if line.startswith('['):section=line
        if section!='[Service]' or '=' not in line or line.startswith('#'):continue
        key,value=line.split('=',1)
        value=value.replace('%h',str(home)).replace('%U',str(os.getuid()))
        if key=='ExecStart':command=shlex.split(value)
        elif key in ('ExecStartPre','ExecStartPost'):raise AssertionError('fixture must not run service hooks')
        elif key!='Restart':properties.append((key,value))
    assert command is not None
    return properties,command


def test_launcher_unit_preserves_private_network_and_narrow_credential_mounts():
    properties,args=service(Path('/home/fixture'))
    assert args[0]=='/usr/bin/bwrap' and '--unshare-user' in args and '--unshare-net' in args
    assert ['--ro-bind','/','/'] in [args[i:i+3] for i in range(len(args))]
    assert ['--ro-bind','/home/fixture/.claude','/home/fixture/.claude'] in [args[i:i+3] for i in range(len(args))]
    assert ['--bind','/home/fixture/.codex','/home/fixture/.codex'] in [args[i:i+3] for i in range(len(args))]
    lock='/home/fixture/Desktop/Sniper/data/desk/codex_call.lock'
    assert ['--bind',lock,lock] in [args[i:i+3] for i in range(len(args))]
    assert 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(os.getuid())+'/bus' in args


PROBE=r'''
import json,os,socket,subprocess,sys,time
from pathlib import Path
rid,root,source,port=sys.argv[1],Path(sys.argv[2]),sys.argv[3],int(sys.argv[4]);child='office-model-'+rid
udp_port,host_netns,sentinel=int(sys.argv[5]),sys.argv[6],Path(sys.argv[7])
expected_lock=json.loads(sys.argv[8]);private_slice=sys.argv[9]
assert private_slice!='office.slice' and '-' not in private_slice
sys.path.insert(0,str(Path(source)/'tests'))
from office.fixture_units import isolated_command
sys.path.insert(0,source)
from sniper.office.child_launcher import Request,child_command,extract_io_proof
from sniper.office.launcher_bindings import unit_properties
from scripts.memecoin_desk_checker import codex_slot,CodexBusy
lock=root/'Desktop/Sniper/data/desk/codex_call.lock'
info=lock.stat();assert [info.st_dev,info.st_ino]==expected_lock
try:
    with codex_slot(timeout_s=0,path=lock):raise AssertionError('office raced the host slot owner')
except CodexBusy:pass
control=root/'Desktop/Sniper/data/office-control'
(control/'lock-busy').write_text('busy')
deadline=time.monotonic()+6
while not (control/'lock-released').exists() and time.monotonic()<deadline:time.sleep(.02)
assert (control/'lock-released').exists()
with codex_slot(timeout_s=0,path=lock):(control/'lock-acquired').write_text('after-host')
scratch=root/'Desktop/Sniper/data/office-control/children'/rid;scratch.mkdir(parents=True)
marker=scratch/'child-proof.json';guard=root/'Desktop/Sniper/data/office-control/guard/guard.json'
guard.parent.mkdir(parents=True);guard.write_text(json.dumps(dict(state='ready',reason=None,guard_policy_version='office-host-guard-pilot-v3',at=time.time())))
code="import json,os,signal,socket,sys,time;from pathlib import Path;assert not Path("+repr(str(root/'.codex/auth.json'))+").exists();assert not Path('/run/user/"+str(os.getuid())+"/bus').exists();signal.signal(signal.SIGTERM,lambda *a:sys.exit(0));s=socket.create_connection(('127.0.0.1',"+str(port)+"),timeout=2);s.close();u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);u.sendto(b'child-fixture',('127.0.0.1',"+str(udp_port)+"));u.close();Path('/opt/scratch/child-proof.json').write_text(json.dumps(dict(uid=os.getuid(),cgroup=Path('/proc/self/cgroup').read_text(),network_outside_launcher=True,netns=os.readlink('/proc/self/ns/net'))));time.sleep(10)"
config=dict(guard_file=str(guard),adapters={'codex_exec':dict(argv=['/usr/bin/python3','-I','-B','-c',code],ro_binds=[])})
now=time.time();request=Request('codex_exec',rid,scratch/'request.json',now+12,b'{}','fixture')
argv=isolated_command(child_command(config,request,scratch=scratch,now=now),slice_name=private_slice)
bwrap=argv.index('/usr/bin/bwrap')
argv[bwrap:bwrap]=['--property=MemoryMax=64M','--property=CPUQuota=10%','--property=TasksMax=16']
waiter=None
try:
    waiter=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline=time.monotonic()+6
    while not marker.exists() and time.monotonic()<deadline and waiter.poll() is None:time.sleep(.05)
    if not marker.exists():
        out,err=waiter.communicate(timeout=8)
        raise RuntimeError(json.dumps(dict(stage='actual_child_command',returncode=waiter.returncode,stdout=out,stderr=err)))
    proof=json.loads(marker.read_text())
    assert proof['uid']==os.getuid() and proof['network_outside_launcher'] is True
    assert '/office.slice/' not in proof['cgroup']
    assert '/'+private_slice+'/'+child+'.service' in proof['cgroup']
    props=unit_properties(child+'.service');pid=int(props['MainPID'])
    assert props['ActiveState']=='active' and pid>0 and props['ControlGroup'].endswith('/'+private_slice+'/'+child+'.service')
    subprocess.run(['/usr/bin/systemctl','--user','stop',child+'.service'],capture_output=True,timeout=5,check=True)
    out,err=waiter.communicate(timeout=6)
    assert extract_io_proof(out,rid) is not None,(out,err)
    deadline=time.monotonic()+3
    while Path('/proc',str(pid)).exists() and time.monotonic()<deadline:time.sleep(.05)
    assert not Path('/proc',str(pid)).exists()
    status=Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:\t1' in status and 'CapEff:\t0000000000000000' in status
    assert os.readlink('/proc/self/ns/net')!=host_netns and proof['netns']==host_netns
    routes=Path('/proc/net/route').read_text().splitlines()[1:]
    assert all(line.split()[1]!='00000000' or line.split()[0]=='lo' for line in routes)
    try:socket.create_connection(('127.0.0.1',port),timeout=.2)
    except OSError:pass
    else:raise AssertionError('launcher reached the host TCP listener')
    udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    udp.sendto(b'launcher-fixture',('127.0.0.1',udp_port));udp.close()
    assert (Path('/run/user')/str(os.getuid())/'bus').is_socket()
    assert sentinel.read_text()=='fixture-runtime'
    try:sentinel.write_text('forbidden')
    except OSError:pass
    else:raise AssertionError('/run/user is writable')
    claude=root/'.claude/.credentials.json';assert claude.read_text()=='fixture-claude'
    try:claude.write_text('changed')
    except OSError:pass
    else:raise AssertionError('Claude source is writable')
    (root/'.codex/auth.json').write_text('fixture-cas')
    (root/'Desktop/Sniper/data/office-control/state-proof').write_text('fixture-state')
    print(json.dumps(dict(state='passed',manager_start=True,manager_show=True,manager_stop=True,
        no_network=True,child_bwrap=True,child_host_network=True,bus_readonly=True,
        claude_readonly=True,codex_cas_writable=True,child_uid=proof['uid'])),flush=True)
finally:
    subprocess.run(['/usr/bin/systemctl','--user','stop',child+'.service'],capture_output=True,timeout=5)
    if waiter is not None and waiter.poll() is None:waiter.communicate(timeout=8)
'''



@pytest.fixture
def launcher_home():
    if not Path('/run/user',str(os.getuid()),'bus').is_socket():pytest.skip('requires a local user manager')
    cache=Path.home()/'.cache/codex';assert cache.is_dir()
    with tempfile.TemporaryDirectory(prefix='launcher-manager-fixture-',dir=cache) as td:
        home=Path(td);repo=home/'Desktop/Sniper'
        for folder in (home/'.codex',home/'.claude',repo/'data/office/launcher',repo/'data/office-control',repo/'data/desk',repo/'.runtime/office/current'):
            folder.mkdir(parents=True,exist_ok=True)
        (home/'.claude/.credentials.json').write_text('fixture-claude')
        (home/'.codex/auth.json').write_text('fixture-codex')
        yield home


def fixture_command(home,parent,payload,*,slice_name=None):
    properties,args=service(home)
    python=next(i for i,arg in enumerate(args) if arg.endswith('/bin/python3'))
    argv=['/usr/bin/systemd-run','--user','--wait','--pipe','--collect','--unit='+parent]
    for name,value in properties:argv+=['--property='+name+'='+value]
    return isolated_command(argv,slice_name=slice_name)+['--property=Restart=no','--property=RuntimeMaxSec=25',*args[:python],*payload]


def test_real_launcher_unit_can_start_show_and_stop_an_isolated_fixture_child(launcher_home):
    home=launcher_home;repo=home/'Desktop/Sniper';control=repo/'data/office-control'
    identity=uuid.uuid4();parent='office-test-launcher-boundary-'+identity.hex;rid=str(identity);child='office-model-'+rid;slice_name=fixture_slice()
    lock=repo/'data/desk/codex_call.lock';lock.touch()
    probe=home/'probe.py';probe.write_text(PROBE);source=str(UNIT.parents[2])
    # The outside caller uses the actual CHECKER/JUDGE default slot API, on fixture state only.
    host_code="import sys,json,select;sys.path.insert(0,sys.argv[1]);from scripts.memecoin_desk_checker import codex_slot,codex_lock_path;\nwith codex_slot(timeout_s=0):\n p=codex_lock_path();s=p.stat();print(json.dumps(dict(path=str(p),identity=[s.st_dev,s.st_ino])),flush=True);select.select([sys.stdin],[],[],30)"
    host=subprocess.Popen([sys.executable,'-I','-B','-c',host_code,source],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env=dict(PATH='/usr/bin:/bin',HOME=str(home),SNIPER_STATE_ROOT=str(repo)))
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    udp_listener=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp_listener.bind(('127.0.0.1',0));udp_listener.settimeout(.3)
    sentinel=Path('/run/user')/str(os.getuid())/(parent+'.sentinel');sentinel.write_text('fixture-runtime')
    job=None
    try:
        assert select.select([host.stdout],[],[],6)[0],'host slot helper did not start'
        ready=json.loads(host.stdout.readline());assert ready['path']==str(lock)
        payload=['/usr/bin/python3','-I','-B',str(probe),rid,str(home),source,str(listener.getsockname()[1]),str(udp_listener.getsockname()[1]),os.readlink('/proc/self/ns/net'),str(sentinel),json.dumps(ready['identity']),slice_name]
        job=subprocess.Popen(fixture_command(home,parent,payload,slice_name=slice_name),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+8
        while not (control/'lock-busy').exists() and job.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        if not (control/'lock-busy').exists():
            out,err=job.communicate(timeout=5);pytest.fail(out+err)
        assert not (control/'lock-acquired').exists()
        host.communicate('release\n',timeout=5);assert host.returncode==0
        (control/'lock-released').write_text('host-released')
        out,err=job.communicate(timeout=30);assert job.returncode==0,out+err
        proof=json.loads(out);assert proof['state']=='passed' and proof['child_uid']==os.getuid()
        assert (control/'lock-acquired').read_text()=='after-host'
        assert (home/'.claude/.credentials.json').read_text()=='fixture-claude'
        assert (home/'.codex/auth.json').read_text()=='fixture-cas'
        assert sentinel.read_text()=='fixture-runtime'
        packets=[]
        try:
            while True:packets.append(udp_listener.recv(1024))
        except TimeoutError:pass
        assert packets==[b'child-fixture'],packets
    finally:
        subprocess.run(['/usr/bin/systemctl','--user','stop',parent+'.service',child+'.service',slice_name],capture_output=True,timeout=8)
        if job is not None and job.poll() is None:job.communicate(timeout=5)
        if host.poll() is None:host.communicate('release\n',timeout=5)
        listener.close();udp_listener.close();sentinel.unlink(missing_ok=True)


def test_missing_host_slot_refuses_start_without_a_private_replacement(launcher_home):
    home=launcher_home;parent='office-test-launcher-missing-slot-'+uuid.uuid4().hex;slice_name=fixture_slice()
    lock=home/'Desktop/Sniper/data/desk/codex_call.lock';assert not lock.exists()
    try:
        result=subprocess.run(fixture_command(home,parent,['/usr/bin/true'],slice_name=slice_name),capture_output=True,text=True,timeout=8)
        assert result.returncode!=0 and str(lock) in result.stderr,result.stdout+result.stderr
        assert not lock.exists()
        assert not list((home/'Desktop/Sniper/data/office-control').iterdir())
    finally:
        subprocess.run(['/usr/bin/systemctl','--user','stop',parent+'.service',slice_name],capture_output=True,timeout=5)
