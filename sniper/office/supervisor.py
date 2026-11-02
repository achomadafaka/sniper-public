"""Durable Office turns and fenced local actions; no funded imports or model authority."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace, field
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import time
from uuid import uuid4

from sniper.office import store


class StaleGeneration(RuntimeError): pass
class LeaseBusy(RuntimeError): pass
class ActorReplaced(RuntimeError): pass
class PendingAction(RuntimeError): pass
class ActionForbidden(RuntimeError): pass
class TurnDeadline(RuntimeError): pass


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _now(clock):
    value=clock()
    if not _number(value): raise ValueError('clock_unknown')
    return float(value)


def _get(conn,table,key):
    row=conn.execute('SELECT payload_json FROM '+table+' WHERE id=?',(key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _put(conn,table,key,value):
    conn.execute('INSERT INTO '+table+' VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json',
                 (key,store.canonical_json(value).decode()))


class Lease:
    def __init__(self,conn,*,clock,ttl_s=30.):
        if not _number(ttl_s) or ttl_s<=0: raise ValueError('lease_ttl')
        self.conn,self.clock,self.ttl_s,self.holder=conn,clock,float(ttl_s),None
        with store.write_transaction(conn):
            conn.execute('CREATE TABLE IF NOT EXISTS supervisor_lease(id INTEGER PRIMARY KEY CHECK(id=1),'
                         'holder TEXT NOT NULL,generation INTEGER NOT NULL,expires_at REAL NOT NULL)')

    def acquire(self,holder):
        if not isinstance(holder,str) or not holder: raise ValueError('lease_holder')
        with store.write_transaction(self.conn):
            now=_now(self.clock)
            row=self.conn.execute('SELECT holder,generation,expires_at FROM supervisor_lease WHERE id=1').fetchone()
            if row and row[2]>now:
                if row[0]!=holder: raise LeaseBusy('supervisor_lease_busy')
                generation=row[1]
            else: generation=1 if row is None else row[1]+1
            self.conn.execute('INSERT OR REPLACE INTO supervisor_lease VALUES(1,?,?,?)',
                              (holder,generation,now+self.ttl_s))
        self.holder=holder
        return generation

    def check(self,gen):
        row=self.conn.execute('SELECT holder,generation,expires_at FROM supervisor_lease WHERE id=1').fetchone()
        if (type(gen) is not int or row is None or row[0]!=self.holder or row[1]!=gen
                or row[2]<=_now(self.clock)):
            raise StaleGeneration('stale_supervisor_lease')

    @contextmanager
    def guard(self,gen):
        # The same Office write lock serializes takeover and local cross-ledger mutation.
        with store.write_transaction(self.conn):
            # A caller may hold a DEFERRED read snapshot; take the write lock before trusting it.
            self.conn.execute('UPDATE supervisor_lease SET expires_at=expires_at WHERE id=1')
            self.check(gen)
            yield
            self.check(gen)

    def renew(self,gen):
        with self.guard(gen):
            self.conn.execute('UPDATE supervisor_lease SET expires_at=? WHERE id=1',
                              (_now(self.clock)+self.ttl_s,))

    def release(self,gen):
        with store.write_transaction(self.conn):
            self.check(gen)
            self.conn.execute('UPDATE supervisor_lease SET expires_at=? WHERE id=1',(_now(self.clock),))


@dataclass(frozen=True)
class Context:
    actor_id: str
    role: str
    book_id: str
    generation: int
    lease_generation: int
    decision_id: str
    turn_id: str
    policy_hash: str
    prompt_hash: str
    cut: dict
    deadline_at: float
    epoch_id: str | None = None


@dataclass(frozen=True)
class Action:
    kind: str
    payload: dict


@dataclass(frozen=True)
class Step:
    done: bool = False
    actions: list[Action] = field(default_factory=list)
    value: dict = field(default_factory=dict)
    source_state: str = 'observed'


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    state: str
    steps: int
    reason: str | None = None
    value: dict = field(default_factory=dict)


def action_key(decision_id,kind,recipient=None):
    if not isinstance(decision_id,str) or not decision_id or not isinstance(kind,str) or not kind:
        raise ValueError('action_identity')
    if recipient is not None and (not isinstance(recipient,str) or not recipient or len(recipient)>256):
        raise ValueError('action_identity')
    return hashlib.sha256(store.canonical_json([decision_id,kind] if recipient is None else [decision_id,kind,recipient])).hexdigest()


class Supervisor:
    def __init__(self,conn,*,lease,clock,step,freeze_cut,handlers=None,lookups=None,book_id='office',fault=None,authorize=None,epoch_id=None):
        self.conn,self.lease,self.clock,self.step,self.freeze_cut=conn,lease,clock,step,freeze_cut
        self.handlers,self.lookups=dict(handlers or {}),dict(lookups or {})
        self.book_id,self.fault,self.authorize=book_id,fault,authorize
        self.epoch_id=epoch_id
        with store.write_transaction(conn):
            for table in ('turn_runs','action_requests'):
                conn.execute('CREATE INDEX IF NOT EXISTS '+table+'_state_time ON '+table+
                             "(json_extract(payload_json,'$.state'),json_extract(payload_json,'$.created_at'),id)")

    def _fault(self,boundary,key):
        if self.fault is not None: self.fault(boundary,key)

    def _book(self,actor):
        book=self.book_id(actor) if callable(self.book_id) else self.book_id
        if not isinstance(book,str) or not book: raise ValueError('book_binding')
        return book

    def _actor(self,context):
        self.lease.check(context.lease_generation)
        actor=_get(self.conn,'agents',context.actor_id)
        if (actor is None or actor.get('state')!='active' or type(actor.get('generation')) is not int
                or actor.get('generation')!=context.generation or context.book_id!=self._book(actor)
                or {'Desk ops':'ops'}.get(actor.get('role'),str(actor.get('role')).lower())!=context.role
                or actor.get('policy_hash')!=context.policy_hash or actor.get('prompt_hash')!=context.prompt_hash):
            raise ActorReplaced('actor_replaced')
        return actor

    def valid_context(self,context):
        """Broker callback only; mutation handlers additionally use apply_action's lease guard."""
        try:
            self._actor(context)
            return _now(self.clock)<context.deadline_at
        except (StaleGeneration,ActorReplaced,AttributeError,TypeError,ValueError): return False

    def context(self,agent_id,turn_id,gen,cut,*,deadline_at):
        with self.lease.guard(gen):
            actor=_get(self.conn,'agents',agent_id)
            if actor is None or actor.get('state')!='active': raise ActorReplaced('actor_replaced')
            if (type(actor.get('generation')) is not int or actor['generation']<1
                    or not all(isinstance(actor.get(k),str) and actor[k] for k in ('role','policy_hash','prompt_hash'))):
                raise ValueError('actor_record')
            sample=cut.get('clock_sample') if isinstance(cut,dict) else None
            if (not isinstance(sample,dict) or cut.get('basis')!='corrected_utc'
                    or not _number(sample.get('offset_s'))
                    or not _number(sample.get('corrected_utc')) or abs(sample['offset_s'])>172800
                    or not _number(cut.get('receipt_cut')) or not _number(cut.get('knowledge_cut'))
                    or cut['knowledge_cut']>cut['receipt_cut'] or cut['receipt_cut']>_now(self.clock)
                    or sample['corrected_utc']!=cut['receipt_cut']
                    or (cut.get('slot_cut') is not None and (type(cut['slot_cut']) is not int or cut['slot_cut']<0))):
                raise ValueError('clock_unknown')
            if not _number(deadline_at): raise ValueError('turn_deadline')
            role={'Desk ops':'ops'}.get(actor['role'],actor['role'].lower())
            return Context(agent_id,role,self._book(actor),actor['generation'],gen,turn_id,turn_id,
                           actor['policy_hash'],actor['prompt_hash'],json.loads(store.canonical_json(cut)),float(deadline_at),
                           self.epoch_id(actor) if callable(self.epoch_id) else self.epoch_id)

    def _authorize(self,context,action,monotonic_deadline):
        self._actor(context)
        if (_now(self.clock)>=context.deadline_at or
                monotonic_deadline is not None and time.monotonic()>=monotonic_deadline):
            raise TurnDeadline('deadline')
        try:
            bound=store.canonical_json([asdict(context),asdict(action)])
            allowed=self.authorize is not None and self.authorize(context,action) is True
            allowed=allowed and store.canonical_json([asdict(context),asdict(action)])==bound
        except Exception:
            allowed=False
        if not allowed: raise ActionForbidden('action_forbidden')
        self._actor(context)
        if (_now(self.clock)>=context.deadline_at or
                monotonic_deadline is not None and time.monotonic()>=monotonic_deadline):
            raise TurnDeadline('deadline')

    def apply_action(self,context,action,*,monotonic_deadline=None):
        if not isinstance(action,Action) or action.kind not in self.handlers or not isinstance(action.payload,dict):
            raise ActionForbidden('action_forbidden')
        key=action_key(context.decision_id,action.kind)
        identity=dict(actor_id=context.actor_id,generation=context.generation,book_id=context.book_id,
                      decision_id=context.decision_id,role=context.role,deadline_at=context.deadline_at,policy_hash=context.policy_hash,prompt_hash=context.prompt_hash,
                      cut=context.cut,kind=action.kind,request=action.payload)
        identity=json.loads(store.canonical_json(identity))
        digest=hashlib.sha256(store.canonical_json(identity)).hexdigest()
        with self.lease.guard(context.lease_generation):
            self._authorize(context,action,monotonic_deadline)
            if action.kind=='msg.send':
                recipient=action.payload.get('to_actor_id')
                if not isinstance(recipient,str) or not recipient or len(recipient)>256:
                    raise ActionForbidden('action_forbidden')
                # Preserve historical single-send identities, including prepared sends.
                legacy=_get(self.conn,'action_requests',key)
                if legacy is None or legacy['request']['to_actor_id']!=recipient:
                    key=action_key(context.decision_id,action.kind,recipient)
            record=_get(self.conn,'action_requests',key)
            fresh=record is None
            if record is not None:
                if record.get('identity_hash')!=digest: raise ValueError('action_identity')
                if record['state']=='completed': return record['result']
            else:
                record={**identity,'context':asdict(context),'identity_hash':digest,'state':'prepared','created_at':_now(self.clock)}
                _put(self.conn,'action_requests',key,record)
        self._fault('intent_committed',key)
        # No network here. Cross-store handlers must be idempotent on key, and expose lookup.
        with self.lease.guard(context.lease_generation):
            self._authorize(context,action,monotonic_deadline)
            lookup=self.lookups.get(action.kind)
            result=lookup(context,key,json.loads(store.canonical_json(action.payload))) if lookup is not None else None
            if not fresh and lookup is None:
                raise PendingAction('action_outcome_unknown')
            if result is None:
                self._authorize(context,action,monotonic_deadline)
                trusted=Context(**json.loads(store.canonical_json(asdict(context))))
                result=self.handlers[action.kind](trusted,key,json.loads(store.canonical_json(action.payload)))
            store.canonical_json(result)
            self._fault('effect_committed',key)
            record.update(state='completed',result=result,completed_at=_now(self.clock))
            _put(self.conn,'action_requests',key,record)
        return result

    def reconcile_turns(self,*,gen,limit=1000):
        """Close expired/unknown-deadline remnants; never infer an action outcome."""
        if type(limit) is not int or not 0<limit<=1000: raise ValueError('reconcile_limit')
        expired=[]
        with self.lease.guard(gen):
            now=_now(self.clock)
            rows=self.conn.execute("SELECT id,payload_json FROM turn_runs INDEXED BY turn_runs_state_time "
                "WHERE json_extract(payload_json,'$.state')='running' "
                "AND (COALESCE(json_type(payload_json,'$.deadline_at'),'null') NOT IN ('integer','real') "
                "OR json_extract(payload_json,'$.deadline_at')<=? "
                "OR json_extract(payload_json,'$.deadline_at')>1.7976931348623157e308) "
                "ORDER BY json_extract(payload_json,'$.created_at'),id LIMIT ?",(now,limit)).fetchall()
            for key,raw in rows:
                record=json.loads(raw)
                deadline=record.get('deadline_at')
                try: known=_number(deadline)
                except OverflowError: known=False
                if known and deadline>now: continue
                if type(deadline) in (int,float) and not known:
                    # JSON numeric overflow is readable by SQLite but cannot be
                    # republished as canonical finite JSON. Preserve the diagnosis.
                    record.update(deadline_at=None,deadline_unknown_kind='nonfinite')
                record.update(state='partial',reason='deadline' if known else 'deadline_unknown',
                              ended_at=now,recovered_by_lease_generation=gen)
                _put(self.conn,'turn_runs',key,record)
                expired.append(key)
        return expired

    def reconcile(self,*,gen,limit=1000):
        if type(limit) is not int or not 0<limit<=1000: raise ValueError('reconcile_limit')
        self.reconcile_turns(gen=gen,limit=limit)
        fixes=[]
        with self.lease.guard(gen):
            rows=self.conn.execute("SELECT id,payload_json FROM action_requests WHERE json_extract(payload_json,'$.state')='prepared' "
                                   'ORDER BY id LIMIT ?',(limit,)).fetchall()
            for key,raw in rows:
                record=json.loads(raw)
                lookup=self.lookups.get(record['kind'])
                result=(lookup(Context(**record['context']),key,record['request'])
                        if lookup is not None and 'context' in record else None)
                if result is not None:
                    record.update(state='completed',result=result,completed_at=_now(self.clock))
                    _put(self.conn,'action_requests',key,record)
                fixes.append({'action_key':key,'state':'recovered' if result is not None else 'unresolved'})
        return fixes

    def run_turn(self,agent_id,*,gen,deadline_s,max_steps,turn_id=None):
        if not _number(deadline_s) or deadline_s<=0 or type(max_steps) is not int or max_steps<1:
            raise ValueError('turn_bounds')
        turn_id=turn_id or str(uuid4())
        with self.lease.guard(gen):
            record=_get(self.conn,'turn_runs',turn_id)
            cut=self.freeze_cut() if record is None else record['cut']
            deadline_at=record['deadline_at'] if record is not None else _now(self.clock)+deadline_s
            try: context=self.context(agent_id,turn_id,gen,cut,deadline_at=deadline_at)
            except ValueError as exc:
                if str(exc)!='clock_unknown': raise
                return TurnResult(turn_id,'refused',0,'clock_unknown')
            if record is not None:
                context=replace(context,epoch_id=record.get('epoch_id'))
                if any(record.get(k)!=getattr(context,k) for k in ('actor_id','generation','policy_hash','prompt_hash')):
                    raise ActorReplaced('actor_replaced')
                if record['state']=='completed':
                    return TurnResult(turn_id,'completed',record['steps'],value=record.get('value',{}))
                if record.get('reason')=='desk_judge_work_unknown' and (
                        record['state']=='refused' or (record['state']=='deferred' and _now(self.clock)<record['retry_at'])):
                    return TurnResult(turn_id,record['state'],record['steps'],record['reason'],record.get('value',{}))
            else:
                record={**asdict(context),'state':'running','created_at':_now(self.clock),
                        'deadline_at':deadline_at,'steps':0,'pending_step':None}
                _put(self.conn,'turn_runs',turn_id,record)
        monotonic_deadline=time.monotonic()+max(0,min(deadline_s,record['deadline_at']-_now(self.clock)))
        state,reason='partial','step_limit'
        try:
            while record['steps']<max_steps or record.get('pending_step') is not None:
                self._actor(context)
                if _now(self.clock)>=record['deadline_at'] or time.monotonic()>=monotonic_deadline:
                    reason='deadline';break
                pending=record.get('pending_step')
                if pending is None:
                    # No Office write lock is held during model calls or broker reads.
                    step=self.step(context,record.get('value',{}),max(0,monotonic_deadline-time.monotonic()))
                    if not isinstance(step,Step) or step.source_state not in ('observed','off','partial'):
                        raise ValueError('turn_step')
                    self._actor(context)
                    if _now(self.clock)>=record['deadline_at'] or time.monotonic()>=monotonic_deadline:
                        # Preserve the returned diagnostic, but never prepare or apply its late actions.
                        returned=asdict(step)
                        store.canonical_json(returned)
                        value=dict(step.value)
                        value.setdefault('reason','deadline')
                        with self.lease.guard(gen):
                            self._actor(context)
                            record.update(last_step=returned,value=value,steps=record['steps']+1,pending_step=None,
                                          source_state=step.source_state if step.source_state!='observed' else 'partial')
                            _put(self.conn,'turn_runs',turn_id,record)
                        reason='deadline';break
                    if (record['steps']==0 and not step.actions and step.source_state=='off'
                            and step.value.get('reason')=='desk_judge_work_unknown'
                            and step.value.get('retryable_pre_dispatch') is True):
                        attempts=record.get('guard_attempts',0)+1
                        retry_at=_now(self.clock)+5.
                        state='deferred' if attempts<3 and retry_at<record['deadline_at'] else 'refused'
                        reason='desk_judge_work_unknown'
                        record.update(guard_attempts=attempts,retry_at=retry_at,value=step.value,source_state='off')
                        break
                    pending=asdict(step)
                    store.canonical_json(pending)
                    with self.lease.guard(gen):
                        self._actor(context)
                        record.update(pending_step=pending,steps=record['steps']+1)
                        if 'request_admission' in pending['value']:
                            record['value']={**record.get('value',{}),'request_admission':pending['value']['request_admission']}
                        _put(self.conn,'turn_runs',turn_id,record)
                for action in pending['actions']:
                    self.apply_action(context,Action(**action),monotonic_deadline=monotonic_deadline)
                with self.lease.guard(gen):
                    self._actor(context)
                    record.update(value=pending['value'],source_state=pending['source_state'],pending_step=None)
                    record.update({k:pending['value'][k] for k in ('dm_context','dm_count','dm_unread_backlog','task_context','ref_context','refs_count') if k in pending['value']})
                    _put(self.conn,'turn_runs',turn_id,record)
                if pending['done']:
                    state,reason='completed',None;break
        except ActorReplaced:
            state,reason='refused','actor_replaced'
        except TurnDeadline:
            state,reason='partial','deadline'
        except ActionForbidden:
            state,reason='refused','action_forbidden'
        except PendingAction:
            state,reason='partial','action_outcome_unknown'
        except Exception:
            state,reason='failed','step_error'
        with self.lease.guard(gen):
            record.update(state=state,reason=reason,ended_at=_now(self.clock))
            _put(self.conn,'turn_runs',turn_id,record)
        return TurnResult(turn_id,state,record['steps'],reason,record.get('value',{}))

    def progress_frontiers(self,*,stall_after_s=30):
        return progress_frontiers(self.conn,now=_now(self.clock),stall_after_s=stall_after_s)


def progress_frontiers(conn,*,now,stall_after_s=30):
    if not _number(now): raise ValueError('clock_unknown')
    action=conn.execute("SELECT MIN(json_extract(payload_json,'$.created_at')) FROM action_requests "
                             "WHERE json_extract(payload_json,'$.state')='prepared'").fetchone()[0]
    running=conn.execute("SELECT MIN(json_extract(payload_json,'$.created_at')) FROM turn_runs "
                              "WHERE json_extract(payload_json,'$.state')='running'").fetchone()[0]
    pending=[at for at in (action,running) if at is not None]
    age=max(0,now-min(pending)) if pending else 0.
    last=conn.execute("SELECT payload_json FROM turn_runs ORDER BY json_extract(payload_json,'$.created_at') DESC,id DESC LIMIT 1").fetchone()
    payload={} if last is None else json.loads(last[0])
    partial=conn.execute("SELECT COUNT(*) FROM turn_runs WHERE json_extract(payload_json,'$.state')='partial'").fetchone()[0]
    through=conn.execute("SELECT MAX(json_extract(payload_json,'$.cut.receipt_cut')) FROM turn_runs "
                              "WHERE json_extract(payload_json,'$.state')='completed' AND json_extract(payload_json,'$.created_at')<?",
                              (min(pending) if pending else now+1,)).fetchone()[0]
    source_off=payload.get('source_state')=='off'
    state=('stalled' if age>stall_after_s else 'source_off' if source_off else
           'partial' if payload.get('state')=='partial' or payload.get('source_state')=='partial' else 'working' if pending else 'healthy_empty')
    return dict(state=state,decisions_through=through,oldest_pending_s=age,partial_count=partial,source_off=source_off)


def run_child(argv,*,timeout_s,cwd,env,max_output_bytes=1_000_000,input_bytes=None):
    """Host-built command only. Drain both pipes and reap the entire owned session."""
    from sniper.office.launcher_diagnostics import CAPTURE_BYTES
    from contextlib import nullcontext
    import tempfile
    if not _number(timeout_s) or timeout_s<=0 or type(max_output_bytes) is not int or max_output_bytes<1:
        raise ValueError('child_bounds')
    if input_bytes is not None and (not isinstance(input_bytes,bytes) or len(input_bytes)>1_000_000):
        raise ValueError('child_input')
    # A bounded spool cannot deadlock writing stdin when the child never reads it.
    with tempfile.TemporaryFile() if input_bytes is not None else nullcontext(None) as source:
        if source is not None:source.write(input_bytes);source.seek(0)
        proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    output=bytearray();errors=bytearray();error_bytes=0;outcome='success';deadline=time.monotonic()+timeout_s
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout,selectors.EVENT_READ,'stdout')
            selector.register(proc.stderr,selectors.EVENT_READ,'stderr')
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    outcome='timeout';break
                for key,_ in selector.select(min(remaining,.05)):
                    chunk=os.read(key.fileobj.fileno(),min(65536,max_output_bytes-(len(output) if key.data=='stdout' else error_bytes)+1))
                    if not chunk:
                        selector.unregister(key.fileobj);continue
                    if key.data=='stdout':
                        room=max_output_bytes-len(output)
                        output.extend(chunk[:room])
                        if len(chunk)>room:outcome='output_limit'
                    else:
                        error_bytes+=len(chunk);errors.extend(chunk)
                        del errors[:-CAPTURE_BYTES]
                        if error_bytes>max_output_bytes:outcome='output_limit'
                    if outcome!='success':break
                if outcome!='success':break
        if outcome=='success':
            try:
                if proc.wait(timeout=max(0,deadline-time.monotonic()))!=0: outcome='child_error'
            except subprocess.TimeoutExpired: outcome='timeout'
    finally:
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
        proc.stdout.close();proc.stderr.close()
    return {'outcome':outcome,'stdout':bytes(output),'stderr':bytes(errors),
            'stderr_truncated':error_bytes>len(errors),'returncode':proc.returncode}
