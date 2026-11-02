"""Synthetic app-server notifications only. No model/session/socket is started."""
import copy
import importlib
import json

import pytest

from tools.crew.handover_context import observe


def adapter():return importlib.import_module('tools.crew.handover_codex')


def binding():
    return dict(host_id='host',session_id='thread',incarnation='one',key='Codex',model='pinned-model',
                client_version='0.166.0',permissions_hash='a'*64)


def usage():
    return dict(method='thread/tokenUsage/updated',params=dict(threadId='thread',turnId='turn',tokenUsage=dict(
        modelContextWindow=1000,total={'inputTokens':999999},last=dict(inputTokens=750,cachedInputTokens=700,
        cacheWriteInputTokens=0,outputTokens=0,reasoningOutputTokens=0,totalTokens=750))))


def normalize(frame,**changes):
    args=dict(binding=binding(),caller=binding(),turn_id='turn',window_id='window',observed_at=10.,semantics_verified=True)
    args.update(changes)
    return adapter().normalize_notification(json.dumps(frame).encode() if isinstance(frame,dict) else frame,**args)


def test_latest_input_uses_common_threshold_without_cache_or_lifetime_double_count():
    result=normalize(usage());assert result['state']=='sample'
    sample=result['sample'];assert sample['input_tokens']==750
    assert observe(sample,expected=binding(),now=11)['fill_pct']==75


def test_schema_only_never_promotes_usage_semantics():
    result=normalize(usage(),semantics_verified=False)
    assert observe(result['sample'],expected=binding(),now=11)['state']=='unknown'


@pytest.mark.parametrize('field',['host_id','session_id','incarnation','key','model','client_version','permissions_hash'])
def test_wrong_host_caller_identity_is_unknown(field):
    caller=binding();caller[field]='wrong'
    assert normalize(usage(),caller=caller)['state']=='unknown'


@pytest.mark.parametrize('mutation',['thread','turn','version','window','missing','bool','negative','cache','above_window'])
def test_wrong_or_unknown_protocol_evidence_never_becomes_zero(mutation):
    frame=usage();args={}
    if mutation=='thread':frame['params']['threadId']='foreign'
    if mutation=='turn':frame['params']['turnId']='foreign'
    if mutation=='version':
        b=binding();b['client_version']='new';args.update(binding=b,caller=b)
    if mutation=='window':frame['params']['tokenUsage']['modelContextWindow']=None
    if mutation=='missing':del frame['params']['tokenUsage']['last']['inputTokens']
    if mutation=='bool':frame['params']['tokenUsage']['last']['inputTokens']=True
    if mutation=='negative':frame['params']['tokenUsage']['last']['inputTokens']=-1
    if mutation=='cache':frame['params']['tokenUsage']['last']['cachedInputTokens']=751
    if mutation=='above_window':frame['params']['tokenUsage']['last']['inputTokens']=1001
    assert normalize(frame,**args)['state']=='unknown'


@pytest.mark.parametrize('raw',[b'not json',b'{"method":"x","method":"y"}',b'{"a":NaN}',b'\xff',b' '*65537],ids=['bad-json','duplicate-key','nonfinite','bad-utf8','oversize'])
def test_bad_transport_frames_are_typed_unknown(raw):
    assert normalize(raw)['state']=='unknown'


@pytest.mark.parametrize('method,extra',[('hook/started',{'run':{'id':'compact','eventName':'preCompact'}}),
    ('hook/completed',{'run':{'id':'compact','eventName':'postCompact'}}),
    ('item/completed',{'item':{'id':'compact','type':'contextCompaction'}})])
def test_bound_compaction_signals_feed_common_observer(method,extra):
    result=normalize(dict(method=method,params=dict(threadId='thread',turnId='turn',**extra)))
    assert observe(result['sample'],expected=binding(),now=11)['state']=='compaction'


def test_prose_and_pretool_notification_are_not_compaction_or_deny_proof():
    result=normalize(dict(method='hook/started',params=dict(threadId='thread',turnId='turn',
        run={'id':'tool','eventName':'preToolUse','text':'preCompact now'})))
    assert result['state']=='irrelevant' and 'sample' not in result


def note_state():return dict(phase='note_pending',session_id='thread',incarnation='one',failed_note_turns=0)


def test_pending_writer_is_exact_and_generic_nested_tools_deny():
    allowed=[dict(tool='mcp__crew__checkpoint',arguments={'request_id':'host-pinned'})]
    kwargs=dict(binding=binding(),caller=binding(),allowlist=allowed)
    assert adapter().pending_tool_allowed(note_state(),tool=allowed[0]['tool'],arguments=allowed[0]['arguments'],**kwargs)
    for tool,args in [('shell',{'cmd':'write note'}),('functions.exec',{'code':'writer()'}),
                      ('mcp__crew__checkpoint',{'request_id':'wrong'})]:
        assert not adapter().pending_tool_allowed(note_state(),tool=tool,arguments=args,**kwargs)


def test_foreign_caller_cannot_write_or_finish_note_and_unknown_stays_parked():
    caller=binding();caller['incarnation']='old'
    assert not adapter().pending_tool_allowed(note_state(),binding=binding(),caller=caller,
        tool='writer',arguments={},allowlist=[dict(tool='writer',arguments={})])
    result=adapter().finish_note_turn(note_state(),binding=binding(),caller=caller,valid=True)
    assert result['phase']=='parked' and result['needs_page'] is True
    assert adapter().finish_note_turn(note_state(),binding=binding(),caller=binding(),valid=True)['phase']=='note_ready'


def test_replayed_compaction_is_latched_and_stale_usage_is_unknown(tmp_path):
    from tools.crew.handover_context import TriggerLatch
    frame=dict(method='item/completed',params=dict(threadId='thread',turnId='turn',
        item=dict(id='compact',type='contextCompaction')))
    value=normalize(frame)['sample'];latch=TriggerLatch(tmp_path/'latch.json')
    assert latch.observe(value,expected=binding(),now=11)['trigger'] is True
    assert TriggerLatch(tmp_path/'latch.json').observe(value,expected=binding(),now=12)['trigger'] is False
    assert observe(normalize(usage())['sample'],expected=binding(),now=200)['state']=='unknown'


def test_partial_or_two_frames_never_decode_as_a_valid_notification():
    packet=json.dumps(usage()).encode()
    assert normalize(packet[:-2])['state']=='unknown'
    assert normalize(packet+b'\n'+packet)['state']=='unknown'


def test_failed_note_completion_uses_common_two_turn_bound():
    state=note_state();original=copy.deepcopy(state)
    first=adapter().finish_note_turn(state,binding=binding(),caller=binding(),valid=False)
    assert state==original and first['phase']=='note_pending'
    second=adapter().finish_note_turn(first,binding=binding(),caller=binding(),valid=False)
    assert second['phase']=='parked' and second['needs_page'] is True


def test_late_bound_turn_is_distinct_without_requesting_a_page():
    frame=usage();frame['params']['turnId']='previous-turn'
    result=normalize(frame)
    assert result==dict(state='unknown',reason='codex_turn_mismatch',needs_page=False)
    frame['params']['threadId']='foreign-thread'
    assert normalize(frame)['needs_page'] is True


@pytest.mark.parametrize('status',['failed','blocked','completed'])
def test_compaction_sample_preserves_hook_status(status):
    frame=dict(method='hook/completed',params=dict(threadId='thread',turnId='turn',
        run=dict(id='compact',eventName='postCompact',status=status)))
    sample=normalize(frame)['sample']
    assert sample['compaction'] is True and sample['hook_status']==status
