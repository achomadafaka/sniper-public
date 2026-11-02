"""Offline host recovery protocol; no key generation, services or production files."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from sniper.office import planned_recovery as R

BOOT = '11111111-1111-1111-1111-111111111111'
NONCE = '22222222-2222-2222-2222-222222222222'
GUARD = dict(state='ready', reason=None, at=100., guard_policy_version='office-host-guard-pilot-v3')
PROCESS = dict(pid=123, birth_ticks=456, release_sha='b'*40)


def record(kind='funded_deploy', phase='armed'):
    return dict(version=1, kind=kind, nonce=NONCE, boot_id=BOOT, state_root='/srv/sniper',
                issued_boot=10., expires_boot=310., phase=phase, old_sha='a'*40, target_sha='b'*40,
                issuer=dict(pid=45, birth_ticks=67), pre_guard=GUARD,
                completed_boot=None if phase=='armed' else 20.,
                result=None if phase=='armed' else PROCESS)


def step(state, doc=None, **kw):
    args=dict(now_boot=10., now_host=100., boot_id=BOOT, state_root='/srv/sniper',
              latch=GUARD, healthy=True, reason=None, heartbeat_ts=99., process=PROCESS,
              issuer_alive=True, containment=True, intent_absent=True)
    args.update(kw)
    return R.advance(state, record() if doc is None else doc, **args)


def accepted(kind='funded_deploy'):
    s, ready=step(R.empty_state(BOOT), record(kind))
    assert s['status']=='accepted' and not ready
    return s


def finish(s, kind='funded_deploy', **kw):
    latch=dict(state='paused', reason='funded_heartbeat', at=105.) if kind=='funded_deploy' else GUARD
    a=dict(now_boot=20.,now_host=110.,latch=latch,heartbeat_ts=109.);a.update(kw)
    return step(s,record(kind,'completed'),**a)


def test_valid_funded_recovery_requires_five_seconds_and_two_advancing_beats():
    s=accepted();s,ready=finish(s);assert not ready
    s,ready=finish(s,now_boot=25.,now_host=115.,heartbeat_ts=114.)
    assert ready and s['status']=='consumed' and s['recovery_basis']=='planned_stop_verified'
    s,ready=finish(s,now_boot=26.,now_host=116.,heartbeat_ts=115.)
    assert not ready


def test_planned_office_stop_can_accept_completed_record_after_watchdog_was_stopped():
    s=R.empty_state(BOOT);doc=record('office_stop','completed')
    s,ready=step(s,doc,now_boot=20.,now_host=110.,latch=GUARD,heartbeat_ts=109.)
    assert s['status']=='accepted' and not ready
    s,ready=step(s,doc,now_boot=25.,now_host=115.,heartbeat_ts=114.)
    assert ready


@pytest.mark.parametrize('field,value', [('expires_boot',311.),('issued_boot',11.),('issued_boot',True),
    ('boot_id','wrong'),('state_root','/other'),('target_sha','short'),('nonce','bad'),('version',2)])
def test_malformed_stale_future_wrong_identity_fail_closed(field,value):
    doc=record();doc[field]=value
    s,ready=step(R.empty_state(BOOT),doc)
    assert not ready and s['status']!='accepted'


def test_exact_expiry_and_suspend_elapsed_cannot_extend_window():
    s=accepted();s,ready=finish(s,now_boot=310.,now_host=101.,heartbeat_ts=100.)
    assert not ready and s['status']=='disqualified'


@pytest.mark.parametrize('fault', ['issuer_dead','crash','abort','conflict','unrelated','unknown_containment',
                                  'wrong_process','intent','bad_heartbeat','unhealthy','missing_record'])
def test_mid_deploy_crashes_and_faults_permanently_disqualify(fault):
    s=accepted();doc=record('funded_deploy','completed');kw=dict(now_boot=20.,now_host=110.,
        latch=dict(state='paused',reason='funded_heartbeat',at=110.),heartbeat_ts=109.)
    if fault=='issuer_dead':doc=record();kw['issuer_alive']=False
    if fault=='crash':doc=record();kw.update(now_boot=311.,issuer_alive=False)
    if fault=='abort':doc['phase']='aborted';doc['completed_boot']=None;doc['result']=None
    if fault=='conflict':doc['nonce']='33333333-3333-3333-3333-333333333333'
    if fault=='unrelated':kw['reason']='cpu_pressure'
    if fault=='unknown_containment':kw['containment']=False
    if fault=='wrong_process':kw['process']={**PROCESS,'birth_ticks':999}
    if fault=='intent':kw['intent_absent']=False
    if fault=='bad_heartbeat':kw['heartbeat_ts']=111.
    if fault=='unhealthy':kw['healthy']=False
    if fault=='missing_record':doc={}
    s,ready=step(s,doc,**kw)
    assert not ready and s['status']=='disqualified'
    s,ready=finish(s,now_boot=30.,now_host=120.,heartbeat_ts=119.)
    assert not ready


def test_old_pause_and_modified_stale_latch_cannot_be_reclassified():
    for latch in [dict(state='paused',reason='operator',at=99.),{**GUARD,'at':98.},{}]:
        s,ready=step(R.empty_state(BOOT),record('office_stop','completed'),
                     now_boot=20.,now_host=110.,latch=latch,heartbeat_ts=109.)
        assert not ready and s['status']!='accepted'


def test_same_heartbeat_or_same_sample_is_not_recovery_progress():
    s=accepted();s,_=finish(s)
    s,ready=finish(s,now_boot=25.,now_host=115.,heartbeat_ts=109.)
    assert not ready


def test_lost_or_wrong_boot_state_does_not_restore_authority():
    with pytest.raises(ValueError):step({},record())
    s=accepted();s['boot_id']='other'
    with pytest.raises(ValueError):finish(s)


def test_nonextendable_identity_and_replay_watermark():
    s=accepted();doc=record();doc['expires_boot']=309.
    s,ready=step(s,doc);assert not ready and s['status']=='disqualified'
    s,ready=step(s,record());assert not ready


def test_public_signature_known_rfc8032_vector_and_forgery():
    # Published RFC8032 test 2. No private key is created or stored by this suite.
    pub=bytes.fromhex('3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c')
    sig=bytes.fromhex('92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0' '85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00')
    assert R.verify_signature(pub,b'\x72',sig)
    assert not R.verify_signature(pub,b'\x73',sig)
    assert not R.verify_signature(pub,b'\x72',bytes(64))


def test_signed_reader_refuses_bare_hash_forged_oversize_symlink_and_unknown_fields(tmp_path):
    p=tmp_path/'marker';p.write_text(json.dumps(dict(payload=record(),signature='00'*64)))
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())
    p.write_text(json.dumps(dict(payload=record(),sha256=hashlib.sha256(R.canonical(record())).hexdigest())))
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())
    p.write_bytes(b'x'*4097)
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())
    q=tmp_path/'link';q.symlink_to(p)
    with pytest.raises((OSError,ValueError)):R.read_record(q,bytes(32),expected_uid=os.getuid())


def test_consumption_is_durable_before_ready_and_write_failure_never_grants(tmp_path):
    store=R.Store(tmp_path/'state',BOOT)
    with pytest.raises(ValueError):store.run(record(),**RUNTIME) # explicit state initialization required
    store.initialize()
    assert not store.run(record(),**RUNTIME)
    args={**RUNTIME,'now_boot':20.,'now_host':110.,'heartbeat_ts':109.,
          'latch':dict(state='paused',reason='funded_heartbeat',at=110.)}
    assert not store.run(record(phase='completed'),**args)
    args.update(now_boot=25.,now_host=115.,heartbeat_ts=114.)
    assert store.run(record(phase='completed'),**args)
    assert json.loads((tmp_path/'state').read_text())['status']=='consumed'
    assert not store.run(record(phase='completed'),**args)


RUNTIME=dict(now_boot=10.,now_host=100.,boot_id=BOOT,state_root='/srv/sniper',latch=GUARD,
             healthy=True,reason=None,heartbeat_ts=99.,process=PROCESS,issuer_alive=True,
             containment=True,intent_absent=True)


def test_failed_durable_consume_returns_no_ready_and_can_retry(tmp_path, monkeypatch):
    store=R.Store(tmp_path/'state',BOOT);store.initialize();store.run(record(),**RUNTIME)
    args={**RUNTIME,'now_boot':20.,'now_host':110.,'heartbeat_ts':109.,
          'latch':dict(state='paused',reason='funded_heartbeat',at=110.)}
    store.run(record(phase='completed'),**args)
    args.update(now_boot=25.,now_host=115.,heartbeat_ts=114.)
    real=R.atomic
    monkeypatch.setattr(R,'atomic',lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):store.run(record(phase='completed'),**args)
    assert json.loads((tmp_path/'state').read_text())['status']=='accepted'
    monkeypatch.setattr(R,'atomic',real)
    assert store.run(record(phase='completed'),**args)


def test_two_processes_cannot_both_consume_the_same_nonce(tmp_path):
    import multiprocessing
    store=R.Store(tmp_path/'state',BOOT);store.initialize();store.run(record(),**RUNTIME)
    args={**RUNTIME,'now_boot':20.,'now_host':110.,'heartbeat_ts':109.,
          'latch':dict(state='paused',reason='funded_heartbeat',at=110.)}
    store.run(record(phase='completed'),**args)
    args.update(now_boot=25.,now_host=115.,heartbeat_ts=114.)
    ctx=multiprocessing.get_context('spawn');event=ctx.Event();q=ctx.Queue()
    procs=[ctx.Process(target=_consume_worker,args=(str(tmp_path/'state'),args,event,q)) for _ in range(2)]
    for p in procs:p.start()
    event.set()
    try:
        results=[q.get(timeout=3) for _ in procs]
        assert results.count(True)==1
    finally:
        for p in procs:
            p.join(3)
            if p.is_alive():p.kill();p.join()
    assert json.loads((tmp_path/'state').read_text())['status']=='consumed'


def test_reboot_and_corrupt_state_never_reset_watermark(tmp_path):
    store=R.Store(tmp_path/'state',BOOT);store.initialize();store.run(record(),**RUNTIME)
    with pytest.raises(FileExistsError):store.initialize()
    for raw in ['{}','[]','not json']:
        (tmp_path/'state').write_text(raw)
        with pytest.raises(ValueError):store.run(record(),**RUNTIME)


def test_unrelated_or_malformed_latch_cannot_be_cleared():
    s=accepted()
    for latch in [{},dict(state='ready',reason='unexpected'),dict(state='paused',reason='operator',at=110.)]:
        s2,ready=finish(s,latch=latch)
        assert not ready and s2['status']=='disqualified'


def test_office_stop_rejects_completion_that_changes_the_signed_ready_latch():
    s=accepted('office_stop')
    s,ready=finish(s,'office_stop',latch={**GUARD,'at':101.})
    assert not ready and s['status']=='disqualified'


def test_armed_office_record_requires_live_issuer_and_fresh_pre_stop_latch():
    s,ready=step(R.empty_state(BOOT),record('office_stop'),now_host=110.,issuer_alive=True)
    assert s['status']!='accepted' and not ready


def test_signed_json_duplicate_keys_and_unknown_fields_are_not_accepted(tmp_path,monkeypatch):
    p=tmp_path/'marker'
    monkeypatch.setattr(R,'verify_signature',lambda *a:True) # parsing boundary only, no test keys
    p.write_text('{"payload":{},"payload":{},"signature":"'+'00'*64+'"}')
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())
    p.write_text(json.dumps(dict(payload=record(),signature='00'*64,extra=True)))
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())
    p.write_text(json.dumps(dict(payload={**record(), 'extra':True},signature='00'*64)))
    with pytest.raises(ValueError):R.read_record(p,bytes(32),expected_uid=os.getuid())


def test_state_nonfinite_or_wrong_types_never_grant_authority():
    for field,value in [('watermark',float('nan')),('watermark',True),('last_sample',False),('beats',-1)]:
        s=R.empty_state(BOOT);s[field]=value
        with pytest.raises(ValueError):step(s)


def _consume_worker(path, args, event, q):
    event.wait(3)
    try:q.put(R.Store(path,BOOT).run(record(phase='completed'),**args))
    except BlockingIOError:q.put('locked')


def test_state_is_bounded_after_many_observations():
    s=accepted()
    for i in range(30):
        s,ready=step(s,record(),now_boot=10.+i,now_host=100.+i)
        assert not ready
    assert len(R.canonical(s))<4096


def test_completion_phase_cannot_revert_to_armed_and_restart_health_clock():
    s=accepted();s,_=finish(s)
    s,ready=step(s,record(),now_boot=21.,now_host=111.)
    assert not ready and s['status']=='disqualified'


def test_state_boolean_version_is_not_protocol_one():
    s=R.empty_state(BOOT);s['version']=True
    with pytest.raises(ValueError):step(s)


def test_wrong_file_owner_is_refused_before_signature_check(tmp_path,monkeypatch):
    import os
    p=tmp_path/'marker';p.write_text('{}')
    with pytest.raises(ValueError):R.read_regular(p,expected_uid=os.getuid()+1)


def test_unrelated_fault_sequence_change_permanently_disqualifies_accepted_window():
    armed=record();armed['pre_guard']={**GUARD,'unrelated_fault_seq':0}
    s,_=step(R.empty_state(BOOT),armed,latch=armed['pre_guard'])
    done={**armed,'phase':'completed','completed_boot':20.,'result':PROCESS}
    latch=dict(state='paused',reason='funded_heartbeat',at=110.,unrelated_fault_seq=1)
    s,ready=step(s,done,now_boot=20.,now_host=110.,heartbeat_ts=109.,latch=latch)
    assert not ready and s['status']=='disqualified'


def test_signed_boolean_fault_sequence_is_malformed_not_zero():
    armed=record();armed['pre_guard']={**GUARD,'unrelated_fault_seq':True}
    s,ready=step(R.empty_state(BOOT),armed,latch=armed['pre_guard'])
    assert not ready and s['status']!='accepted'
