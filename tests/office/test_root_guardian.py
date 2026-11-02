"""Root guardian policy fixtures only; no live cgroup, private key or service use."""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from sniper.office import planned_host as H
from sniper.office import root_guardian as G

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('root_guard_policy_fixture',ROOT/'scripts/office_watchdog.py')
W=importlib.util.module_from_spec(spec);spec.loader.exec_module(W)


def health(at,**over):
    value=dict(ts=at,funded_heartbeat_age_s=.1,cpu_psi_some_avg10=.01,io_psi_some_avg10=.01,
               mem_available_bytes=8*1024**3,base_caps_applied=True,roster_valid=True,
               resources_verified=True,guard_policy_version='office-host-guard-pilot-v3')
    value.update(over);return value


def fixture(tmp_path):
    public=tmp_path/'public';locks=tmp_path/'locks'
    public.mkdir(mode=0o755);locks.mkdir(mode=0o700)
    publisher=H.GuardPublisher(public,locks,owner_uid=os.getuid(),anchor=tmp_path)
    H.atomic(publisher.guard,dict(state='ready',reason=None,at=99.,revision=0,
                                guard_policy_version='office-host-guard-pilot-v3',unrelated_fault_seq=0))
    calls=[]
    guard=G.Guardian(publisher,W.PilotGuard(),terminate=lambda:calls.append('office-only'),
                     population=lambda:False,kill_available=lambda:True)
    return guard,publisher,calls


def test_normal_healthy_tick_refreshes_authoritative_guard_without_replay_state(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    out=guard.tick(health(100.),now=100.)
    assert out['state']=='ready' and out['revision']==1 and calls==[]
    assert not hasattr(pub,'private') and not hasattr(pub,'state')


def test_real_heartbeat_failure_still_pauses_no_marker_suppression(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    guard.population=lambda:True
    guard.tick(health(100.,funded_heartbeat_age_s=6.),now=100.)
    out=guard.tick(health(102.,funded_heartbeat_age_s=8.),now=102.)
    assert out['state']=='paused' and out['reason']=='funded_heartbeat'
    assert calls==['office-only'] and out['termination']['state']=='sent'


def test_pause_is_durable_before_termination_and_failed_kill_blocks_resume(tmp_path):
    guard,pub,calls=fixture(tmp_path);guard.population=lambda:True
    def fail():
        assert pub.read(pub.guard)['state']=='paused'
        raise OSError('fixture kill unavailable')
    guard.terminate=fail
    guard.tick(health(100.,funded_heartbeat_age_s=6.),now=100.)
    out=guard.tick(health(102.,funded_heartbeat_age_s=8.),now=102.)
    assert out['termination']['state']=='failed' and out['auto_resume_blocked']


def test_no_marker_normal_pause_still_requires_real_900_healthy_seconds(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    pub.pause('funded_heartbeat',now=99.)
    for at in range(100,1000,2):
        out=guard.tick(health(float(at)),now=float(at))
        assert out['state']=='paused' and not out['resume_eligible']
    out=guard.tick(health(1000.),now=1000.)
    assert out['state']=='ready' and out['recovery_basis']=='ordinary_900s'


def test_operator_pause_does_not_auto_clear_after_900_seconds(tmp_path):
    guard,pub,calls=fixture(tmp_path);pub.pause('operator',now=99.)
    for at in range(100,1002,2):out=guard.tick(health(float(at)),now=float(at))
    assert out['state']=='paused' and out['reason']=='operator'


def test_ready_stale_unknown_latch_does_not_get_forgiven_by_good_telemetry(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    out=guard.tick(health(110.),now=110.)
    assert out['state']=='paused' and out['reason']=='guard_stale' and out['auto_resume_blocked']


@pytest.mark.parametrize('fields',[{'telemetry_error':True},{'roster_valid':False},
                                  {'resources_verified':False},{'base_caps_applied':False}])
def test_unrelated_unknown_faults_keep_real_pause(tmp_path,fields):
    guard,pub,calls=fixture(tmp_path)
    assert guard.tick(health(100.,**fields),now=100.)['state']=='paused'


def test_root_service_ready_does_not_claim_900_after_adapter_fast_resume(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    H.atomic(pub.guard,dict(state='ready',reason=None,at=100.,revision=5,
          guard_policy_version='office-host-guard-pilot-v3',unrelated_fault_seq=0,recovery_basis='planned_stop_verified'))
    out=guard.tick(health(101.),now=101.)
    assert out['state']=='ready' and out['recovery_basis']=='planned_stop_verified'
    assert out['healthy_since']==101. and out['resume_eligible'] is False


def test_missing_authority_never_auto_initializes_ready(tmp_path):
    guard,pub,calls=fixture(tmp_path);pub.guard.unlink()
    out=guard.tick(health(100.),now=100.)
    assert out['state']=='paused' and out['reason']=='guard_unknown' and out['auto_resume_blocked']


def test_missing_kill_control_cannot_advertise_ready(tmp_path):
    guard,pub,calls=fixture(tmp_path);guard.kill_available=lambda:False
    out=guard.tick(health(100.),now=100.)
    assert out['state']=='paused' and out['reason']=='kill_control_unavailable'


def test_unknown_population_blocks_automatic_recovery(tmp_path):
    guard,pub,calls=fixture(tmp_path);pub.pause('funded_heartbeat',now=99.)
    guard.population=lambda:None
    out=guard.tick(health(100.),now=100.)
    assert out['auto_resume_blocked'] is True


@pytest.mark.parametrize('contradiction',[dict(auto_resume_blocked=True),dict(termination={'state':'failed'})])
def test_contradictory_ready_authority_is_not_sanitized_by_good_health(tmp_path,contradiction):
    guard,pub,calls=fixture(tmp_path)
    old=pub.read(pub.guard);old.update(contradiction);H.atomic(pub.guard,old)
    out=guard.tick(health(100.),now=100.)
    assert out['state']=='paused' and out['auto_resume_blocked'] is True
    assert out['reason']=='guard_unknown'


def test_transient_unrelated_fault_is_durable_even_if_adapter_misses_the_tick(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    guard.tick(health(100.),now=100.)
    old=pub.read(pub.guard);baseline=old['unrelated_fault_seq']
    guard.tick(health(101.,cpu_psi_some_avg10=.21),now=101.) # pressure debounce not due, but fast recovery must know
    out=guard.tick(health(102.),now=102.)
    assert out['unrelated_fault_seq']>baseline
    assert out['last_unrelated_fault']['reason']=='cpu_pressure'


def test_fault_sequence_survives_ordinary_ready_refresh_and_adapter_ready(tmp_path):
    guard,pub,calls=fixture(tmp_path)
    old=pub.read(pub.guard);old['unrelated_fault_seq']=7;H.atomic(pub.guard,old)
    out=guard.tick(health(100.),now=100.)
    assert out['unrelated_fault_seq']==7
