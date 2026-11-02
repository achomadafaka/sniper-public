from contextlib import contextmanager
import hashlib
import json
import os
from uuid import uuid4
import pytest
from sniper.office import host_auth as H
from sniper.office.model_auth import AuthRefused


def fixture(tmp_path):
    config={k:str(tmp_path/k) for k in ('private_state','request_root','child_root','launcher_state','codex_home','codex_lock','claude_credentials')}
    config['claude_credentials']=str(tmp_path/'.claude/.credentials.json')
    config['model_roots']=[]
    config['supervisor_roots']=[]
    path=tmp_path/'config.json';path.write_text(json.dumps(config));raw=path.read_bytes()
    events=[]
    return config,path,hashlib.sha256(raw).hexdigest(),events


def test_hash_mismatch_and_overlapping_private_state_refuse(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    with pytest.raises(ValueError):H.Auth(path,expected_sha256='0'*64,completed=lambda _:True,clock=lambda:1000.,alert=lambda *a:None)
    cfg['private_state']=cfg['child_root']+'/private';path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):H.Auth(path,expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed=lambda _:True,clock=lambda:1000.,alert=lambda *a:None)


def test_unknown_completion_durably_pauses_across_factory_restart(tmp_path,monkeypatch):
    cfg,path,pin,events=fixture(tmp_path);calls=[]
    @contextmanager
    def snapshot(**kw):
        calls.append(kw)
        yield
        kw['alert']('codex_cleanup_unproven',kw['parent'].name)
        raise AuthRefused('codex_cleanup_unproven')
    monkeypatch.setattr(H.codex_auth,'snapshot',snapshot)
    def build():return H.Auth(path,expected_sha256=pin,completed=lambda _:True,clock=lambda:1000.,alert=lambda *a:events.append(a))
    parent=tmp_path/'child_root'/str(uuid4())
    with pytest.raises(AuthRefused,match='codex_cleanup_unproven'):
        with build()('codex_exec',parent,deadline_at=1062.):pass
    with pytest.raises(AuthRefused,match='provider_paused'):
        with build()('codex_exec',parent,deadline_at=1062.):pass
    assert len(calls)==1 and events[0][0]=='codex_cleanup_unproven'


def test_claude_gets_exact_call_deadline_and_current_corrected_clock(tmp_path,monkeypatch):
    cfg,path,pin,events=fixture(tmp_path);seen=[]
    @contextmanager
    def snapshot(source,parent,**kw):seen.append((source,parent,kw));yield
    monkeypatch.setattr(H,'claude_snapshot',snapshot)
    auth=H.Auth(path,expected_sha256=pin,completed=lambda _:True,clock=lambda:1001.,alert=lambda *a:None)
    parent=tmp_path/'child_root'/str(uuid4())
    with auth('claude_sdk',parent,deadline_at=1062.):pass
    assert seen[0][2]['now']==1001. and seen[0][2]['deadline_at']==1062.


def test_request_outside_pinned_root_refuses(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    auth=H.Auth(path,expected_sha256=pin,completed=lambda _:True,clock=lambda:1000.,alert=lambda *a:None)
    with pytest.raises(AuthRefused):
        with auth('codex_exec',tmp_path/'elsewhere'/str(uuid4()),deadline_at=1062.):pass


def test_private_auth_cannot_be_under_any_model_readonly_mount(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    cfg['model_roots']=[str(tmp_path/'release')]
    cfg['private_state']=str(tmp_path/'release'/'auth')
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='overlap'):
        H.Auth(path,expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed=lambda _:True,
               clock=lambda:1000.,alert=lambda *a:None)


def test_child_snapshot_is_prepared_directly_and_unknown_cleanup_pauses(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    real=tmp_path/'codex_home';real.mkdir()
    doc=dict(tokens=dict(account_id='fixture-account',id_token='a.eyJzdWIiOiJmaXh0dXJlIn0.x',access_token='fixture',refresh_token='fixture'),last_refresh='2026-10-28T00:00:00Z')
    (real/'auth.json').write_text(json.dumps(doc))
    auth=H.Auth(path,expected_sha256=pin,completed=lambda _:False,clock=lambda:1000.,alert=lambda *a:events.append(a))
    parent=tmp_path/'child_root'/str(uuid4())
    with pytest.raises(AuthRefused,match='codex_cleanup_unproven'):
        with auth('codex_exec',parent,deadline_at=1062.):
            assert (parent/'home/.codex/auth.json').exists()
            assert not (tmp_path/'request_root').exists()
    assert (tmp_path/'private_state/codex.paused.json').exists()
    assert json.loads((real/'auth.json').read_bytes())==doc


@pytest.mark.parametrize('field',['private_state','child_root'])
def test_supervisor_mounts_cannot_expose_auth_or_children(tmp_path,field):
    cfg,path,pin,events=fixture(tmp_path)
    cfg['supervisor_roots']=[str(tmp_path/'shared')]
    cfg[field]=str(tmp_path/'shared'/field)
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='overlap'):
        H.Auth(path,expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed=lambda _:True,
               clock=lambda:1000.,alert=lambda *a:None)


@pytest.mark.parametrize('mounts',['model_roots','supervisor_roots'])
@pytest.mark.parametrize('secret',['codex_home','claude_credentials'])
def test_real_credentials_cannot_be_exposed_through_declared_mounts(tmp_path,mounts,secret):
    cfg,path,pin,events=fixture(tmp_path)
    cfg[mounts]=[cfg[secret]];path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='overlap'):
        H.Auth(path,expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed=lambda _:True,
               clock=lambda:1000.,alert=lambda *a:None)


def test_host_claude_reader_refuses_every_other_file(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    cfg['claude_credentials']=str(tmp_path/'.claude/other.json');path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='claude_auth_source'):
        H.Auth(path,expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed=lambda _:True,
               clock=lambda:1000.,alert=lambda *a:None)


def test_each_claude_call_reads_atomic_host_refresh_without_writeback(tmp_path):
    cfg,path,pin,events=fixture(tmp_path)
    source=tmp_path/'.claude/.credentials.json';source.parent.mkdir()
    def data(token):return json.dumps(dict(claudeAiOauth=dict(accessToken=token,expiresAt=2000_000,refreshToken='fixture-refresh'))).encode()
    source.write_bytes(data('first'))
    auth=H.Auth(path,expected_sha256=pin,completed=lambda _:True,clock=lambda:1000.,alert=lambda *a:None)
    for token in ('first','refreshed'):
        if token=='refreshed':
            stage=source.with_name('new');stage.write_bytes(data(token));os.replace(stage,source)
        parent=tmp_path/'child_root'/str(uuid4())
        with auth('claude_sdk',parent,deadline_at=1062.):
            snapshot=json.loads((parent/'home/.claude/.credentials.json').read_bytes())
            assert snapshot['claudeAiOauth']['accessToken']==token
            assert 'refreshToken' not in snapshot['claudeAiOauth']
        assert source.read_bytes()==data(token)
