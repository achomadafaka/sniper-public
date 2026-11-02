from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
from uuid import uuid4
import pytest


def fixture(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
    spec=importlib.util.spec_from_file_location('launcher_auth_server',Path(__file__).resolve().parents[2]/'scripts/office_child_launcher.py')
    S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)
    rid=str(uuid4());root=tmp_path/'requests'/rid;root.mkdir(parents=True);(root/'request.json').write_text('{}')
    req=dict(adapter_id='codex_exec',request_id=rid,request_file=rid+'/request.json',deadline_at=110.)
    cfg=dict(supervisor_cgroup='fixed',supervisor_script='fixed',clock_file='fixed',office_cgroup='fixed',guard_file='fixed',
             request_root=tmp_path/'requests',state_root=tmp_path/'state',child_root=tmp_path/'children')
    for name,fn in dict(validate_peer=lambda *a,**k:'cookie',receive=lambda _:req,corrected_now=lambda *a,**k:100.,
                        caps_applied=lambda _:True,external_guard=lambda _:None,child_command=lambda *a,**k:['fixed']).items():
        monkeypatch.setattr(S,name,fn)
    events=[]
    monkeypatch.setattr(S.subprocess,'run',lambda *a,**k:events.append('stop'))
    class Conn:
        def getsockopt(self,*a):return struct.pack('3i',123,os.getuid(),os.getgid())
    def execute(*a,**k):
        events.append('execute');(cfg['child_root']/rid/'result.json').write_text(json.dumps({'text':'{}'}))
        return dict(outcome='success',returncode=0,stdout=('OFFICE_IO_PREEXEC '+json.dumps(dict(request_id=rid,source='ioprio_get',io_class=3,before_provider_exec=True,observed_at_host=1000.))).encode())
    return S,cfg,rid,events,Conn(),execute


def test_auth_is_host_side_and_completion_precedes_cleanup_and_reply(tmp_path,monkeypatch):
    S,cfg,rid,events,conn,execute=fixture(tmp_path,monkeypatch)
    @contextmanager
    def auth(adapter,parent,*,deadline_at):
        assert parent==cfg['child_root']/rid and deadline_at==110.
        assert not (cfg['request_root']/rid/'home').exists()
        events.append('auth_enter');yield
        assert (cfg['state_root']/(rid+'.result.json')).exists()
        assert not (cfg['state_root']/(rid+'.reply.json')).exists()
        events.append('auth_exit')
    result=S.handle(conn,cfg,execute=execute,auth=auth)
    assert events==['auth_enter','execute','stop','auth_exit']
    assert result['model_result']=={'text':'{}'}
    assert S.handle(conn,cfg,execute=execute,auth=auth)==result and len(events)==4


def test_auth_cleanup_failure_cannot_publish_or_replay_success(tmp_path,monkeypatch):
    S,cfg,rid,events,conn,execute=fixture(tmp_path,monkeypatch)
    @contextmanager
    def auth(*a,**kw):
        yield
        raise ValueError('cleanup_unknown')
    with pytest.raises(ValueError,match='cleanup_unknown'):S.handle(conn,cfg,execute=execute,auth=auth)
    assert not (cfg['state_root']/(rid+'.reply.json')).exists()
    assert S.handle(conn,cfg,execute=execute,auth=auth)['outcome']=='existing_unknown'
    assert events==['execute','stop']


@pytest.mark.parametrize('reason',['codex_busy','codex_cleanup_unproven','claude_auth_near_expiry'])
def test_typed_auth_refusal_is_identity_bound_and_never_cached(tmp_path,monkeypatch,reason):
    from sniper.office.model_auth import AuthRefused
    S,cfg,rid,events,conn,execute=fixture(tmp_path,monkeypatch)
    @contextmanager
    def auth(*a,**kw):
        raise AuthRefused(reason)
        yield
    result=S.handle(conn,cfg,execute=execute,auth=auth)
    assert result['request_id']==rid and result['reason']==reason and result['outcome']=='refused'
    assert not events and not (cfg['state_root']/(rid+'.reply.json')).exists()


def test_protected_runtime_roots_are_private_and_bad_existing_modes_refuse(tmp_path,monkeypatch):
    S,cfg,rid,events,conn,execute=fixture(tmp_path,monkeypatch)
    @contextmanager
    def auth(*a,**kw):yield
    cfg['state_root'].mkdir(mode=0o755)
    with pytest.raises(ValueError,match='private_directory'):S.handle(conn,cfg,execute=execute,auth=auth)
    assert not events
    cfg['state_root'].chmod(0o700)
    S.handle(conn,cfg,execute=execute,auth=auth)
    assert cfg['state_root'].stat().st_mode & 0o777 == 0o700
    assert cfg['child_root'].stat().st_mode & 0o777 == 0o700


def test_success_without_preexec_io_proof_cannot_publish_model_output(tmp_path,monkeypatch):
    S,cfg,rid,events,conn,execute=fixture(tmp_path,monkeypatch)
    @contextmanager
    def auth(*a,**kw):yield
    def no_proof(*a,**kw):
        result=execute(*a,**kw);result.pop('stdout');return result
    result=S.handle(conn,cfg,execute=no_proof,auth=auth)
    assert result['outcome']=='invalid' and result['io_preexec'] is None and result['model_result'] is None
