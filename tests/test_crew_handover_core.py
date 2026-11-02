"""No model or live session: durable handover with a fenced fake host."""
import importlib
from pathlib import Path

import pytest


def core():
    return importlib.import_module('tools.crew.handover')


def descriptor():
    return dict(key='office:head',session_id='old',incarnation='one',host_id='host',
        permissions_hash='a'*64,policy_hash='b'*64,model='test-model',worktree='/workspace',
        epoch_id='epoch',generation=1,sdk_version='verified-sdk',tainted_by_web=True,
        capabilities=dict(tool_fence=True,process_attribution=True,permissions_verified=True,
                          effect_lookup=True,mail_continuity=True))


class Host:
    def __init__(self):
        self.receipts={};self.calls=[];self.busy=False;self.unknown=None
        self.current='old';self.live={'old'};self.retired=[]
    def lookup(self,effect):
        return self.receipts.get(effect['effect_id'],{'state':'not_started'})
    def __getattr__(self,name):
        def effect(packet):
            assert core().owns_effect(packet)
            self.calls.append(name)
            if name=='safe_point' and self.busy:return {'state':'busy','no_effect':True,'reason':'owned_child'}
            if self.unknown==name:return {'state':'unknown','reason':'ambiguous'}
            value={}
            if name=='safe_point':value=dict(safe=True,fence_synced=True,complete=True,business_disabled_at=float(len(self.calls)+1))
            elif name=='request_checkpoint':value=dict(note_ref='sealed',note_hash='c'*64)
            elif name=='validate_checkpoint':value=dict(valid=True,complete=True,note_hash='c'*64,tainted_by_web=True)
            elif name=='park_old':value=dict(parked=True,tool_sandbox_empty=True,checkpoint_saved=True,inventory_state='known',position_open=True)
            elif name=='launch_candidate':
                self.live.add('new');value=dict(session_id='new',incarnation='two',dormant=True)
            elif name=='candidate_ready':value=dict(ready=True,session_id='new',incarnation='two',epoch_id='epoch',generation=1,model='test-model',sdk_version='verified-sdk',host_id='host',note_hash='c'*64,
                permissions_hash='a'*64,watermark=3,bootstrap_fill_pct=20)
            elif name=='activate_exact':self.current='new';value=dict(activated=True,session_id='new',old_tools_closed=True)
            elif name=='inbox_ack':value=dict(ack=True,session_id='new',watermark=3,continuity=True,business_enabled_at=float(len(self.calls)+1))
            elif name=='retire_old':self.live.discard('old');self.retired.append('old');value=dict(retired=True,owned_exit_proven=True)
            elif name=='abort_candidate':self.live.discard('new');value=dict(gone=True,never_acted=True)
            elif name=='restore_old':self.current='old';self.live.add('old');value=dict(restored=True,exact_checkpoint=True,business_enabled_at=float(len(self.calls)+1))
            elif name=='page':value=dict(sent=True)
            result=dict(state='done',value=value);self.receipts[packet['effect_id']]=result;return result
        return effect


def advance(engine,now=10.,n=20):
    record=None
    for i in range(n):
        record=engine.tick('office:head',now=now+i)
        if record['phase'] in ('retired_old','failed','recovery_unknown'):return record
    return record


def test_busy_child_defers_then_one_swap_preserves_identity_and_two_acks(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host)
    record=engine.begin(descriptor(),dict(event_id='event',reason='fill',window_id='w'),now=1.)
    host.busy=True
    assert engine.tick('office:head',now=2.)['phase']=='deferred_busy'
    assert host.current=='old' and 'launch_candidate' not in host.calls
    host.busy=False;record=advance(engine)
    assert record['phase']=='retired_old' and host.current=='new' and host.retired==['old']
    assert host.calls.index('inbox_ack')<host.calls.index('retire_old')
    assert record['descriptor']['generation']==1 and record['tainted_by_web'] is True
    assert record['position_open_at_park'] is True and record['unavailable_s']>0


def test_unknown_activation_ack_never_relaunches_or_blindly_restores(tmp_path):
    host=Host();host.unknown='inbox_ack';engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',reason='compact',window_id='w'),now=1.)
    record=advance(engine)
    assert record['phase']=='recovery_unknown' and host.current=='new'
    assert host.calls.count('launch_candidate')==1 and not host.retired and 'restore_old' not in host.calls
    assert record['business_allowed'] is False


def test_restart_reconciles_effect_and_fences_stale_owner(tmp_path):
    host=Host();root=tmp_path/'control';old=core().Coordinator(root,host)
    old.begin(descriptor(),dict(event_id='e',reason='compact',window_id='w'),now=1.)
    old.tick('office:head',now=2.)
    new=core().Coordinator(root,host);new.claim('office:head',now=3.)
    with pytest.raises(core().HandoverRefused,match='owner_changed'):old.tick('office:head',now=4.)
    assert advance(new,now=5.)['phase']=='retired_old'
    assert host.calls.count('launch_candidate')==1


def test_unverified_client_capability_stays_manual(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host);desc=descriptor()
    desc['capabilities']['process_attribution']=None
    with pytest.raises(core().HandoverRefused,match='capability'):
        engine.begin(desc,dict(event_id='e',reason='fill',window_id='w'),now=1.)
    assert host.calls==[]


def test_known_readiness_failure_reaps_candidate_and_restores_exact_old(tmp_path):
    host=Host();actual=host.__getattr__('candidate_ready')
    host.candidate_ready=lambda packet:{'state':'failed','no_effect':True,'reason':'readiness_rejected'}
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',reason='compact',window_id='w'),now=1.)
    record=advance(engine)
    assert record['phase']=='failed' and host.current=='old' and host.live=={'old'}
    assert 'abort_candidate' in host.calls and 'restore_old' in host.calls
    assert record['business_allowed'] is True


def test_ambiguous_failure_is_paged_once_and_page_failure_stays_visible(tmp_path):
    host=Host();host.unknown='launch_candidate'
    host.page=lambda packet:{'state':'failed','reason':'page_transport'}
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',reason='fill',window_id='w'),now=1.)
    record=advance(engine)
    assert record['phase']=='recovery_unknown' and record['page_state']=='failed'
    before=record['page_intent']['effect_id']
    assert engine.tick('office:head',now=100.)['page_intent']['effect_id']==before


def test_checkpoint_cannot_change_identity_or_unknown_taint_to_clean(tmp_path):
    host=Host();host.validate_checkpoint=lambda packet:dict(state='done',value=dict(valid=True,complete=True,
        note_hash='c'*64,tainted_by_web=False))
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',reason='compact',window_id='w'),now=1.)
    record=advance(engine)
    assert record['tainted_by_web'] is True and record['descriptor']['model']=='test-model'


def test_same_terminal_trigger_never_starts_a_second_swap(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host)
    trigger=dict(event_id='e',reason='compact',window_id='w')
    first=engine.begin(descriptor(),trigger,now=1.);advance(engine)
    second=engine.begin(descriptor(),trigger,now=4000.)
    assert first['handover_id']==second['handover_id']


def test_only_one_candidate_launch_in_flight_across_roles(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host)
    second=descriptor();second['key']='office:judge'
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    engine.begin(second,dict(event_id='j',window_id='w'),now=1.)
    actual=host.__getattr__('launch_candidate')
    host.launch_candidate=lambda packet:dict(state='pending')
    for now in range(2,8):engine.tick('office:head',now=now)
    for now in range(8,14):other=engine.tick('office:judge',now=now)
    assert other['phase']=='old_parked' and other['reason']=='host_launch_busy'
    assert host.calls.count('launch_candidate')==0
    assert engine.status('office:head')['phase']=='old_parked'


def test_expired_effect_is_not_started_after_coordinator_pause(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    actual=host.__getattr__('safe_point')
    host.safe_point=lambda packet:dict(state='pending')
    engine.tick('office:head',now=2.)
    host.safe_point=actual
    result=engine.tick('office:head',now=1000.)
    assert result['phase']=='recovery_unknown' and 'safe_point' not in host.calls


def test_memory_fallback_reaps_candidate_then_proves_old_gone_before_one_retry(tmp_path):
    host=Host();launch=host.__getattr__('launch_candidate');park=host.__getattr__('park_old');attempts=[]
    def pressure(packet):
        attempts.append(packet['effect_id'])
        if len(attempts)==1:return dict(state='failed',no_effect=True,reason='memory_pressure')
        assert host.calls.index('abort_candidate')<host.calls.index('old_harness_gone')
        assert packet['mode']=='memory_retry'
        return launch(packet)
    def stop(packet):
        result=park(packet)
        if packet['mode']=='memory_stop':
            host.calls.append('old_harness_gone');host.live.discard('old');result['value']['old_harness_gone']=True
        return result
    host.launch_candidate=pressure;host.park_old=stop
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    result=advance(engine,n=30)
    assert result['phase']=='retired_old' and len(attempts)==2 and len(set(attempts))==2


def test_real_owned_child_defers_without_wrapper_lease(tmp_path):
    import subprocess
    import sys
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)'])
    try:
        host=Host();actual=host.__getattr__('safe_point')
        def safe(packet):
            if child.poll() is None:return dict(state='busy',no_effect=True,reason='owned_descendant')
            return actual(packet)
        host.safe_point=safe;engine=core().Coordinator(tmp_path/'control',host)
        engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
        assert engine.tick('office:head',now=2.)['phase']=='deferred_busy'
        assert child.poll() is None and 'launch_candidate' not in host.calls
        child.wait(timeout=3)
        assert advance(engine)['phase']=='retired_old'
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=3)


@pytest.mark.parametrize('field,bad',[('incarnation','wrong'),('epoch_id','wrong'),('generation',2),('sdk_version','wrong'),('model','wrong'),('host_id','wrong')])
def test_candidate_readiness_requires_exact_host_and_policy_identity(tmp_path,field,bad):
    host=Host();ready=host.__getattr__('candidate_ready')
    def mismatched(packet):
        result=ready(packet);result['value'][field]=bad;return result
    host.candidate_ready=mismatched
    engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    result=advance(engine)
    assert result['phase']=='recovery_unknown' and host.current=='old' and 'activate_exact' not in host.calls


@pytest.mark.parametrize('method',['request_checkpoint','validate_checkpoint','park_old','launch_candidate','candidate_ready','activate_exact','inbox_ack','retire_old'])
def test_crash_after_effect_before_journal_reconciles_stable_id_without_reexecution(tmp_path,method):
    host=Host();root=tmp_path/'control';engine=core().Coordinator(root,host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    save=engine._save;crashed=[]
    def crash(record):
        if not crashed and record['effects'] and record['effects'][-1]['method']==method:
            crashed.append(True);raise KeyboardInterrupt('simulated coordinator crash')
        return save(record)
    engine._save=crash
    with pytest.raises(KeyboardInterrupt):advance(engine)
    engine._save=save
    recovered=core().Coordinator(root,host);recovered.claim('office:head',now=50.)
    assert advance(recovered,now=51.)['phase']=='retired_old'
    assert host.calls.count(method)==1
    assert host.calls.count('launch_candidate')==1 and host.retired==['old']


def test_parked_old_timeout_pages_and_freezes_without_starting_candidate(tmp_path):
    host=Host();engine=core().Coordinator(tmp_path/'control',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    for now in range(2,6):record=engine.tick('office:head',now=now)
    assert record['phase']=='old_parked'
    record=engine.tick('office:head',now=306.)
    assert record['phase']=='recovery_unknown' and record['reason']=='parked_old_timeout'
    assert record['page_state']=='sent' and host.calls.count('launch_candidate')==0


def test_completed_retention_keeps_two_latest_and_recent_and_never_unresolved(tmp_path):
    import json
    root=tmp_path/'control';host=Host();engine=core().Coordinator(root,host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    for i in range(5):
        record=dict(handover_id=f'old-{i}',descriptor=descriptor(),phase='retired_old',terminal_at=i+1)
        (root/f'old-{i}.completed.json').write_text(json.dumps(record));(root/f'old-{i}.completed.json').chmod(0o600)
    pending=dict(handover_id='unresolved',descriptor=descriptor(),phase='recovery_unknown',terminal_at=0)
    (root/'unresolved.completed.json').write_text(json.dumps(pending));(root/'unresolved.completed.json').chmod(0o600)
    assert engine.prune(now=8*86400)==3
    assert sorted(p.name for p in root.glob('*.completed.json'))==['old-3.completed.json','old-4.completed.json','unresolved.completed.json']
    assert engine.status('office:head')['phase']=='requested'


@pytest.mark.parametrize('value',[None,'60',float('nan'),float('inf'),True])
def test_malformed_policy_is_typed_refusal_before_any_effect(tmp_path,value):
    host=Host()
    with pytest.raises(core().HandoverRefused,match='invalid_policy'):
        core().Coordinator(tmp_path/'control',host,policy={'warn_pct':value})
    assert host.calls==[]
