import copy
import importlib
import pytest


def module():
    return importlib.import_module('tools.crew.handover_checkpoint')


def inputs():
    inventory=dict(complete=True,pagination_complete=True,cut_id='cut',content_digest='d'*64,
        tasks=[dict(id='task',version=1,owner='delta')],operator_order_ids=['order'],
        pending_message_ids=['message'],pending_action_ids=['action'],verified_terminal_ids=[])
    descriptor=dict(session_id='old',incarnation='one',host_id='host',tainted_by_web=True)
    note=dict(session_id='old',incarnation='one',host_id='host',objective='build',limits=['no money'],
        ownership=['core'],sha='a'*40,worktree='/work',dirty_paths=[],tasks=['task'],
        operator_order_ids=['order'],pending_message_ids=['message'],pending_action_ids=['action'],
        reviewer='Gamma',blocked=[],completed_unacked=[],processes=[],provenance=[],
        notebook_refs=['notes'],tainted_by_web=False)
    return dict(note=note,descriptor=descriptor,inventory_before=inventory,
        inventory_after=copy.deepcopy(inventory),verified_references={'notes':'b'*64},secret_scan=lambda raw:False)


def test_seals_complete_note_as_data_and_preserves_taint():
    args=inputs();result=module().validate_checkpoint(**args)
    assert result['tainted_by_web'] is True and len(result['note_hash'])==64
    assert result['note']==args['note'] and result['quoted_data'] is True
    args['note']['tasks'].clear()
    assert result['note']['tasks']==['task']


@pytest.mark.parametrize('mutation', ['paging','task_version','order_content','missing_order','secret','scan_error','reference','identity','oversize'])
def test_refuses_incomplete_changed_or_unsafe_checkpoint(mutation):
    args=inputs()
    if mutation=='paging':args['inventory_after']['pagination_complete']=False
    if mutation=='task_version':args['inventory_after']['tasks'][0]['version']=2
    if mutation=='order_content':args['inventory_after']['content_digest']='e'*64
    if mutation=='missing_order':args['note']['operator_order_ids']=[]
    if mutation=='secret':args['secret_scan']=lambda raw:True
    if mutation=='scan_error':args['secret_scan']=None
    if mutation=='reference':args['verified_references']={}
    if mutation=='identity':args['note']['session_id']='other'
    if mutation=='oversize':args['note']['objective']='x'*131072
    with pytest.raises(module().CheckpointRefused):module().validate_checkpoint(**args)


def test_inventory_cut_and_ordering_are_provenance_only():
    args=inputs();args['inventory_after']['cut_id']='new-cut'
    assert module().validate_checkpoint(**args)['complete'] is True


@pytest.mark.parametrize('host_taint',[False,None])
def test_note_taint_and_missing_host_taint_cannot_be_cleared(host_taint):
    args=inputs();args['note']['tainted_by_web']=True
    if host_taint is None:args['descriptor'].pop('tainted_by_web')
    else:args['descriptor']['tainted_by_web']=host_taint
    assert module().validate_checkpoint(**args)['tainted_by_web'] is True


def test_sealed_inventory_does_not_retain_caller_task_dictionaries():
    args = inputs()
    result = module().validate_checkpoint(**args)
    args['inventory_before']['tasks'][0]['version'] = 999
    args['inventory_before']['tasks'][0]['owner'] = 'other'
    args['inventory_after']['tasks'][0]['id'] = 'replacement'
    assert result['inventory']['tasks'] == [dict(id='task', version=1, owner='delta')]
