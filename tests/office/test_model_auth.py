import json
from pathlib import Path
import pytest
from sniper.office import model_auth as A


def creds(path,expires):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'claudeAiOauth':{'accessToken':'fixture-token','expiresAt':expires}}))


@pytest.mark.parametrize('expires',[None,True,float('inf'),1000_000,1122_000])
def test_claude_near_expiry_or_unknown_refuses_before_snapshot(tmp_path,expires):
    source=tmp_path/'real'/'.credentials.json';creds(source,expires)
    target=tmp_path/'request'
    with pytest.raises(A.AuthRefused):
        with A.claude_snapshot(source,target,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None):
            pytest.fail('must not dispatch')
    assert not target.exists()


def test_claude_valid_snapshot_rotated_child_is_discarded_never_written_back(tmp_path):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000);original=source.read_bytes()
    parent=tmp_path/'request';child=tmp_path/'children'/'request'/'home'/'.claude'/'.credentials.json';events=[]
    with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:events.append(a)):
        snap=parent/'home'/'.claude'/'.credentials.json'
        assert snap.read_bytes()==original and snap.stat().st_mode&0o777==0o400
        child.parent.mkdir(parents=True);child.write_text('{"rotated":"fixture"}')
    assert source.read_bytes()==original and events==[('claude_auth_child_rotated','request')]
    assert not child.exists() and not snap.exists()


def test_child_auth_symlink_cannot_delete_host_credentials(tmp_path):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000);original=source.read_bytes()
    parent=tmp_path/'request';events=[]
    home=tmp_path/'children'/'request'/'home'
    with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:events.append(a)):
        home.mkdir(parents=True);(home/'.claude').symlink_to(source.parent,target_is_directory=True)
    assert source.exists() and source.read_bytes()==original
    assert events==[('claude_auth_child_rotated','request')]


def test_client_preserves_typed_claude_expiry_refusal(tmp_path):
    from sniper.office.launcher_client import Client
    source=tmp_path/'real'/'.credentials.json';creds(source,1000_000)
    def auth(adapter,parent):
        return A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None)
    c=Client(dict(request_root=str(tmp_path/'requests')),clock=lambda:1000.,auth=auth,verify_server=lambda *a:True)
    assert c.call('claude_sdk',{},deadline_at=1062.)['call_error']=='claude_auth_near_expiry'


@pytest.mark.parametrize('component',['home','.claude'])
def test_request_snapshot_parent_replacement_cannot_touch_external_credentials(tmp_path,component):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000)
    parent=tmp_path/'request';external=tmp_path/'external';creds(external/'.claude/.credentials.json',3000_000)
    original=(external/'.claude/.credentials.json').read_bytes()
    with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None):
        changed=parent/'home' if component=='home' else parent/'home/.claude'
        changed.rename(changed.with_name('original-'+changed.name))
        changed.symlink_to(external if component=='home' else external/'.claude',target_is_directory=True)
    assert (external/'.claude/.credentials.json').read_bytes()==original


@pytest.mark.parametrize('component',['home','.claude'])
def test_request_snapshot_creation_refuses_intermediate_symlink(tmp_path,component):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000)
    parent=tmp_path/'request';external=tmp_path/'external';external.mkdir();parent.mkdir()
    if component=='home':(parent/'home').symlink_to(external,target_is_directory=True)
    else:
        (parent/'home').mkdir();(parent/'home/.claude').symlink_to(external,target_is_directory=True)
    with pytest.raises(A.AuthRefused):
        with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None):
            pytest.fail('symlink must refuse')
    assert list(external.iterdir())==[]


def test_near_expiry_gate_prevents_even_launcher_connection(tmp_path,monkeypatch):
    import socket
    from sniper.office.launcher_client import Client
    source=tmp_path/'real'/'.credentials.json';creds(source,1122_000)
    def auth(adapter,parent):
        return A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None)
    def forbidden(*a,**kw):pytest.fail('near-expiry token must not reach a child or provider refresh')
    monkeypatch.setattr(socket,'socket',forbidden)
    c=Client(dict(request_root=str(tmp_path/'requests')),clock=lambda:1000.,auth=auth,verify_server=lambda *a:True)
    assert c.call('claude_sdk',{},deadline_at=1062.)['call_error']=='claude_auth_near_expiry'


def test_claude_snapshot_never_exposes_shared_refresh_token(tmp_path):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000)
    data=json.loads(source.read_bytes());data['claudeAiOauth']['refreshToken']='fixture-shared-refresh'
    source.write_text(json.dumps(data));original=source.read_bytes();parent=tmp_path/'request'
    with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None):
        snapshot=json.loads((parent/'home/.claude/.credentials.json').read_bytes())
        assert 'refreshToken' not in snapshot['claudeAiOauth']
        assert snapshot['claudeAiOauth']['accessToken']=='fixture-token'
    assert source.read_bytes()==original


def test_claude_snapshot_omits_unrelated_host_credentials(tmp_path):
    source=tmp_path/'real'/'.credentials.json';creds(source,2000_000)
    data=json.loads(source.read_bytes());data['mcpOAuth']={'secret':'fixture-other-login'}
    data['claudeAiOauth']['unknownSecret']='fixture-extra-secret'
    source.write_text(json.dumps(data));parent=tmp_path/'request'
    with A.claude_snapshot(source,parent,now=1000.,deadline_at=1062.,child_root=tmp_path/'children',alert=lambda *a:None):
        snapshot=json.loads((parent/'home/.claude/.credentials.json').read_bytes())
        assert set(snapshot)=={'claudeAiOauth'}
        assert set(snapshot['claudeAiOauth'])=={'accessToken','expiresAt'}
