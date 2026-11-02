import importlib
import pytest


def detector():return importlib.import_module('tools.crew.handover_context')


def sample():
    return dict(host_id='h',session_id='s',incarnation='i',key='Delta',model='m',client_version='v',
        window_id='w',event_id='e',observed_at=10,provenance='host',semantics_verified=True,
        backend='codex',input_tokens=750,cache_read_tokens=600,window_tokens=1000)


@pytest.mark.parametrize('used,expected',[(599,'normal'),(600,'warn'),(749,'warn'),(750,'handover')])
def test_latest_input_thresholds_never_double_count_codex_cache(used,expected):
    value=sample();value['input_tokens']=used;value['lifetime_input_tokens']=999999
    assert detector().observe(value,expected=sample(),now=11)['state']==expected


def test_claude_adds_nonoverlapping_input_cache_fields():
    value=sample();value.update(backend='claude',input_tokens=50,cache_read_tokens=600,cache_create_tokens=100)
    assert detector().observe(value,expected=sample(),now=11)['fill_pct']==75


@pytest.mark.parametrize('field,value',[('window_tokens',0),('input_tokens',None),('input_tokens',True),('observed_at',-1),('observed_at',999),('semantics_verified',False),('session_id','wrong'),('client_version','wrong')])
def test_unknown_never_zero_fills(field,value):
    data=sample();data[field]=value
    assert detector().observe(data,expected=sample(),now=11)['state']=='unknown'


def test_compaction_latches_and_deduplicates_across_restart(tmp_path):
    latch=detector().TriggerLatch(tmp_path/'latch.json');value=sample()
    value.update(compaction=True,input_tokens=None)
    assert latch.observe(value,expected=sample(),now=11)['trigger'] is True
    value.update(compaction=False,input_tokens=1,event_id='post')
    assert detector().TriggerLatch(tmp_path/'latch.json').observe(value,expected=sample(),now=12)['trigger'] is False
    value['session_id']='replacement'
    assert latch.observe(value,expected=sample(),now=13)['state']=='unknown'


def test_unknown_recovery_is_one_bounded_swap_across_replacement_sessions(tmp_path):
    latch=detector().TriggerLatch(tmp_path/'latch.json');expected=sample()
    assert latch.unknown_usage(expected=expected,budget_known=False,initializing=False)['action']=='park'
    assert latch.unknown_usage(expected=expected,budget_known=True,initializing=True)['action']=='initializing'
    assert latch.unknown_usage(expected=expected,budget_known=True,initializing=False)['action']=='checkpoint_and_swap'
    expected['session_id']='replacement';expected['incarnation']='next'
    latch=detector().TriggerLatch(tmp_path/'latch.json')
    assert latch.unknown_usage(expected=expected,budget_known=True,initializing=False)['action']=='park'


def test_corrupt_trigger_latch_never_grants_another_recovery(tmp_path):
    path=tmp_path/'latch.json';path.write_text('{broken');path.chmod(0o600)
    latch=detector().TriggerLatch(path)
    assert latch.unknown_usage(expected=sample(),budget_known=True,initializing=False)['action']=='park'
    assert latch.observe(sample(),expected=sample(),now=11)['state']=='unknown'


@pytest.mark.parametrize('raw',['{}','{"latched":{}}','{"latched":[],"unknown_recovered":{}}'])
def test_malformed_latch_shape_refuses_recovery(tmp_path,raw):
    path=tmp_path/'latch.json';path.write_text(raw);path.chmod(0o600)
    latch=detector().TriggerLatch(path)
    assert latch.unknown_usage(expected=sample(),budget_known=True,initializing=False)['action']=='park'
