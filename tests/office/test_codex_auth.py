import json
from pathlib import Path
import pytest
from sniper.office import codex_auth as A
from sniper.office.model_auth import AuthRefused


def credentials(refresh):
    return dict(tokens=dict(account_id='fixture-account',id_token='a.eyJzdWIiOiJmaXh0dXJlIn0.x',
                            access_token='fixture-access',refresh_token=refresh),last_refresh='2026-10-28T00:00:00Z')


def setup(tmp_path,completed=lambda _:True):
    real=tmp_path/'real';real.mkdir();(real/'auth.json').write_text(json.dumps(credentials('old')))
    parent=tmp_path/'requests'/'request';parent.mkdir(parents=True)
    child=tmp_path/'children'/'request'/'home/.codex/auth.json'
    events=[]
    kw=dict(real_home=real,parent=parent,child_root=tmp_path/'children',private_state=tmp_path/'state',
            lock_path=tmp_path/'slot.lock',completed=completed,alert=lambda *a:events.append(a))
    return real,parent,child,events,kw


def test_codex_uses_reviewed_identity_guarded_writeback(tmp_path):
    real,parent,child,events,kw=setup(tmp_path)
    with A.snapshot(**kw):
        assert json.loads((parent/'home/.codex/auth.json').read_bytes())==credentials('old')
        child.parent.mkdir(parents=True);child.write_text(json.dumps(credentials('rotated')))
    assert json.loads((real/'auth.json').read_bytes())==credentials('rotated')
    assert not child.exists() and not (parent/'home/.codex/auth.json').exists()


def test_codex_never_overwrites_concurrent_real_rotation(tmp_path):
    real,parent,child,events,kw=setup(tmp_path)
    with A.snapshot(**kw):
        (real/'auth.json').write_text(json.dumps(credentials('other-process')))
        child.parent.mkdir(parents=True);child.write_text(json.dumps(credentials('child')))
    assert json.loads((real/'auth.json').read_bytes())==credentials('other-process')


def test_codex_unknown_cleanup_preserves_recovery_and_refuses_writeback(tmp_path):
    real,parent,child,events,kw=setup(tmp_path,completed=lambda _:False)
    with pytest.raises(AuthRefused,match='codex_cleanup_unproven'):
        with A.snapshot(**kw):
            child.parent.mkdir(parents=True);child.write_text(json.dumps(credentials('child')))
    assert json.loads((real/'auth.json').read_bytes())==credentials('old') and child.exists()
    assert list((tmp_path/'state').iterdir()) and events==[('codex_cleanup_unproven','request')]


def test_codex_changed_account_cannot_replace_host_login(tmp_path):
    real,parent,child,events,kw=setup(tmp_path)
    with pytest.raises(AuthRefused,match='codex_auth_writeback_refused'):
        with A.snapshot(**kw):
            changed=credentials('child');changed['tokens']['account_id']='other'
            child.parent.mkdir(parents=True);child.write_text(json.dumps(changed))
    assert json.loads((real/'auth.json').read_bytes())==credentials('old')
