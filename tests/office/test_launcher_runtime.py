from pathlib import Path
import shlex


def test_supervisor_unit_has_no_writable_host_credentials():
    unit=Path(__file__).resolve().parents[2]/'deploy/office/office-supervisor.service'
    text=unit.read_text().replace('%h','/home/operator')
    args=shlex.split(next(line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')))
    credentials=('/home/operator/.codex','/home/operator/.claude')
    for path in credentials:
        assert any(args[i:i+2]==['--tmpfs',path]for i in range(len(args)))
    for at,arg in enumerate(args):
        if arg=='--bind':
            assert not any(args[at+1]==p or args[at+1].startswith(p+'/')for p in credentials)
    for line in text.splitlines():
        if line.startswith('ReadWritePaths='):
            assert not any(p in line for p in credentials)


def test_bwrap_units_do_not_request_nested_systemd_mount_namespaces():
    units=Path(__file__).resolve().parents[2]/'deploy/office'
    forbidden={'PrivateTmp','ProtectSystem','ProtectHome','ReadWritePaths','ReadOnlyPaths','InaccessiblePaths','TemporaryFileSystem','BindPaths','BindReadOnlyPaths','PrivateDevices','PrivateMounts','PrivateUsers'}
    checked=[]
    for path in sorted(units.glob('*.service')):
        text=path.read_text()
        if not any(line.startswith('ExecStart=') and '/usr/bin/bwrap' in line for line in text.splitlines()):continue
        directives={line.split('=',1)[0] for line in text.splitlines() if '=' in line and not line.lstrip().startswith('#')}
        conflicts={key for key in directives if key in forbidden or key.startswith('Protect')}
        assert not conflicts,(path.name,sorted(conflicts))
        args=shlex.split(next(line.split('=',1)[1] for line in text.splitlines() if line.startswith('ExecStart=')))
        assert ['--ro-bind','/','/'] in [args[i:i+3] for i in range(len(args))]
        assert ['--tmpfs','/tmp'] in [args[i:i+2] for i in range(len(args))]
        checked.append(path.name)
    # By name: a new bwrap unit must be added here on purpose, and a count cannot say which one changed.
    assert checked==sorted(('office-child-launcher.service','office-monitor.service','office-paper-watcher-v2-gpt.service','office-paper-watcher-v2.service','office-paper-watcher.service','office-supervisor.service','office-watchdog.service'))


def test_launcher_unit_narrow_host_auth_mounts():
    text=(Path(__file__).resolve().parents[2]/'deploy/office/office-child-launcher.service').read_text()
    args=shlex.split(next(line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')))
    assert any(args[i:i+3]==['--bind','%h/.codex','%h/.codex']for i in range(len(args)))
    assert any(args[i:i+3]==['--ro-bind','%h/.claude','%h/.claude']for i in range(len(args)))
    assert not any(args[i:i+2]==['--bind','%h/.claude']for i in range(len(args)))


def test_supervisor_hides_launcher_private_roots():
    text=(Path(__file__).resolve().parents[2]/'deploy/office/office-supervisor.service').read_text()
    args=shlex.split(next(line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')))
    control='%h/Desktop/Sniper/data/office-control'
    assert any(args[i:i+2]==['--tmpfs',control]for i in range(len(args)))
    assert any(args[i:i+3]==['--ro-bind',control+'/guard',control+'/guard']for i in range(len(args)))
    assert '/auth-private'not in text and '/children'not in text


def test_reviewed_cas_helper_writes_only_three_approved_names():
    import ast,inspect
    from scripts.memecoin_desk_checker import sync_auth_back
    tree=ast.parse(inspect.getsource(sync_auth_back))
    names=[call.args[0]for call in ast.walk(tree)if isinstance(call,ast.Call)
           and isinstance(call.func,ast.Attribute)and call.func.attr=='with_name']
    assert len(names)==2
    assert isinstance(names[0],ast.Constant)and names[0].value=='auth.json.pre-checker'
    assert ast.unparse(names[1])=="'.auth.json.checker-%d' % os.getpid()"
    writers=[call.func.value.id for call in ast.walk(tree)if isinstance(call,ast.Call)
             and isinstance(call.func,ast.Attribute)and call.func.attr=='write_bytes']
    assert writers==['backup']
    stage_opens=[ast.unparse(call)for call in ast.walk(tree)if isinstance(call,ast.Call)
                 and isinstance(call.func,ast.Attribute)and call.func.attr=='open']
    assert sorted(stage_opens)==sorted(["stage.open('wb')", 'os.open(real.parent, os.O_RDONLY | os.O_DIRECTORY)'])
    streamed_writes=[ast.unparse(call)for call in ast.walk(tree)if isinstance(call,ast.Call)
                     and isinstance(call.func,ast.Attribute)and call.func.attr=='write']
    assert streamed_writes==['staged.write(new_b)']
    replacements=[ast.unparse(call)for call in ast.walk(tree)if isinstance(call,ast.Call)
                  and isinstance(call.func,ast.Attribute)and call.func.attr=='replace']
    assert replacements==['os.replace(stage, real)']


def test_actual_supervisor_mounts_hide_auth_and_children(tmp_path):
    import subprocess,sys
    home=tmp_path/'home';control=home/'Desktop/Sniper/data/office-control'
    for folder in (home/'.codex',home/'.claude',control/'auth-private',control/'children',
                   home/'Desktop/Sniper/data/office',home/'Desktop/Sniper/data/desk'):
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'secret').write_text('credential-fixture')
    (control/'guard').mkdir();(control/'guard/guard.json').write_text('guard-fixture');(control/'launcher-sock').mkdir()
    (home/'Desktop/Sniper/data/desk/codex_call.lock').touch()
    (control/'transport.json').write_text('transport-fixture')
    secrets=[home/'Desktop/Sniper'/name for name in ('.env','.j7.env','sniper_tracker.db')]
    for path in secrets:path.write_text('funded-secret-fixture')
    absent=[home/'Desktop/Sniper'/name for name in ('sniper_tracker.db-wal','sniper_tracker.db-shm')]
    masks=secrets+absent
    text=(Path(__file__).resolve().parents[2]/'deploy/office/office-supervisor.service').read_text().replace('%h',str(home))
    args=shlex.split(next(line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')))
    args=args[:args.index('/usr/bin/env')]
    hidden=[home/'.codex/secret',home/'.claude/secret',control/'auth-private/secret',control/'children/secret']
    code='from pathlib import Path; import os; assert all(not Path(p).exists()for p in '+repr(list(map(str,hidden)))+'); assert Path('+repr(str(control/'guard/guard.json'))+').read_text()=="guard-fixture"; assert Path('+repr(str(control/'transport.json'))+').read_text()=="transport-fixture"'
    code+='; assert os.statvfs("/tmp").f_blocks*os.statvfs("/tmp").f_frsize==268435456\nfor p in '+repr(list(map(str,masks)))+':\n try:\n  assert Path(p).read_bytes()==b""\n except PermissionError:\n  pass\nprint("isolated")\n'
    result=subprocess.run(args+[sys.executable,'-c',code],capture_output=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert result.stdout==b'isolated\n'
    assert all(path.read_text()=='funded-secret-fixture' for path in secrets)
    assert all(not path.exists() for path in absent)
    for path in masks:assert ['--ro-bind-try','/dev/null',str(path)] in [args[i:i+3] for i in range(len(args))]
    assert ['--size','268435456','--tmpfs','/tmp'] in [args[i:i+4] for i in range(len(args))]


def test_model_child_gets_dedicated_guard_directory_only(tmp_path):
    import uuid
    from sniper.office.child_launcher import child_command,validate_request
    rid=str(uuid.uuid4());parent=tmp_path/rid;parent.mkdir();(parent/'request.json').write_text('{}')
    request=validate_request(dict(request_id=rid,adapter_id='codex_exec',request_file=rid+'/request.json',deadline_at=101),root=tmp_path,now=100)
    guard=tmp_path/'control/guard/guard.json'
    config={'guard_file':str(guard),'adapters':{'codex_exec':{'argv':['fixed'],'ro_binds':[]}}}
    args=child_command(config,request,scratch=tmp_path/'scratch',now=100)
    assert any(args[i:i+3]==['--setenv','PYTHONPATH','/opt/office:/opt/site']for i in range(len(args)))
    assert any(args[i:i+3]==['--setenv','LD_LIBRARY_PATH','/opt/python/lib']for i in range(len(args)))
    bindings=[args[i+1:i+3]for i,x in enumerate(args)if x=='--ro-bind']
    assert [str(guard.parent),'/opt/guard']in bindings
    assert [str(guard.parent.parent),'/opt/guard']not in bindings


def factory_config(tmp_path,monkeypatch):
    import hashlib,json
    home=tmp_path/'home';control=home/'Desktop/Sniper/data/office-control';control.mkdir(parents=True)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    site=home/'.local/lib/python3.13/site-packages';site.mkdir(parents=True)
    roots=[str(home/'release'),str(home/'python'),str(site)]
    doc=dict(private_state=str(control/'auth-private'),child_root=str(control/'children'),
             launcher_state=str(control/'launcher-state'),state_root=str(control/'launcher-state'),
             request_root=str(home/'Desktop/Sniper/data/office/launcher/requests'),
             codex_home=str(home/'.codex'),codex_lock=str(home/'Desktop/Sniper/data/desk/codex_call.lock'),
             claude_credentials=str(home/'.claude/.credentials.json'),guard_file=str(control/'guard/guard.json'),socket_path=str(control/'launcher-sock/launcher.sock'),
             model_roots=roots+[str(control/'guard')],supervisor_roots=[str(home/'Desktop/Sniper/data/office'),str(home/'Desktop/Sniper/data/desk/codex_call.lock')],
             adapters={k:dict(ro_binds=[[roots[0],'/opt/office'],[roots[1],'/opt/python'],[roots[2],'/opt/site']])for k in('claude_sdk','codex_exec')})
    path=control/'launcher.json'
    def save():
        raw=json.dumps(doc).encode();path.write_bytes(raw);return hashlib.sha256(raw).hexdigest()
    return doc,path,save


def test_factory_pins_completion_and_actual_mount_roots(tmp_path,monkeypatch):
    from sniper.office.launcher_runtime import build_auth
    from sniper.office.launcher_bindings import Completion
    doc,path,save=factory_config(tmp_path,monkeypatch)
    auth=build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    assert auth.config==doc and isinstance(auth.completed,Completion)
    assert auth.completed.state_root==Path(doc['launcher_state'])
    assert auth.private_state==Path(doc['private_state'])


import pytest
@pytest.mark.parametrize('failure',['pin','state','models','supervisor','private_location','child_location','claude_path'])
def test_factory_refuses_conflicting_auth_authority_before_creating_state(tmp_path,monkeypatch,failure):
    from sniper.office.launcher_runtime import build_auth
    doc,path,save=factory_config(tmp_path,monkeypatch)
    if failure=='state':doc['state_root']=doc['request_root']
    if failure=='models':doc['model_roots']=[]
    if failure=='supervisor':doc['supervisor_roots']=[]
    if failure=='private_location':doc['private_state']=doc['request_root']+'/private'
    if failure=='child_location':doc['child_root']=doc['request_root']+'/children'
    if failure=='claude_path':doc['claude_credentials']=str(tmp_path/'other/.credentials.json')
    pin=save()
    with pytest.raises(ValueError):
        build_auth(path,expected_sha256='0'*64 if failure=='pin'else pin,clock=lambda:1000,alert=lambda *a:None)
    assert not Path(doc['private_state']).exists()


def test_actual_child_guard_observes_atomic_replacement(tmp_path):
    import json,subprocess,uuid
    from sniper.office.child_launcher import child_command,validate_request
    rid=str(uuid.uuid4());parent=tmp_path/rid;parent.mkdir();(parent/'request.json').write_text('{}')
    request=validate_request(dict(request_id=rid,adapter_id='codex_exec',request_file=rid+'/request.json',deadline_at=101),root=tmp_path,now=100)
    guard=tmp_path/'control/guard/guard.json';guard.parent.mkdir(parents=True);guard.write_text('ready')
    scratch=tmp_path/'scratch';scratch.mkdir()
    args=child_command({'guard_file':str(guard),'adapters':{'codex_exec':{'argv':['fixed'],'ro_binds':[]}}},request,scratch=scratch,now=100)
    args=args[args.index('/usr/bin/bwrap'):args.index('/usr/bin/python3')]
    code='from pathlib import Path; import sys; p=Path("/opt/guard/guard.json"); print(p.read_text(),flush=True); sys.stdin.readline(); print(p.read_text(),flush=True)'
    proc=subprocess.Popen(args+['/usr/bin/python3','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        import select
        assert select.select([proc.stdout],[],[],3)[0]
        assert proc.stdout.readline()==b'ready\n'
        stage=guard.with_name('guard.tmp');stage.write_text('paused');stage.replace(guard)
        output,error=proc.communicate(b'continue\n',timeout=3)
        assert proc.returncode==0,error
        assert output==b'paused\n'
    finally:
        if proc.poll()is None:proc.kill();proc.wait()


def test_factory_requires_usersite_descriptor_in_declared_model_roots(tmp_path,monkeypatch):
    from sniper.office.launcher_runtime import build_auth
    doc,path,save=factory_config(tmp_path,monkeypatch)
    site=str(Path.home()/'.local/lib/python3.13/site-packages')
    doc['model_roots'].remove(site)
    with pytest.raises(ValueError,match='launcher_model_roots'):
        build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    doc['model_roots'].append(site)
    auth=build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    assert site in auth.config['model_roots']


def test_library_audit_refuses_before_private_state_or_auth(tmp_path,monkeypatch):
    from sniper.office import launcher_runtime as F
    doc,path,save=factory_config(tmp_path,monkeypatch)
    site=Path.home()/'.local/lib/python3.13/site-packages'
    (site/'startup.pth').write_text('import unapproved_startup\n')
    monkeypatch.setattr(F,'Auth',lambda *a,**k:pytest.fail('auth ran before library audit'))
    with pytest.raises(ValueError):
        F.build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    assert not Path(doc['private_state']).exists()


@pytest.mark.parametrize('change',['absent','duplicate'])
def test_library_mount_must_be_unambiguous_for_each_adapter(tmp_path,monkeypatch,change):
    from sniper.office import launcher_runtime as F
    doc,path,save=factory_config(tmp_path,monkeypatch)
    mounts=doc['adapters']['claude_sdk']['ro_binds']
    site=next(pair for pair in mounts if pair[1]=='/opt/site')
    if change=='absent':mounts.remove(site)
    else:mounts.append(list(site))
    with pytest.raises(ValueError):
        F.build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    assert not Path(doc['private_state']).exists()


@pytest.mark.parametrize('mutation',['writable','redirected_source','redirected_target','whole_control'])
def test_factory_rejects_every_transport_mount_except_exact_readonly_file(tmp_path,monkeypatch,mutation):
    from sniper.office import launcher_runtime as F
    doc,path,save=factory_config(tmp_path,monkeypatch)
    unit=Path(F.__file__).resolve().parents[2]/'deploy/office/office-supervisor.service'
    original=F.read_regular
    control='%h/Desktop/Sniper/data/office-control'
    approved=f'--ro-bind {control}/transport.json {control}/transport.json'
    changed={'writable':f'--bind {control}/transport.json {control}/transport.json',
        'redirected_source':f'--ro-bind /unapproved.json {control}/transport.json',
        'redirected_target':f'--ro-bind {control}/transport.json {control}/auth-private',
        'whole_control':f'--ro-bind {control} {control}'}[mutation]
    def read(p):
        raw=original(p)
        return raw.replace(approved.encode(),changed.encode()) if Path(p)==unit else raw
    monkeypatch.setattr(F,'read_regular',read)
    with pytest.raises(ValueError,match='launcher_supervisor_'):
        F.build_auth(path,expected_sha256=save(),clock=lambda:1000,alert=lambda *a:None)
    assert not Path(doc['private_state']).exists()
