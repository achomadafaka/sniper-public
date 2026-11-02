"""Untrusted requests select an approved adapter, never argv or a host path."""
from pathlib import Path
import json
import uuid
import subprocess
from office.fixture_units import fixture_slice, isolated_command
import pytest
from sniper.office import child_launcher as C


def request(tmp_path):
    tmp_path.mkdir(parents=True,exist_ok=True)
    rid=str(uuid.uuid4());d=tmp_path/rid;d.mkdir();(d/'request.json').write_text('{}')
    return dict(adapter_id='codex_exec',request_id=rid,request_file=rid+'/request.json',deadline_at=110.)


def test_request_rejects_unknown_adapter_keys_paths_and_deadlines(tmp_path):
    r=request(tmp_path)
    assert C.validate_request(r,root=tmp_path,now=100.).request_id==r['request_id']
    for over in [dict(adapter_id='shell'),dict(argv=['sh']),dict(request_file='/etc/passwd'),
                 dict(request_file='../request.json'),dict(deadline_at=float('nan')),dict(deadline_at=100.),
                 dict(deadline_at=1000.)]:
        with pytest.raises(ValueError):C.validate_request(dict(r,**over),root=tmp_path,now=100.)
    (tmp_path/r['request_file']).unlink();(tmp_path/r['request_file']).symlink_to('/etc/passwd')
    with pytest.raises(ValueError):C.validate_request(r,root=tmp_path,now=100.)


def test_peer_requires_exact_supervisor_service_and_pinned_entrypoint(tmp_path):
    p=tmp_path/'123';p.mkdir()
    (p/'stat').write_text('123 (python) '+' '.join(['0']*19+['777']))
    (p/'cgroup').write_text('0::/user.slice/office.slice/office-supervisor.service\n')
    (p/'cmdline').write_bytes(b'python3\0/pinned/scripts/office_supervisor.py\0')
    kw=dict(proc_root=tmp_path,expected_cgroup='/user.slice/office.slice/office-supervisor.service',
            supervisor_script='/pinned/scripts/office_supervisor.py',expected_uid=1000)
    assert C.validate_peer(123,1000,**kw)=='777'
    with pytest.raises(ValueError):C.validate_peer(123,1001,**kw)
    (p/'cgroup').write_text('0::/user.slice/office.slice/office-model-other.service\n')
    with pytest.raises(ValueError):C.validate_peer(123,1000,**kw)


def test_command_uses_sibling_service_and_hides_control_socket(tmp_path):
    r=C.validate_request(request(tmp_path),root=tmp_path,now=100.)
    cfg={'guard_file':str(tmp_path/'guard/guard.json'),'adapters':{'codex_exec':{'argv':['/opt/python/bin/python3','/opt/office/scripts/office_model_child.py',
             '--adapter','codex_exec','--request','/opt/scratch/request.json','--output','/opt/scratch/result.json'],
             'ro_binds':[['/trusted/python','/opt/python'],['/pinned','/opt/office']]}}}
    args=C.child_command(cfg,r,scratch=tmp_path/'child',now=100.)
    assert '--slice=office.slice' in args and '--property=NoNewPrivileges=no' in args
    assert args.count('--bind')==1 and '--cap-drop' in args
    assert '--unshare-pid' in args and '--unshare-net' not in args
    for path in ['/home','/root','/run','/tmp']:
        assert any(args[i:i+2]==['--tmpfs',path] for i in range(len(args)))
    assert str(tmp_path/'child') in args
    assert not any('launcher.sock' in a for a in args)


def test_no_spawn_on_unhealthy_guard_or_duplicate_intent(tmp_path):
    r=C.validate_request(request(tmp_path/'requests'),root=tmp_path/'requests',now=100.)
    calls=[]
    with pytest.raises(ValueError):C.reserve(tmp_path/'state',r,guard_reason='paused')
    assert C.reserve(tmp_path/'state',r,guard_reason=None)
    assert not C.reserve(tmp_path/'state',r,guard_reason=None)

def test_handle_consumes_run_child_dict_and_caches_completion(tmp_path,monkeypatch):
    import importlib.util,sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    spec=importlib.util.spec_from_file_location('launcher_server',Path(__file__).resolve().parents[2]/'scripts/office_child_launcher.py')
    S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)
    r=request(tmp_path/'requests');home=tmp_path/'requests'/r['request_id']/'home/.codex'
    home.mkdir(parents=True);(home/'auth.json').write_text('{}')
    cfg=dict(supervisor_cgroup='fixed',supervisor_script='fixed',clock_file='fixed',office_cgroup='fixed',
             guard_file='fixed',request_root=tmp_path/'requests',state_root=tmp_path/'state',child_root=tmp_path/'children')
    monkeypatch.setattr(S,'validate_peer',lambda *a,**k:'cookie')
    monkeypatch.setattr(S,'receive',lambda _:r)
    monkeypatch.setattr(S,'corrected_now',lambda *a,**k:100.)
    monkeypatch.setattr(S,'caps_applied',lambda _:True)
    monkeypatch.setattr(S,'external_guard',lambda _:None)
    monkeypatch.setattr(S,'child_command',lambda *a,**k:['fixed'])
    monkeypatch.setattr(S.subprocess,'run',lambda *a,**k:None)
    class Conn:
        def getsockopt(self,*a):
            import struct,os
            return struct.pack('3i',123,os.getuid(),os.getgid())
    calls=[]
    def spawn(*a,**k):
        calls.append(1);(cfg['child_root']/r['request_id']/'result.json').write_text('{}')
        return {'outcome':'success','stdout':('OFFICE_IO_PREEXEC '+json.dumps(dict(request_id=r['request_id'],source='ioprio_get',io_class=3,before_provider_exec=True,observed_at_host=1000.))).encode(),'returncode':0}
    from contextlib import nullcontext
    auth=lambda *a,**kw:nullcontext()
    result=S.handle(Conn(),cfg,execute=spawn,auth=auth)
    assert result['outcome']=='success' and result['output_file']==r['request_id']+'/result.json'
    assert S.handle(Conn(),cfg,execute=spawn,auth=auth)==result and calls==[1]

def test_sibling_spawn_from_networkless_nnp_parent_hides_socket_and_host_files(tmp_path):
    import os,socket,subprocess,time,tempfile
    scratch=tmp_path/'child';scratch.mkdir()
    outside=tmp_path/'outside';outside.write_text('canary')
    with tempfile.TemporaryDirectory(prefix='.dl-',dir=Path.home()) as sd, socket.socket(socket.AF_UNIX) as sock:
        sockpath=Path(sd)/'s'
        sock.bind(str(sockpath));sock.listen(1)
        code="""from pathlib import Path
import socket,sys
assert not Path(sys.argv[1]).exists()
assert not Path(sys.argv[2]).exists()
try:
 s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[2])
except OSError:pass
else:raise AssertionError('launcher socket exposed')
Path('/opt/scratch/result.json').write_text('{}')
status=Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status and 'CapEff:\\t0000000000000000' in status
"""
        cfg={'guard_file':str(tmp_path/'guard/guard.json'),'adapters':{'codex_exec':{'argv':['/usr/bin/python3','-c',code,str(outside),str(sockpath)],'ro_binds':[]}}}
        now=time.time();guard=tmp_path/'guard';guard.mkdir();(guard/'guard.json').write_text(json.dumps({'state':'ready','reason':None,'at':now,'guard_policy_version':'office-host-guard-pilot-v3'}))
        obj=request(tmp_path/'requests');obj['deadline_at']=now+15
        r=C.validate_request(obj,root=tmp_path/'requests',now=now)
        slice_name=fixture_slice()
        child=isolated_command(C.child_command(cfg,r,scratch=scratch,now=now),slice_name=slice_name)
        child.insert(child.index('/usr/bin/bwrap'),'--property=MemoryMax=128M')
        child.insert(child.index('/usr/bin/bwrap'),'--property=CPUQuota=5%')
        outer=['/usr/bin/bwrap','--unshare-user','--unshare-net','--ro-bind','/','/','--proc','/proc',
               '--dev','/dev','--tmpfs','/tmp','--bind',str(tmp_path),str(tmp_path),'--cap-drop','ALL',
               '/usr/bin/python3','-c','import json,subprocess,sys;sys.exit(subprocess.run(json.loads(sys.argv[1])).returncode)',json.dumps(child)]
        env={k:v for k,v in os.environ.items() if k in ('HOME','PATH','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS')}
        try:got=subprocess.run(outer,capture_output=True,text=True,timeout=30,env=env)
        finally:subprocess.run(['/usr/bin/systemctl','--user','stop',slice_name],capture_output=True,timeout=5)
        assert got.returncode==0,got.stdout+got.stderr
        assert (scratch/'result.json').read_text()=='{}'
        assert outside.read_text()=='canary'

def test_real_socket_peer_outside_supervisor_service_is_refused_before_reading():
    import importlib.util,os,socket,sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    spec=importlib.util.spec_from_file_location('launcher_peer_server',Path(__file__).resolve().parents[2]/'scripts/office_child_launcher.py')
    S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)
    a,b=socket.socketpair()
    try:
        with pytest.raises(ValueError,match='peer_not_supervisor'):
            S.handle(a,{'supervisor_cgroup':'/not-the-supervisor','supervisor_script':'/not-the-supervisor.py'})
    finally:a.close();b.close()

def test_child_gate_refuses_pause_before_entrypoint(tmp_path):
    import subprocess,time
    guard=tmp_path/'guard';guard.mkdir();p=guard/'guard.json'
    p.write_text(json.dumps({'state':'paused','reason':'cpu_pressure','at':time.time()}))
    cmd=['/usr/bin/bwrap','--unshare-user','--ro-bind','/','/','--proc','/proc','--tmpfs','/opt',
         '--ro-bind',str(guard),'/opt/guard','--cap-drop','ALL','/usr/bin/python3','-c',C.CHILD_GATE,
         '/usr/bin/printf','ENTRYPOINT_RAN']
    got=subprocess.run(cmd,capture_output=True,text=True,timeout=10)
    assert got.returncode!=0 and 'guard_refused' in got.stderr and 'ENTRYPOINT_RAN' not in got.stdout

def test_fifo_refuses_without_writer_and_next_regular_read_succeeds(tmp_path):
    import os,subprocess,sys
    fifo=tmp_path/'request.json';os.mkfifo(fifo)
    regular=tmp_path/'next.json';regular.write_text('{}')
    code="""from sniper.office.child_launcher import read_regular
import sys
try:read_regular(sys.argv[1])
except ValueError:pass
else:raise AssertionError('FIFO accepted')
assert read_regular(sys.argv[2])==b'{}'
"""
    # A regression fails by bounded subprocess timeout, never hangs the pytest worker.
    result=subprocess.run([sys.executable,'-c',code,str(fifo),str(regular)],
                          cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=1)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('adapter,readonly',[('claude_sdk',True),('codex_exec',False)])
def test_only_claude_auth_has_readonly_overlay_after_scratch(tmp_path,adapter,readonly):
    r=request(tmp_path);r['adapter_id']=adapter
    req=C.validate_request(r,root=tmp_path,now=100.)
    cfg=dict(guard_file=str(tmp_path/'guard/guard.json'),adapters={adapter:dict(argv=['fixed'],ro_binds=[])})
    args=C.child_command(cfg,req,scratch=tmp_path/'child',now=100.)
    mount=['--ro-bind',str(tmp_path/'child/home/.claude/.credentials.json'),'/opt/scratch/home/.claude/.credentials.json']
    positions=[i for i in range(len(args)) if args[i:i+3]==mount]
    assert bool(positions) is readonly
    if readonly:assert positions[0]>args.index('--bind')


@pytest.mark.parametrize('adapter,expected',[('claude_sdk','denied'),('codex_exec','writable')])
def test_actual_sibling_auth_mount_permissions(tmp_path,adapter,expected):
    import os,time
    r=request(tmp_path/'requests');r['adapter_id']=adapter;r['deadline_at']=time.time()+10
    req=C.validate_request(r,root=tmp_path/'requests',now=time.time())
    scratch=tmp_path/'child';name='.claude/.credentials.json' if adapter=='claude_sdk' else '.codex/auth.json'
    auth=scratch/'home'/name;auth.parent.mkdir(parents=True);auth.write_text('fixture')
    guard=tmp_path/'guard';guard.mkdir();(guard/'guard.json').write_text(json.dumps(dict(state='ready',reason=None,at=time.time(),guard_policy_version='office-host-guard-pilot-v3')))
    program="""from pathlib import Path
p=Path.home()/__import__('sys').argv[1]
stage=Path('/opt/scratch')/'replacement';stage.write_text('changed')
results=[]
for operation in (lambda:p.write_text('changed'),lambda:p.unlink(),lambda:__import__('os').replace(stage,p)):
 try:
  operation();results.append('writable')
 except OSError:
  results.append('denied')
print(','.join(results))
(Path('/opt/scratch')/'own-write').write_text('allowed')
"""
    cfg=dict(guard_file=str(guard/'guard.json'),adapters={adapter:dict(argv=['/usr/bin/python3','-c',program,name],ro_binds=[])})
    slice_name=fixture_slice()
    cmd=isolated_command(C.child_command(cfg,req,scratch=scratch,now=time.time()),slice_name=slice_name)
    at=cmd.index('/usr/bin/bwrap');cmd[at:at]=['--property=MemoryMax=64M','--property=CPUQuota=20%','--property=TasksMax=16']
    try:result=subprocess.run(cmd,capture_output=True,text=True,timeout=15)
    finally:subprocess.run(['/usr/bin/systemctl','--user','stop',slice_name],capture_output=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert C.extract_io_proof(result.stdout.encode(),req.request_id) is not None
    assert result.stdout.strip().splitlines()[-1]==','.join([expected]*3) and (scratch/'own-write').read_text()=='allowed'
    assert auth.read_text()==('fixture' if expected=='denied' else 'changed')
