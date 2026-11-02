"""Offline-capable shared handover coordinator; host adapters own every external effect.

No launcher, credentials, Office import or installed hooks. Unverified client
capabilities refuse automation. Adapters MUST honor deadlines, reconcile stable
IDs, and validate the durable owner fence atomically at the effect boundary.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from .crew_session_bridge import BridgeError, private, private_dir, read_json, write_json


class HandoverRefused(RuntimeError):
    pass


DEFAULTS = dict(warn_pct=60, handover_pct=75, telemetry_stale_s=120, busy_page_s=300,
    candidate_ready_timeout_s=120, activation_ack_timeout_s=60, min_swap_interval_s=1800,
    max_launch_attempts_per_hour=2, parked_old_timeout_s=300, poll_s=30,
    storage_cap_bytes=32*1024**2)
CAPABILITIES = ('tool_fence','process_attribution','permissions_verified','effect_lookup','mail_continuity')
ACTIONS = dict(requested='safe_point',deferred_busy='safe_point',note_requested='request_checkpoint',
    note_ready='validate_checkpoint',validated='park_old',old_parked='launch_candidate',
    candidate_starting='candidate_ready',candidate_ready='activate_exact',activating='inbox_ack',
    active_new='retire_old',aborting='abort_candidate',restoring='restore_old',fallback_stop='park_old')
NEXT = dict(safe_point='note_requested',request_checkpoint='note_ready',validate_checkpoint='validated',
    park_old='old_parked',launch_candidate='candidate_starting',candidate_ready='candidate_ready',
    activate_exact='activating',inbox_ack='active_new',retire_old='retired_old',
    abort_candidate='restoring',restore_old='failed')
TERMINAL = ('retired_old','failed','recovery_unknown')


def number(value):
    return type(value) in (int,float) and 0 <= value <= 2**53-1 and math.isfinite(value)


def identity(value):
    return isinstance(value,str) and 0 < len(value) <= 512 and not any(ord(c)<32 for c in value)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def owns_effect(packet):
    """Adapter check, not a substitute for its atomic lifecycle/socket fence."""
    try:
        path=Path(packet['journal_path'])
        if private(path).st_size>1024**2:return False
        record=read_json(path)
        effects=([record.get('page_intent')] if record.get('phase') in TERMINAL else
                 [record.get('intent'),record.get('sync_intent'),record.get('page_intent')])
        return (record['handover_id']==packet['handover_id'] and record['owner_token']==packet['owner_token']
                and any(e and e['effect_id']==packet['effect_id'] and e['method']==packet['method'] for e in effects))
    except (BridgeError,OSError,ValueError,KeyError,TypeError):
        return False


class Coordinator:
    """One transition per tick; no role lock is held across an adapter call.

    Adapter methods (spec section 4) receive one effect packet. lookup(packet)
    returns not_started, pending, done, busy(no_effect=True), failed or unknown.
    A restarted owner first refences via safe_point before reconciling old work.
    """
    def __init__(self,root,adapter,*,policy=None):
        self.root=private_dir(root);self.adapter=adapter;self.owner_token=str(uuid.uuid4())
        if policy is not None and (not isinstance(policy,dict) or set(policy)-set(DEFAULTS)):
            raise HandoverRefused('invalid_policy')
        self.policy=dict(DEFAULTS,**(policy or {}))
        if (any(not number(v) or v<=0 for v in self.policy.values())
                or not 0<self.policy['warn_pct']<self.policy['handover_pct']<100
                or type(self.policy['max_launch_attempts_per_hour']) is not int
                or not 1<=self.policy['max_launch_attempts_per_hour']<=2):
            raise HandoverRefused('invalid_policy')

    def _path(self,key):
        if not identity(key):raise HandoverRefused('invalid_key')
        return self.root/(hashlib.sha256(key.encode()).hexdigest()+'.json')

    @contextmanager
    def _lock(self,key):
        path=self._path(key).with_suffix('.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            private(path)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise HandoverRefused('coordinator_busy') from None
            yield
        finally:os.close(fd)

    def _read(self,key):
        path=self._path(key)
        if private(path).st_size>1024**2:raise HandoverRefused('control_record_too_large')
        return read_json(path)

    def _save(self,record):
        record['version']=record.get('version',0)+1
        raw=canonical(record)
        if len(raw)>1024**2:raise HandoverRefused('control_record_too_large')
        files=list(self.root.iterdir())
        if len(files)>4096:raise HandoverRefused('control_storage_capped')
        target=self._path(record['descriptor']['key'])
        used=sum(private(p).st_size for p in files if p.is_file() and p!=target)
        if used+len(raw)>self.policy['storage_cap_bytes']:raise HandoverRefused('control_storage_capped')
        write_json(target,record)

    def begin(self,descriptor,trigger,*,now):
        if (not isinstance(descriptor,dict) or not isinstance(trigger,dict) or not number(now)
                or not all(identity(descriptor.get(k)) for k in ('key','session_id','incarnation','host_id','permissions_hash','policy_hash','model','worktree'))
                or not identity(trigger.get('event_id')) or not identity(trigger.get('window_id'))):
            raise HandoverRefused('descriptor_invalid')
        if any((descriptor.get('capabilities') or {}).get(k) is not True for k in CAPABILITIES):
            raise HandoverRefused('client_capability_unverified_manual_only')
        key=descriptor['key']
        with self._lock(key):
            if self._path(key).exists():
                prior=self._read(key)
                if prior['descriptor']==descriptor and prior['trigger']==trigger:return prior
                if prior['phase'] not in TERMINAL:
                    if prior['descriptor']==descriptor and prior['trigger']==trigger:return prior
                    raise HandoverRefused('handover_pending')
                if prior['phase']=='recovery_unknown':raise HandoverRefused('unresolved_handover')
                if now-prior.get('terminal_at',now)<self.policy['min_swap_interval_s']:
                    raise HandoverRefused('handover_interval_limited')
                archive=self.root/(prior['handover_id']+'.completed.json')
                if not archive.exists():write_json(archive,prior)
                history=prior.get('launch_history',[])
            else:history=[]
            record=dict(handover_id=str(uuid.uuid4()),owner_token=self.owner_token,owner_synced=True,
                descriptor=json.loads(canonical(descriptor)),trigger=trigger,phase='requested',requested_at=now,
                business_allowed=False,tainted_by_web=descriptor.get('tainted_by_web') is not False,
                intent=None,effects=[],launch_history=history,needs_page=False,page_intent=None,
                trigger_at=trigger.get('observed_at') if number(trigger.get('observed_at')) else None,
                business_disabled_at=None,business_enabled_at=None,activated_at=None,restored_at=None,terminal_at=None,
                unavailable_started_at=None,unavailable_s=None,parked_s=None,position_open_at_park=None,
                unavailable_basis='host_business_fence_to_enable; unknown without both stamps')
            self._save(record);return record

    def claim(self,key,*,now):
        if not number(now):raise HandoverRefused('clock_unknown')
        with self._lock(key):
            record=self._read(key)
            if record['phase'] in TERMINAL:raise HandoverRefused('handover_terminal')
            record.update(previous_owner=record['owner_token'],owner_token=self.owner_token,
                          owner_synced=False,sync_intent=None)
            self._save(record);return record

    def status(self,key):return self._read(key)

    def prune(self,*,now):
        """Off-path archive cleanup. Current and unresolved controls never expire."""
        if not number(now):raise HandoverRefused('clock_unknown')
        with self._lock('retention'):
            paths=list(self.root.glob('*.completed.json'))
            if len(paths)>4096:raise HandoverRefused('retention_read_capped')
            grouped={}
            for path in paths:
                if private(path).st_size>1024**2:raise HandoverRefused('control_record_too_large')
                record=read_json(path)
                if record.get('phase') not in ('retired_old','failed'):continue
                key=record.get('descriptor',{}).get('key');at=record.get('terminal_at')
                if not identity(key) or not number(at):raise HandoverRefused('archive_identity_unknown')
                grouped.setdefault(key,[]).append((at,str(path)))
            remove=[]
            for records in grouped.values():
                remove.extend(Path(path) for at,path in sorted(records,reverse=True)[2:] if now-at>7*86400)
            for path in remove:path.unlink()
            if remove:
                fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            return len(remove)

    def _packet(self,record,intent):
        return dict(intent,handover_id=record['handover_id'],owner_token=record['owner_token'],
            journal_path=str(self._path(record['descriptor']['key'])),descriptor=record['descriptor'],
            checkpoint=record.get('checkpoint'),candidate=record.get('candidate'),
            verified=record.get('verified'),tainted_by_web=record['tainted_by_web'],
            previous_owner=record.get('previous_owner'),mode=('refence' if intent['method']=='safe_point' and not record['owner_synced'] else 'memory_stop' if record['phase']=='fallback_stop' else 'memory_retry' if intent['method']=='launch_candidate' and record.get('fallback_used') else 'normal'))

    def _valid(self,method,value,record):
        if not isinstance(value,dict):return False
        required=dict(safe_point=('safe','fence_synced','complete'),validate_checkpoint=('valid','complete'),
            park_old=('parked','tool_sandbox_empty','checkpoint_saved'),launch_candidate=('dormant',),
            candidate_ready=('ready',),activate_exact=('activated','old_tools_closed'),
            inbox_ack=('ack','continuity'),retire_old=('retired','owned_exit_proven'),
            abort_candidate=('gone','never_acted'),restore_old=('restored','exact_checkpoint'))
        if any(value.get(k) is not True for k in required.get(method,())):return False
        if method=='park_old' and record['phase']=='fallback_stop' and value.get('old_harness_gone') is not True:return False
        if method=='request_checkpoint':return identity(value.get('note_ref')) and identity(value.get('note_hash'))
        if method=='validate_checkpoint':return value.get('note_hash')==record['checkpoint']['note_hash']
        if method=='launch_candidate':
            return all(identity(value.get(k)) and value[k]!=record['descriptor'][k] for k in ('session_id','incarnation'))
        if method in ('candidate_ready','activate_exact','inbox_ack'):
            if value.get('session_id')!=record['candidate']['session_id']:return False
        if method=='candidate_ready':
            if value.get('incarnation')!=record['candidate']['incarnation']:return False
            if any(value.get(k)!=record['descriptor'].get(k) for k in ('host_id','epoch_id','generation','model','sdk_version')):return False
            return (value.get('note_hash')==record['verified']['note_hash']
                and value.get('permissions_hash')==record['descriptor']['permissions_hash']
                and type(value.get('watermark')) is int and value['watermark']>=0
                and number(value.get('bootstrap_fill_pct')) and value['bootstrap_fill_pct']<self.policy['warn_pct'])
        if method=='inbox_ack':return type(value.get('watermark')) is int and value['watermark']>=record['ready']['watermark']
        return True

    def _launch_slot(self,record,*,release=False):
        # A crashed/unknown launch retains its slot until that handover proves cleanup.
        with self._lock('host-launch-slot'):
            path=self.root/'host-launch-slot.json'
            slot=read_json(path) if path.exists() else None
            if slot and slot.get('handover_id')!=record['handover_id']:return False
            if release:
                if slot:write_json(path,{})
            elif not slot:write_json(path,dict(handover_id=record['handover_id'],host_id=record['descriptor']['host_id']))
            return True

    def _tick(self,key,*,now):
        if not number(now):raise HandoverRefused('clock_unknown')
        with self._lock(key):
            record=self._read(key)
            if record['owner_token']!=self.owner_token:raise HandoverRefused('owner_changed')
            if record['phase'] in TERMINAL:return record
            if (record.get('unavailable_started_at') is not None and not record['business_allowed']
                    and now-record['unavailable_started_at']>self.policy['parked_old_timeout_s']):
                record.update(phase='recovery_unknown',reason='parked_old_timeout',needs_page=True,terminal_at=now)
                self._save(record);return record
            sync=not record['owner_synced']
            method='safe_point' if sync else ACTIONS[record['phase']]
            field='sync_intent' if sync else 'intent'
            intent=record.get(field)
            if not intent or intent['method']!=method:
                deadline=now+(self.policy['activation_ack_timeout_s'] if method=='inbox_ack' else self.policy['candidate_ready_timeout_s'])
                intent=dict(effect_id=str(uuid.uuid4()),method=method,deadline=deadline,attempts=0)
                record[field]=intent
            if method=='launch_candidate' and not self._launch_slot(record):
                record['reason']='host_launch_busy'
                self._save(record);return record
            if method=='launch_candidate' and intent['attempts']==0:
                history=[at for at in record['launch_history'] if 0<=now-at<3600]
                if len(history)>=self.policy['max_launch_attempts_per_hour']:
                    self._launch_slot(record,release=True)
                    record.update(phase='recovery_unknown',business_allowed=False,reason='launch_rate_limited',needs_page=True,terminal_at=now)
                    self._save(record);return record
                record['launch_history']=history+[now]
            self._save(record);packet=self._packet(record,intent)
        try:
            result=self.adapter.lookup(packet)
            if not isinstance(result,dict):raise ValueError('invalid_effect_result')
            if result.get('state') in ('not_started','busy'):
                if now>intent['deadline']:
                    result=dict(state='unknown',reason='effect_deadline_expired')
                elif result.get('state')=='busy' and result.get('no_effect') is not True:
                    result=dict(state='unknown',reason='busy_effect_unproven')
                else:
                    with self._lock(key):
                        current=self._read(key)
                        if current['owner_token']!=self.owner_token:raise HandoverRefused('owner_changed')
                        current[field]['attempts']+=1;self._save(current)
                    result=getattr(self.adapter,method)(packet)
            canonical(result)
        except HandoverRefused:raise
        except Exception as exc:result=dict(state='unknown',reason='dependency_'+type(exc).__name__)
        with self._lock(key):
            record=self._read(key)
            if record['owner_token']!=self.owner_token:raise HandoverRefused('owner_changed')
            intent=record[field]
            state=result.get('state') if isinstance(result,dict) else 'unknown'
            if state=='busy' and result.get('no_effect') is True:
                record['reason']=str(result.get('reason') or 'busy')[:80]
                record['deferrals']=record.get('deferrals',0)+1
                if not sync and method=='safe_point':record['phase']='deferred_busy'
                if now-record['requested_at']>=self.policy['busy_page_s']:record['needs_page']=True
                self._save(record);return record
            if state=='pending' and now<=intent['deadline']:
                self._save(record);return record
            if state=='failed' and result.get('no_effect') is True and record.get('verified') and method not in ('activate_exact','inbox_ack','retire_old','restore_old','abort_candidate'):
                if method in ('launch_candidate','candidate_ready') and result.get('reason')=='memory_pressure' and not record.get('fallback_used'):
                    record['fallback_requested']=True
                record['effects'].append(dict(intent,result=result));record[field]=None
                record.update(phase='aborting',reason=str(result.get('reason') or 'effect_failed')[:80],needs_page=True,business_allowed=False)
                self._save(record);return record
            if state!='done' or not self._valid(method,result.get('value'),record):
                record.update(phase='recovery_unknown',reason=str(result.get('reason') or 'effect_or_proof_unknown')[:80],
                              needs_page=True,business_allowed=False,terminal_at=now)
                self._save(record);return record
            if method in ('candidate_ready','abort_candidate'):
                self._launch_slot(record,release=True)
            value=result['value'];record['effects'].append(dict(intent,result=result))
            record[field]=None
            if sync:
                record['owner_synced']=True
            else:
                if method=='safe_point' and record['business_disabled_at'] is None:
                    at=value.get('business_disabled_at')
                    record['business_disabled_at']=at if number(at) else None
                previous_phase=record['phase']
                record['phase']=NEXT[method]
                if method=='abort_candidate' and record.pop('fallback_requested',False):
                    record.update(phase='fallback_stop',fallback_used=True)
                if method=='request_checkpoint':record['checkpoint']=value
                elif method=='validate_checkpoint':
                    record['verified']=value;record['tainted_by_web'] |= value.get('tainted_by_web') is not False
                elif method=='park_old':
                    if previous_phase!='fallback_stop':record['unavailable_started_at']=now
                    record['position_open_at_park']=value.get('position_open') if value.get('inventory_state')=='known' and type(value.get('position_open')) is bool else None
                elif method=='launch_candidate':record['candidate']=value
                elif method=='candidate_ready':record['ready']=value
                elif method=='inbox_ack':
                    record['business_allowed']=True
                    record['parked_s']=now-record['unavailable_started_at']
                    at=value.get('business_enabled_at')
                    record['business_enabled_at']=at if number(at) else None
                    start=record['business_disabled_at']
                    record['unavailable_s']=at-start if number(at) and number(start) and at>=start else None
                    record['activated_at']=now
                elif method in ('retire_old','restore_old'):
                    record['terminal_at']=now
                    if method=='restore_old':
                        record['business_allowed']=True
                        record['restored_at']=now
                        record['parked_s']=now-record['unavailable_started_at'] if record['unavailable_started_at'] is not None else None
                        at=value.get('business_enabled_at');start=record['business_disabled_at']
                        record['business_enabled_at']=at if number(at) else None
                        record['unavailable_s']=at-start if number(at) and number(start) and at>=start else None
            self._save(record);return record

    def _page(self,key,*,now):
        with self._lock(key):
            record=self._read(key)
            if record['owner_token']!=self.owner_token:raise HandoverRefused('owner_changed')
            if not record['needs_page'] or record.get('page_state') in ('sent','failed','unknown'):return record
            if record.get('page_intent') is None:
                record['page_intent']=dict(effect_id=str(uuid.uuid4()),method='page',deadline=now+self.policy['poll_s'])
            self._save(record);packet=self._packet(record,record['page_intent'])
        try:
            result=self.adapter.lookup(packet)
            if result.get('state')=='not_started':result=self.adapter.page(packet)
            state='sent' if result.get('state')=='done' and (result.get('value') or {}).get('sent') is True else 'failed' if result.get('state')=='failed' else 'unknown'
        except Exception:state='unknown'
        with self._lock(key):
            record=self._read(key)
            if record['owner_token']!=self.owner_token:raise HandoverRefused('owner_changed')
            record['page_state']=state;self._save(record);return record

    def tick(self,key,*,now):
        record=self._tick(key,now=now)
        return self._page(key,now=now) if record.get('needs_page') else record
