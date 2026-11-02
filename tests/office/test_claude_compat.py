"""Pinned CLI startup checks: fake executables/version replies, no model or auth calls."""
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from sniper.office import claude_compat as C,model_child,runtime as R
from .test_model_child import request
from .test_runtime import configured

@pytest.fixture
def binary(tmp_path,monkeypatch):
    wheel=tmp_path/'fixture.whl';body=b'fixture reviewed binary'
    with zipfile.ZipFile(wheel,'w') as z:
        z.writestr(C.CLI_REL,body)
        z.writestr('claude_agent_sdk/__init__.py',"VERSION='0.2.174'\n")
        z.writestr(C.DIST_INFO+'/METADATA','Name: claude-agent-sdk\nVersion: 0.2.174\n')
    monkeypatch.setattr(C,'VENDOR_ROOT',tmp_path/'vendor')
    monkeypatch.setattr(C,'CLI_BYTES',len(body))
    monkeypatch.setattr(C,'CLI_SHA256',hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(C,'WHEEL_SHA256',hashlib.sha256(wheel.read_bytes()).hexdigest())
    C.install_wheel(wheel,C.VENDOR_ROOT)
    path=C.bundled_cli()
    calls=[]
    def run(argv,**kw):calls.append((argv,kw));return SimpleNamespace(returncode=0,stdout='2.1.310 (Claude Code)\n',stderr='')
    monkeypatch.setattr(C.subprocess,'run',run)
    return path,calls


def test_verified_cli_version_probe_has_no_auth_home_or_prompt(binary):
    path,calls=binary
    assert C.startup_refusal() is None
    argv,kw=calls[0]
    assert argv==[str(path),'--version'] and kw['timeout']<=3
    assert kw['env']['HOME']=='/nonexistent' and kw['env']['CLAUDE_CONFIG_DIR']=='/nonexistent'
    assert not any('TOKEN' in key or 'KEY' in key for key in kw['env'])
    assert kw['stdin']==C.subprocess.DEVNULL


@pytest.mark.parametrize('version,reason', [('2.1.209 (Claude Code)','claude_cli_too_old'),('garbage','claude_cli_version_unknown'),('2.1.280 (Claude Code)',None)])
def test_model_minimum_is_fail_closed(binary,monkeypatch,version,reason):
    monkeypatch.setattr(C.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=version,stderr=''))
    assert C.startup_refusal()==reason


def test_sdk_or_binary_mismatch_never_executes(binary,monkeypatch):
    path,calls=binary
    original=C.metadata.Distribution.at
    monkeypatch.setattr(C.metadata.Distribution,'at',lambda p:SimpleNamespace(metadata={'Name':'claude-agent-sdk'},version='0.2.118'))
    assert C.startup_refusal()=='claude_sdk_version_mismatch' and calls==[]
    monkeypatch.setattr(C.metadata.Distribution,'at',original)
    path.chmod(0o700);path.write_bytes(b'x'*C.CLI_BYTES)
    assert C.startup_refusal()=='claude_cli_hash_mismatch' and calls==[]


def test_child_refuses_unsupported_cli_before_dispatch(monkeypatch):
    monkeypatch.setattr(C,'startup_refusal',lambda:'claude_cli_too_old')
    calls=[]
    assert model_child.execute('claude_sdk',request(),dispatch=lambda d:calls.append(d))=={'call_error':'claude_cli_too_old'}
    assert calls==[]
    assert model_child.execute('codex_exec',request(),dispatch=lambda d:{'untouched':True})=={'untouched':True}


def test_sdk_options_use_verified_bundled_path_not_host_alias(binary):
    path,_=binary
    assert model_child.claude_options(request()).cli_path==str(path)


def test_supervisor_refuses_incompatible_sdk_before_state_creation(configured,monkeypatch):
    _,args,_=configured
    monkeypatch.setattr(C,'startup_refusal',lambda:'claude_cli_too_old')
    with pytest.raises(R.RuntimeRefused,match='claude_cli_too_old'):R.build(**args)
    assert not args['store_path'].exists()


@pytest.mark.parametrize('failure',[TimeoutError('fixture'),OSError('fixture')])
def test_missing_or_stalled_version_probe_refuses(binary,monkeypatch,failure):
    if isinstance(failure,TimeoutError):failure=C.subprocess.TimeoutExpired('claude-version',3)
    def fail(*a,**kw):raise failure
    monkeypatch.setattr(C.subprocess,'run',fail)
    assert C.startup_refusal()=='claude_cli_version_unknown'


def test_cli_symlink_is_not_admitted(binary):
    path,calls=binary
    real=path.with_name('real');path.rename(real);path.symlink_to(real)
    assert C.startup_refusal()=='claude_sdk_vendor_mismatch' and calls==[]


def test_cli_refusal_stops_model_turn_and_stays_typed():
    from sniper.office import models
    out=models.call('claude','claude-opus-5-5','s','u',deadline_s=5,clock=lambda:1000.,
        transport=lambda *a,**kw:{'call_error':'claude_cli_too_old'})
    assert out.outcome=='refused' and out.error_reason=='claude_cli_too_old'


def test_vendor_file_change_or_extra_file_refuses_before_exec(binary):
    path,calls=binary
    extra=C.VENDOR_ROOT/'unexpected.pth';extra.write_text('unapproved')
    assert C.startup_refusal()=='claude_sdk_vendor_mismatch' and calls==[]
    extra.unlink()
    module=C.VENDOR_ROOT/'claude_agent_sdk/__init__.py';module.chmod(0o600);module.write_text('wrong')
    assert C.startup_refusal()=='claude_sdk_vendor_mismatch' and calls==[]


def test_wheel_change_refuses_before_exec(binary):
    path,calls=binary
    wheel=C.VENDOR_ROOT/C.WHEEL_FILE;wheel.chmod(0o600);wheel.write_bytes(b'wrong')
    assert C.startup_refusal()=='claude_sdk_vendor_mismatch' and calls==[]


def test_install_is_idempotent_and_does_not_replace_foreign_tree(binary,tmp_path):
    path,calls=binary
    C.install_wheel(C.VENDOR_ROOT/C.WHEEL_FILE,C.VENDOR_ROOT)
    foreign=tmp_path/'foreign';foreign.mkdir();(foreign/'mine').write_text('preserve')
    with pytest.raises(ValueError):C.install_wheel(C.VENDOR_ROOT/C.WHEEL_FILE,foreign)
    assert (foreign/'mine').read_text()=='preserve'
