"""Fake local children only: no systemd/model/auth calls."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sniper.office.supervisor import run_child


def run(tmp_path, code, timeout=1, limit=1024):
    return run_child([sys.executable,'-I','-B','-c',code],timeout_s=timeout,cwd=tmp_path,env={'PATH':os.defpath},max_output_bytes=limit)


def test_capture_stderr_without_corrupting_stdout_or_exit(tmp_path):
    result=run(tmp_path,"import os;os.write(1,b'proof');os.write(2,b'PermissionError: config denied\\n');raise SystemExit(7)")
    assert result['outcome']=='child_error' and result['returncode']==7
    assert result['stdout']==b'proof'
    assert result['stderr']==b'PermissionError: config denied\n'


def test_stderr_flood_is_bounded_not_deadlocked(tmp_path):
    result=run(tmp_path,"import os;os.write(2,b'x'*1000000)",limit=4096)
    assert result['outcome']=='output_limit'
    assert len(result['stderr'])<=16384


def test_grandchild_holding_only_stderr_is_reaped_at_deadline(tmp_path):
    code="import os,time;pid=os.fork();os.write(2,b'child started\\n');os.close(1);time.sleep(30) if pid==0 else None"
    result=run(tmp_path,code,timeout=.25)
    assert result['outcome']=='timeout' and b'child started' in result['stderr']


def test_tail_redacts_credentials_before_utf8_byte_cap():
    from sniper.office.launcher_diagnostics import stderr_tail
    raw=('Error: TLS failed\n'+json.dumps({'accessToken':'secret-access','refresh_token':'secret-refresh','password':'secret-password'})+'\nAuthorization: Bearer secret-bearer\napi_key=secret-key\nhttps://user:secret-url@example.invalid/\nsk-ant-'+('A'*60)+'\neyJ'+('B'*60)+'\n').encode()
    clean=stderr_tail(raw)
    for secret in ('secret-access','secret-refresh','secret-password','secret-bearer','secret-key','secret-url','A'*60,'B'*60):assert secret not in clean
    assert 'TLS failed' in clean
    assert len(stderr_tail(('😀'*5000).encode()).encode())<=4096
    assert 'secret-partial' not in stderr_tail(b'secret-partial\nError: safe\n',truncated=True)


def test_persist_private_exclusive_file_and_refuse_symlink(tmp_path):
    from sniper.office.launcher_diagnostics import persist_stderr
    persist_stderr(tmp_path,'request',b'token=secret-value\nError: denied\n')
    path=tmp_path/'request.stderr.txt'
    assert path.stat().st_mode & 0o777==0o600 and 'secret-value' not in path.read_text()
    outside=tmp_path/'outside';outside.write_text('unchanged')
    (tmp_path/'other.stderr.txt').symlink_to(outside)
    with pytest.raises(FileExistsError):persist_stderr(tmp_path,'other',b'new')
    assert outside.read_text()=='unchanged'


@pytest.mark.parametrize('collision',[False,True])
def test_handle_persists_redacted_tail_without_changing_auth_or_completion(tmp_path,monkeypatch,collision,capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'scripts'))
    spec=importlib.util.spec_from_file_location('stderr_launcher',Path(__file__).resolve().parents[2]/'scripts/office_child_launcher.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    from office.test_child_launcher import request
    obj=request(tmp_path/'requests')
    cfg=dict(supervisor_cgroup='fixture',supervisor_script='fixture',clock_file='fixture',office_cgroup='fixture',guard_file='fixture',request_root=tmp_path/'requests',state_root=tmp_path/'state',child_root=tmp_path/'children')
    for name,fn in [('validate_peer',lambda *a,**k:'cookie'),('receive',lambda _:obj),('corrected_now',lambda *a,**k:100.),('caps_applied',lambda _:True),('external_guard',lambda _:None),('child_command',lambda *a,**k:['fixture'])]:monkeypatch.setattr(launcher,name,fn)
    monkeypatch.setattr(launcher,'subprocess',SimpleNamespace(run=lambda *a,**k:None))
    class Conn:
        def getsockopt(self,*a):return struct.pack('3i',123,os.getuid(),os.getgid())
    events=[]
    @contextmanager
    def auth(*a,**k):
        events.append('enter')
        try:yield
        finally:events.append('exit')
    target=cfg['state_root']/(obj['request_id']+'.stderr.txt')
    if collision:
        cfg['state_root'].mkdir(mode=0o700)
        outside=tmp_path/'outside';outside.write_text('unchanged');target.symlink_to(outside)
    result=launcher.handle(Conn(),cfg,execute=lambda *a,**k:dict(outcome='child_error',stdout=b'',stderr=b'api_key=secret-value\nPermissionError: denied\n',returncode=7),auth=auth)
    assert result['outcome']=='child_error' and result['exit_code']==7 and events==['enter','exit']
    if collision:
        assert outside.read_text()=='unchanged'
        assert 'stderr_write_failed' in capsys.readouterr().err
    else:
        assert target.is_file() and 'PermissionError: denied' in target.read_text()
        assert 'secret-value' not in target.read_text()


@pytest.mark.parametrize('raw,secrets',[
    (b'Authorization: Bearer short-secret',('short-secret',)),
    (b'Authorization: Basic dXNlcjpwYXNz',('dXNlcjpwYXNz',)),
    (b'Cookie: a=short-secret; b=another-secret',('short-secret','another-secret')),
    (b'[sdk] Authorization: Bearer short-secret',('short-secret',)),
])
def test_standalone_secret_headers_are_redacted_in_full(raw,secrets):
    from sniper.office.launcher_diagnostics import stderr_tail
    clean=stderr_tail(raw)
    assert all(s not in clean for s in secrets)
