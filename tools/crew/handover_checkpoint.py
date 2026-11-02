"""Host-verified checkpoint sealing. Returned prose is quoted data, never authority.

Sealed only through cut_after; later items require the host continuity queue.
The host replay layer must reconcile completed_unacked with action receipts.
secret_scan covers note bytes only: the host must separately scan referenced
notebook content before supplying verified_references hashes.
"""
import hashlib
import json
import re


class CheckpointRefused(ValueError):
    pass


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _ids(value):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x or len(x)>512 for x in value):
        raise CheckpointRefused('invalid_ids')
    if len(set(value))!=len(value):raise CheckpointRefused('duplicate_ids')
    return sorted(value)


def _inventory(value):
    if not isinstance(value,dict) or value.get('complete') is not True or value.get('pagination_complete') is not True:
        raise CheckpointRefused('inventory_incomplete')
    if not isinstance(value.get('cut_id'),str) or not value['cut_id']:
        raise CheckpointRefused('inventory_cut_unknown')
    # Host digest covers same-ID edits to orders/messages/actions, not just membership.
    if not isinstance(value.get('content_digest'),str) or not re.fullmatch('[0-9a-f]{64}',value['content_digest']):
        raise CheckpointRefused('inventory_content_unknown')
    tasks=value.get('tasks')
    if not isinstance(tasks,list):raise CheckpointRefused('tasks_unknown')
    normalized=[]
    for task in tasks:
        if (not isinstance(task,dict) or set(task)!= {'id','version','owner'}
                or any(not isinstance(task[k],str) or not task[k] for k in ('id','owner'))
                or type(task['version']) is not int or task['version']<0):
            raise CheckpointRefused('task_identity_unknown')
        normalized.append(dict(task))
    _ids([x['id'] for x in tasks])
    result=dict(tasks=sorted(normalized,key=lambda x:x['id']),content_digest=value['content_digest'])
    for key in ('operator_order_ids','pending_message_ids','pending_action_ids','verified_terminal_ids'):
        result[key]=_ids(value.get(key))
    return result


def validate_checkpoint(note, *, descriptor, inventory_before, inventory_after,
                        verified_references, secret_scan):
    """Inputs other than note must come from the trusted host, including full paging."""
    try:
        raw=_json(note)
        if len(raw)>128*1024:raise CheckpointRefused('note_too_large')
        if not isinstance(note,dict) or not isinstance(descriptor,dict):raise CheckpointRefused('note_invalid')
        for key in ('session_id','incarnation','host_id'):
            if not isinstance(descriptor.get(key),str) or not descriptor[key] or note.get(key)!=descriptor[key]:
                raise CheckpointRefused('note_identity_mismatch')
        before=_inventory(inventory_before);after=_inventory(inventory_after)
        if before!=after:raise CheckpointRefused('inventory_changed')
        for key in ('objective','sha','worktree','reviewer'):
            if not isinstance(note.get(key),str) or not note[key]:raise CheckpointRefused('note_field_missing')
        if not re.fullmatch('[0-9a-f]{40}',note['sha']):raise CheckpointRefused('sha_invalid')
        for key in ('limits','ownership','dirty_paths','blocked','completed_unacked','processes','provenance','notebook_refs'):
            if not isinstance(note.get(key),list):raise CheckpointRefused('note_field_missing')
        expected=dict(tasks=[x['id'] for x in before['tasks']],**{k:before[k] for k in ('operator_order_ids','pending_message_ids','pending_action_ids')})
        for key,ids in expected.items():
            if _ids(note.get(key))!=sorted(ids):raise CheckpointRefused('required_inventory_missing')
        refs=_ids(note['notebook_refs'])
        if not isinstance(verified_references,dict) or any(not isinstance(verified_references.get(ref),str) or not re.fullmatch('[0-9a-f]{64}',verified_references[ref]) for ref in refs):
            raise CheckpointRefused('reference_unverified')
        if not callable(secret_scan) or secret_scan(raw) is not False:raise CheckpointRefused('secret_scan_refused')
        result=dict(note=json.loads(raw),note_hash=hashlib.sha256(raw).hexdigest(),complete=True,
            quoted_data=True,tainted_by_web=descriptor.get('tainted_by_web') is not False or note.get('tainted_by_web') is not False,
            inventory=before,cut_before=inventory_before['cut_id'],cut_after=inventory_after['cut_id'],
            references={ref:verified_references[ref] for ref in refs})
        if len(_json(result))>1024**2:raise CheckpointRefused('envelope_too_large')
        return result
    except CheckpointRefused:raise
    except Exception:raise CheckpointRefused('checkpoint_validation_failed') from None
