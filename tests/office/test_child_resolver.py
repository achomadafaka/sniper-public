"""Rendered child sandbox DNS proof; no provider or user-manager calls."""
import json
from pathlib import Path
import subprocess
import time
import uuid

from sniper.office import child_launcher as C


def command(tmp_path,code):
    now=time.time();guard=tmp_path/'guard';guard.mkdir();scratch=tmp_path/'scratch';scratch.mkdir()
    (guard/'guard.json').write_text(json.dumps(dict(state='ready',reason=None,at=now,guard_policy_version='office-host-guard-pilot-v3')))
    request=C.Request('codex_exec',str(uuid.uuid4()),tmp_path/'request.json',now+10,b'{}','fixture')
    cfg=dict(guard_file=str(guard/'guard.json'),adapters={'codex_exec':dict(argv=['/usr/bin/python3','-c',code],ro_binds=[])})
    argv=C.child_command(cfg,request,scratch=scratch,now=now)
    return argv


def run(argv):
    return subprocess.run(['/usr/bin/ionice','-c','3',*argv[argv.index('/usr/bin/bwrap'):]],capture_output=True,text=True,timeout=10)


def test_rendered_child_resolver_exists_readonly_while_other_run_paths_stay_hidden(tmp_path):
    argv=command(tmp_path,"""from pathlib import Path
import os
p=Path('/etc/resolv.conf').resolve(strict=True)
assert p.is_file()
assert str(p).startswith('/run/systemd/resolve/')
assert os.statvfs(p).f_flag & os.ST_RDONLY
assert not Path('/run/user').exists()
assert not Path('/run/dbus').exists()
assert not Path('/run/lock').exists()
print('RESOLVER_VISIBLE_OTHER_RUN_MASKED')
""")
    got=run(argv)
    assert got.returncode==0,got.stdout+got.stderr
    assert 'RESOLVER_VISIBLE_OTHER_RUN_MASKED' in got.stdout
    bind=['--ro-bind','/run/systemd/resolve','/run/systemd/resolve']
    assert any(argv[i:i+3]==bind for i in range(len(argv)))
    assert argv.index('/run/systemd/resolve')>argv.index('/run')


def test_child_gate_refuses_missing_resolver_before_provider_exec(tmp_path):
    argv=command(tmp_path,"print('PROVIDER_ENTRYPOINT_RAN')")
    bind=['--ro-bind','/run/systemd/resolve','/run/systemd/resolve']
    for i in range(len(argv)):
        if argv[i:i+3]==bind:
            del argv[i:i+3];break
    got=run(argv)
    assert got.returncode!=0 and 'child_resolver_refused' in got.stderr
    assert 'PROVIDER_ENTRYPOINT_RAN' not in got.stdout
