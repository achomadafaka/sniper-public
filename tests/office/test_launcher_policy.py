import copy
import os
import subprocess
import sys

import pytest

from sniper.office import launcher_policy as P, roster as R, store as S
from .launcher_fixture import LauncherFixture
from .epoch_fixture import build


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path/'office.db')
    root = tmp_path/'config'
    nominee, refs = build(conn, root)
    launcher = LauncherFixture(root, lambda:1000.)
    monkeypatch.setattr(P, 'readonly_mount', lambda fd:True)  # Protocol-only; separate native bwrap witness uses the kernel.
    yield conn, root, nominee, refs, launcher
    launcher.close()
    conn.close()


def prepare(env, **over):
    _, root, _, refs, launcher = env
    args = dict(root=root, refs=refs, by='crew', clock=launcher.clock,
                launcher_policy=launcher.policy, probe_write=launcher.probe)
    args.update(over)
    return P.prepare(**args)


def test_policy_is_host_callable_not_request_mapping(env):
    data = env[4].fresh()
    with pytest.raises(P.Invalid, match='host'):
        prepare(env, launcher_policy=data)


@pytest.mark.parametrize('field,value', [('principal','head'),('approval_origin','repo'),
    ('office_uid',True),('office_gids',None),('issued_at',900.),('expires_at',999.),
    ('approved_full_guard_sha256','unmeasured'),('nonce',''),('extra','unknown')])
def test_malformed_unapproved_or_expired_policy_refuses(env, field, value):
    env[4].pinned = env[4].fresh()
    env[4].pinned[field] = value
    with pytest.raises(P.Invalid):
        prepare(env)


@pytest.mark.parametrize('answer', [True,None,0,'false'])
def test_probe_requires_literal_false(env, answer):
    with pytest.raises(P.Invalid, match='probe'):
        prepare(env, probe_write=lambda relative:answer)


def test_root_identity_and_actual_readonly_mount_required(env, monkeypatch):
    env[4].pinned = env[4].fresh()
    env[4].pinned['config_root_dev_ino'][1] += 1
    with pytest.raises(P.Invalid, match='identity'):
        prepare(env)
    env[4].pinned = None
    monkeypatch.setattr(P, 'readonly_mount', lambda fd:False)
    with pytest.raises(P.Invalid, match='readonly'):
        prepare(env)


def test_nonce_only_consumed_by_success_and_cannot_replay(env):
    conn, root, nominee, refs, launcher = env
    launcher.pinned = launcher.fresh()
    args = dict(clock=launcher.clock,hypothesis_id='fixture',by='crew',reason='freeze',
                nominee=nominee,nominee_refs=refs,config_root=root,
                launcher_policy=launcher.policy,probe_write=launcher.probe)
    with pytest.raises(R.OrgRefused):
        R._open_evaluated(conn, {}, **dict(args, nominee=dict(nominee,delta1=.5)))
    assert conn.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0
    eid = R._open_evaluated(conn, {}, **args)
    assert conn.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1
    with pytest.raises(R.OrgRefused, match='nonce_replayed'):
        R.verify_nominee(conn, epoch_id=eid,config_root=root,clock=launcher.clock,
                         launcher_policy=launcher.policy,probe_write=launcher.probe)


def test_each_verification_needs_a_fresh_policy(env):
    conn, root, nominee, refs, launcher = env
    eid = R._open_evaluated(conn, {},clock=launcher.clock,hypothesis_id='fixture',by='crew',reason='freeze',
                            nominee=nominee,nominee_refs=refs,config_root=root,
                            launcher_policy=launcher.policy,probe_write=launcher.probe)
    launcher.pinned = launcher.fresh()
    kw = dict(epoch_id=eid,config_root=root,clock=launcher.clock,launcher_policy=launcher.policy,probe_write=launcher.probe)
    R.verify_nominee(conn, **kw)
    with pytest.raises(R.OrgRefused, match='nonce_replayed'):
        R.verify_nominee(conn, **kw)


def test_self_owned_0444_does_not_prove_config_unwritable(env):
    env[4].pinned = env[4].fresh()
    env[4].pinned['office_uid'] = os.geteuid()
    with pytest.raises(P.Invalid, match='probe'):
        prepare(env, probe_write=lambda relative:True)


def test_wrong_approved_guard_cannot_open_epoch(env):
    conn, root, nominee, refs, launcher = env
    launcher.pinned = launcher.fresh()
    launcher.pinned['approved_full_guard_sha256'] = '0'*64
    with pytest.raises(R.OrgRefused, match='guard'):
        R._open_evaluated(conn, {}, clock=launcher.clock, hypothesis_id='fixture',
                         by='crew', reason='freeze', nominee=nominee,
                         nominee_refs=refs, config_root=root,
                         launcher_policy=launcher.policy, probe_write=launcher.probe)
    assert conn.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0


def test_callbacks_run_outside_write_transaction(env):
    conn, _, _, _, launcher = env
    def policy(by):
        assert not conn.in_transaction
        return launcher.policy(by)
    def probe(relative):
        assert not conn.in_transaction
        return launcher.probe(relative)
    authority = prepare(env, launcher_policy=policy, probe_write=probe)
    authority.close()


def test_native_readonly_mount_denies_fixture_writes(tmp_path):
    root = tmp_path/'config'
    root.mkdir()
    (root/'guard.json').write_text('{}')
    script = r'''
import errno, os, sys
from sniper.office.launcher_policy import readonly_mount
root = sys.argv[1]
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
assert readonly_mount(fd)
assert not os.access(root, os.W_OK)
for name, flags in [('guard.json', os.O_WRONLY), ('new', os.O_WRONLY | os.O_CREAT)]:
    try:
        child = os.open(name, flags, 0o600, dir_fd=fd)
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        os.close(child)
        raise AssertionError('read-only directory FD permitted write')
os.close(fd)
'''
    result = subprocess.run(['/usr/bin/bwrap', '--die-with-parent', '--unshare-net',
        '--ro-bind', '/', '/', '--bind', str(tmp_path), str(tmp_path),
        '--ro-bind', str(root), str(root), '--proc', '/proc', '--dev', '/dev',
        sys.executable, '-B', '-c', script, str(root)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (root/'guard.json').read_text() == '{}'
    assert not (root/'new').exists()


def test_total_probe_budget_refuses_without_nonce_write(env,monkeypatch):
    ticks=[0.]
    monkeypatch.setattr(P,'PROBE_BUDGET_S',30.)
    monkeypatch.setattr(P.time,'monotonic',lambda:ticks[0])
    def slow_probe(relative):
        ticks[0]+=31.
        return False
    with pytest.raises(P.Invalid,match='probe_budget'):
        prepare(env,probe_write=slow_probe)
    assert env[0].execute('SELECT count(*) FROM receipts').fetchone()[0]==0
