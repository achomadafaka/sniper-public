"""Tiny synthetic wheel through the real installer; all manager commands are fixture scripts."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
import pytest
from .test_pilot_activate import env,sha,SCRIPT

@pytest.fixture
def vendor_env(env):
    repo,bundle=env['repo'],env['bundle']
    wheel=bundle/'claude-sdk.whl';body=b"#!/bin/sh\nprintf '2.1.310 (Claude Code)\\n'\n"
    with zipfile.ZipFile(wheel,'w') as z:
        z.writestr('claude_agent_sdk/__init__.py',"VENDOR_MARKER='synthetic-fixture'\n")
        z.writestr('claude_agent_sdk/_bundled/claude',body)
        z.writestr('claude_agent_sdk-0.2.174.dist-info/METADATA','Name: claude-agent-sdk\nVersion: 0.2.174\n')
    source=(SCRIPT.parents[1]/'sniper/office/claude_compat.py').read_text()
    from sniper.office import claude_compat as C
    source=source.replace(C.WHEEL_SHA256,sha(wheel.read_bytes())).replace(C.CLI_SHA256,sha(body)).replace('CLI_BYTES=291638640','CLI_BYTES='+str(len(body)))
    helper=repo/'sniper/office/claude_compat.py';helper.write_text(source)
    (repo/'.gitignore').write_text('/vendor/claude_sdk/\n/vendor/.claude_sdk-*/\n')
    env['git']('add','--','sniper/office/claude_compat.py','.gitignore');env['git']('commit','-m','synthetic SDK artifact fixture')
    commit=env['git']('rev-parse','HEAD');release=repo/'.runtime/office/releases'/commit
    module={'__file__':str(helper),'__name__':'fixture_sdk'};exec(compile(source,str(helper),'exec'),module)
    old=str(env['release'])
    for name in ('runtime.json','launcher.json','monitor.json'):
        path=bundle/name;path.write_bytes(path.read_bytes().replace(old.encode(),str(release).encode()))
    path=bundle/'runtime.json';runtime=json.loads(path.read_bytes());runtime['claude_sdk']=module['runtime_pins'](release);path.write_text(json.dumps(runtime))
    for name in ('runtime','launcher'):(bundle/(name+'.sha256')).write_text(sha((bundle/(name+'.json')).read_bytes())+'\n')
    manifest=env['manifest'];manifest.update(main_sha=commit,release_root=str(release),claude_sdk=module['artifact_manifest']())
    manifest['files']={name:sha((bundle/name).read_bytes()) for name in manifest['files']}
    def run(*args):
        raw=json.dumps(manifest).encode();(bundle/'manifest.json').write_bytes(raw)
        return subprocess.run(['bash',str(SCRIPT),'--repo',str(repo),'--sha',commit,'--bundle',str(bundle),'--bundle-sha256',sha(raw),*args],
            env=env['child_env'],capture_output=True,text=True,timeout=20)
    env.update(run=run,release=release,sdk=module,vendor=release/'vendor/claude_sdk')
    return env


def test_vendor_dry_run_writes_nothing(vendor_env):
    e=vendor_env;p=e['run']()
    assert p.returncode==0,p.stdout+p.stderr
    assert not e['release'].exists() and not e['vendor'].exists()


def test_vendor_apply_idempotence_and_clean_rollback(vendor_env):
    e=vendor_env;p=e['run']('--apply')
    assert p.returncode==3,p.stdout+p.stderr
    assert e['sdk']['verify_vendor'](e['vendor']) is None
    assert (e['vendor']/'claude_agent_sdk/_bundled/claude').stat().st_mode&0o777==0o500
    assert e['run']('--apply').returncode==3
    rollback=e['run']('--rollback','--apply')
    assert rollback.returncode==0,rollback.stdout+rollback.stderr
    assert not e['release'].exists()
    assert e['run']('--rollback','--apply').returncode==0


def test_tampered_wheel_refuses_before_install(vendor_env):
    e=vendor_env;(e['bundle']/'claude-sdk.whl').write_bytes(b'wrong')
    assert e['run']('--apply').returncode==2
    assert not e['release'].exists()


def test_failed_enable_rolls_back_new_vendor(vendor_env):
    e=vendor_env;e['child_env']['FIXTURE_FAIL_ENABLE']='1'
    p=e['run']('--apply')
    assert p.returncode==2,p.stdout+p.stderr
    assert not e['release'].exists() and not e['vendor'].exists()
