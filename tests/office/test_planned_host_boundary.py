"""Unprivileged boundary fixtures; NOT root-isolation or live-service proof."""
import json
import os
from pathlib import Path
import threading

import pytest

from sniper.office import planned_host as H


def fixture(tmp_path):
    private=tmp_path/'private';public=tmp_path/'public'
    private.mkdir(mode=0o700);public.mkdir(mode=0o755)
    locks=tmp_path/'locks';locks.mkdir(mode=0o700)
    a=H.Authority(private,public,lock_dir=locks,owner_uid=os.getuid(),anchor=tmp_path)
    a.initialize(dict(status='accepted'),dict(state='paused',reason='funded_heartbeat',at=100.,revision=0))
    return a,private,public


def consume(state,guard):
    return dict(status='consumed'),dict(state='ready',reason=None,at=105.,recovery_basis='planned_stop_verified')


def test_default_root_boundary_refuses_same_uid_fixture(tmp_path):
    if os.getuid()==0:pytest.skip('unprivileged negative witness')
    private=tmp_path/'private';public=tmp_path/'public';private.mkdir();public.mkdir()
    with pytest.raises(ValueError):H.Authority(private,public,lock_dir=tmp_path,anchor=tmp_path)


def test_world_writable_ancestor_and_symlink_refuse(tmp_path):
    p=tmp_path/'p';p.mkdir(mode=0o777);p.chmod(0o777)
    with pytest.raises(ValueError):H.protected_directory(p,owner_uid=os.getuid(),anchor=tmp_path)
    p.chmod(0o700);q=tmp_path/'q';q.symlink_to(p)
    with pytest.raises((ValueError,OSError)):H.protected_directory(q,owner_uid=os.getuid(),anchor=tmp_path)


def test_completed_pause_during_unlocked_observation_cannot_be_overwritten(tmp_path):
    a,private,public=fixture(tmp_path)
    a.pause('operator',now=102.)
    with pytest.raises(ValueError,match='revision'):a.recover(0,consume)
    assert json.loads((public/'guard.json').read_text())['reason']=='operator'
    assert json.loads((private/'state.json').read_text())['status']=='accepted'


def test_pause_between_final_check_and_rename_is_excluded_then_remains_final(tmp_path,monkeypatch):
    a,private,public=fixture(tmp_path);checked=threading.Event();resume=threading.Event()
    real=H.atomic
    def stop_before_rename(path,value):
        if path.name=='guard.json' and value['state']=='ready':
            checked.set();assert resume.wait(2)
        real(path,value)
    monkeypatch.setattr(H,'atomic',stop_before_rename)
    errors=[]
    def recover():
        try:a.recover(0,consume)
        except BaseException as e:errors.append(e)
    t=threading.Thread(target=recover);t.start();assert checked.wait(2)
    # File lock is held across the entire final check/consume/publication section.
    with pytest.raises(BlockingIOError):a.pause('operator',now=106.)
    resume.set();t.join(3);assert not t.is_alive() and not errors
    a.pause('operator',now=106.)
    guard=json.loads((public/'guard.json').read_text())
    assert guard['state']=='paused' and guard['reason']=='operator' and guard['revision']==2


def test_crash_after_consume_never_publishes_and_replay_refuses(tmp_path,monkeypatch):
    a,private,public=fixture(tmp_path);real=H.atomic
    def crash(path,value):
        if path.name=='guard.json':raise OSError('simulated crash before publication')
        real(path,value)
    monkeypatch.setattr(H,'atomic',crash)
    with pytest.raises(OSError):a.recover(0,consume)
    assert json.loads((private/'state.json').read_text())['status']=='consumed'
    assert json.loads((public/'guard.json').read_text())['state']=='paused'
    monkeypatch.setattr(H,'atomic',real)
    with pytest.raises(ValueError,match='consumed'):a.recover(0,consume)


def test_missing_state_does_not_initialize_implicitly(tmp_path):
    a,private,public=fixture(tmp_path);(private/'state.json').unlink()
    with pytest.raises((OSError,ValueError)):a.recover(0,consume)
    assert json.loads((public/'guard.json').read_text())['state']=='paused'


def test_pause_reason_and_ready_payload_are_not_interchangeable(tmp_path):
    a,private,public=fixture(tmp_path)
    with pytest.raises(ValueError):a.pause('',now=102.)
    def forged(state,guard):return dict(status='accepted'),dict(state='ready',reason=None,at=105.)
    with pytest.raises(ValueError):a.recover(0,forged)
    assert json.loads((public/'guard.json').read_text())['state']=='paused'


def test_pause_that_commits_between_check_and_write_breaks_old_check_rename_model(tmp_path):
    """Deterministic RED witness of the rejected design, no concurrency timing needed."""
    a,private,public=fixture(tmp_path)
    old=a.read(a.guard)
    a.pause('operator',now=102.)
    H.atomic(a.guard,dict(old,state='ready',reason=None)) # old design: recheck had already happened
    assert a.read(a.guard)['state']=='ready'              # proves lost pause in the rejected algorithm
    a.pause('operator',now=103.)
    with pytest.raises(ValueError):a.recover(old['revision'],consume)
    assert a.read(a.guard)['reason']=='operator'



def test_authoritative_guard_is_public_readonly_while_state_stays_private(tmp_path):
    import stat
    a,private,public=fixture(tmp_path)
    assert stat.S_IMODE((public/'guard.json').stat().st_mode)==0o644
    assert stat.S_IMODE((private/'state.json').stat().st_mode)==0o600


def test_boolean_guard_revision_is_corrupt_not_revision_one(tmp_path):
    a,private,public=fixture(tmp_path)
    guard=a.read(a.guard);guard['revision']=True;H.atomic(a.guard,guard)
    with pytest.raises(ValueError):a.recover(1,consume)


def test_operator_pause_never_becomes_recovery_even_with_current_revision(tmp_path):
    a,private,public=fixture(tmp_path);a.pause('operator',now=102.)
    with pytest.raises(ValueError):a.recover(1,consume)
    assert a.read(a.guard)['reason']=='operator'


def test_private_and_public_trees_cannot_overlap(tmp_path):
    p=tmp_path/'p';p.mkdir(mode=0o700)
    q=p/'public';q.mkdir(mode=0o755)
    with pytest.raises(ValueError):H.Authority(p,q,lock_dir=tmp_path,owner_uid=os.getuid(),anchor=tmp_path)


def test_guard_publisher_has_no_replay_directory_dependency(tmp_path,monkeypatch):
    public=tmp_path/'public';locks=tmp_path/'locks'
    public.mkdir(mode=0o755);locks.mkdir(mode=0o700)
    H.atomic(public/'guard.json',dict(state='ready',reason=None,at=100.,revision=0))
    publisher=H.GuardPublisher(public,locks,owner_uid=os.getuid(),anchor=tmp_path)
    publisher.pause('funded_heartbeat',now=102.)
    assert not hasattr(publisher,'state') and not hasattr(publisher,'private')
    assert publisher.read(publisher.guard)['state']=='paused'


def test_guard_and_adapter_contend_on_same_external_lock(tmp_path):
    private=tmp_path/'private';public=tmp_path/'public';locks=tmp_path/'locks'
    for p in (private,public,locks):p.mkdir(mode=0o700)
    adapter=H.Authority(private,public,lock_dir=locks,owner_uid=os.getuid(),anchor=tmp_path)
    adapter.initialize(dict(status='accepted'),dict(state='paused',reason='funded_heartbeat',at=100.,revision=0))
    guardian=H.GuardPublisher(public,locks,owner_uid=os.getuid(),anchor=tmp_path)
    with adapter.locked():
        with pytest.raises(BlockingIOError):guardian.pause('operator',now=101.)
    guardian.pause('operator',now=102.)
    with pytest.raises(ValueError):adapter.recover(0,consume)


@pytest.mark.parametrize('flags',[dict(auto_resume_blocked=True),dict(termination={'state':'failed'})])
def test_adapter_also_refuses_contradictory_or_blocked_authority(tmp_path,flags):
    a,private,public=fixture(tmp_path);old=a.read(a.guard);old.update(flags);H.atomic(a.guard,old)
    with pytest.raises(ValueError):a.recover(0,consume)
    assert a.read(a.state)['status']=='accepted'
