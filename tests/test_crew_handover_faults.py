"""Crash boundaries use only the fenced fake host; they are not live hook proof."""
import pytest
from test_crew_handover_core import Host, advance, core, descriptor


@pytest.mark.parametrize('method',['request_checkpoint','validate_checkpoint','park_old','launch_candidate','candidate_ready','activate_exact','inbox_ack','retire_old'])
def test_crash_before_effect_reuses_durable_intent_after_refence(tmp_path,method):
    host=Host();original=host.__getattr__(method);crashed=[]
    def before(packet):
        assert core().owns_effect(packet)
        if not crashed:
            crashed.append(packet['effect_id']);raise KeyboardInterrupt('before effect')
        assert packet['effect_id']==crashed[0]
        return original(packet)
    setattr(host,method,before)
    root=tmp_path/'controls';engine=core().Coordinator(root,host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    with pytest.raises(KeyboardInterrupt):advance(engine)
    recovered=core().Coordinator(root,host);recovered.claim('office:head',now=50.)
    assert advance(recovered,now=51.)['phase']=='retired_old'
    assert host.calls.count(method)==1 and host.calls.count('launch_candidate')==1


@pytest.mark.parametrize('method',['abort_candidate','restore_old'])
def test_cleanup_effect_receipt_survives_coordinator_crash(tmp_path,method):
    host=Host();host.candidate_ready=lambda packet:dict(state='failed',no_effect=True,reason='startup_rejected')
    root=tmp_path/'controls';engine=core().Coordinator(root,host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    save=engine._save;crashed=[]
    def after(record):
        if not crashed and record['effects'] and record['effects'][-1]['method']==method:
            crashed.append(True);raise KeyboardInterrupt('after cleanup')
        return save(record)
    engine._save=after
    with pytest.raises(KeyboardInterrupt):advance(engine)
    recovered=core().Coordinator(root,host);recovered.claim('office:head',now=50.)
    result=advance(recovered,now=51.)
    assert result['phase']=='failed' and result['business_allowed'] is True and host.current=='old'
    assert host.calls.count(method)==1 and host.calls.count('launch_candidate')==1


def test_unknown_candidate_cleanup_never_resumes_old_or_launches_again(tmp_path):
    host=Host();host.candidate_ready=lambda packet:dict(state='failed',no_effect=True,reason='startup_rejected')
    host.unknown='abort_candidate';engine=core().Coordinator(tmp_path/'controls',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    result=advance(engine)
    assert result['phase']=='recovery_unknown' and result['business_allowed'] is False
    assert 'restore_old' not in host.calls and host.calls.count('launch_candidate')==1


@pytest.mark.parametrize('outcome',['retired_old','failed','recovery_unknown'])
def test_terminal_timing_schema_never_omits_unknown_intervals(tmp_path,outcome):
    host=Host()
    if outcome=='failed':host.candidate_ready=lambda packet:dict(state='failed',no_effect=True,reason='startup_rejected')
    if outcome=='recovery_unknown':host.unknown='inbox_ack'
    engine=core().Coordinator(tmp_path/'controls',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    result=advance(engine)
    assert result['phase']==outcome
    for key in ('trigger_at','business_disabled_at','activated_at','restored_at','terminal_at','unavailable_started_at','unavailable_s'):
        assert key in result and (result[key] is None or core().number(result[key]))
    assert result['trigger_at'] is None and core().number(result['business_disabled_at'])


def test_missing_host_fence_timestamps_keep_decision_unavailability_unknown(tmp_path):
    host=Host()
    for method,field in [('safe_point','business_disabled_at'),('inbox_ack','business_enabled_at')]:
        original=host.__getattr__(method)
        def unstamped(packet,original=original,field=field):
            result=original(packet);result['value'].pop(field,None);return result
        setattr(host,method,unstamped)
    engine=core().Coordinator(tmp_path/'controls',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w',observed_at=0.5),now=1.)
    result=advance(engine)
    assert result['phase']=='retired_old' and result['trigger_at']==0.5
    assert result['business_disabled_at'] is None and result['business_enabled_at'] is None
    assert result['unavailable_s'] is None and result['parked_s']>0


def test_unknown_terminal_fences_late_effect_packet_while_page_remains_authorized(tmp_path):
    host=Host();packets=[]
    def lost_ack(packet):
        packets.append(packet);return dict(state='unknown',reason='lost_ack')
    host.inbox_ack=lost_ack;engine=core().Coordinator(tmp_path/'controls',host)
    engine.begin(descriptor(),dict(event_id='e',window_id='w'),now=1.)
    result=advance(engine)
    assert result['phase']=='recovery_unknown' and result['page_state']=='sent'
    assert core().owns_effect(packets[0]) is False
