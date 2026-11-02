"""Bounded live work and model steps. No funded imports, model-selected identity, or implicit retry."""
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL,uuid5

from sniper.office import loops,pilot_models,store,model_diagnostics
from sniper.office.broker import quote_external
from sniper.office.supervisor import Action,Step

class RuntimeRefused(ValueError):
    def __init__(self,reason,*,cause=None):
        super().__init__(reason)
        self.detail=None if cause is None else dict(exception_class=type(cause).__name__,
            message=model_diagnostics.detail(str(cause)))


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


class DueWork:
    def __init__(self,path,cadence,*,clock,desk_path=None,exclude_cohort=lambda:None):
        self.path,self.cadence,self.clock,self.desk_path=Path(path),cadence,clock,desk_path
        self.exclude_cohort=exclude_cohort   # eval-1: its frozen cohort's tickets are Office input, not desk backlog

    def _connect(self,path):
        conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        conn.row_factory=sqlite3.Row
        deadline=time.monotonic()+.2
        conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        return conn

    def tasks(self):
        try:
            now=self.clock()
            if not _number(now):raise ValueError()
            with closing(self._connect(self.path)) as conn:
                rows=conn.execute('SELECT payload_json FROM agents LIMIT 6').fetchall()
                if len(rows)>5:raise ValueError()   # eval-1 has 5 seats
                agents=[json.loads(row[0]) for row in rows]
                candidates=loops.due_turns(self.cadence,agents,now=now,done_turn_ids=set())
                out=[];pending={}
                deferred=conn.execute("SELECT id,payload_json FROM turn_runs INDEXED BY turn_runs_deferred_deadline "
                    "WHERE json_extract(payload_json,'$.state')='deferred' AND json_extract(payload_json,'$.deadline_at')>? "
                    "ORDER BY json_extract(payload_json,'$.deadline_at'),id LIMIT 5",(now,)).fetchall()
                if len(deferred)>4:raise ValueError()
                epoch=None
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_epoch_control'").fetchone():
                    epoch=json.loads(conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()[0])['active_epoch']
                for row in deferred:
                    record=json.loads(row['payload_json'])
                    own=next((a for a in agents if a['agent_id']==record.get('actor_id') and a['state']=='active'),None)
                    if own is None or any(record.get(k)!=own.get(k) for k in ('generation','policy_hash','prompt_hash')):continue
                    if epoch is not None and record.get('epoch_id')!=epoch:continue
                    if (record.get('reason')!='desk_judge_work_unknown' or not _number(record.get('retry_at'))
                            or not _number(record.get('deadline_at')) or record.get('steps')!=0 or record.get('pending_step') is not None
                            or type(record.get('guard_attempts'))is not int or not 0<record['guard_attempts']<3
                            or record.get('value',{}).get('retryable_pre_dispatch') is not True):raise ValueError()
                    if own['agent_id'] in pending:raise ValueError()
                    pending[own['agent_id']]=(row['id'],record)
                for task in candidates:
                    if task['agent_id'] in pending:
                        tid,record=pending[task['agent_id']]
                        if now>=record['retry_at']:out.append(dict(task,turn_id=tid,slot=None))
                        continue
                    row=conn.execute('SELECT payload_json FROM turn_runs WHERE id=?',(task['turn_id'],)).fetchone()
                    if row is not None:
                        record=json.loads(row[0])
                        if record.get('state')=='deferred':
                            if (record.get('reason')!='desk_judge_work_unknown' or not _number(record.get('retry_at'))
                                    or not _number(record.get('deadline_at'))):raise ValueError()
                            if now<record['retry_at'] or now>=record['deadline_at']:continue
                            out.append(task);continue
                        if record.get('state') in ('completed','failed','refused','partial'):continue
                        if record.get('state')!='running':raise ValueError()
                        if not _number(record.get('deadline_at')):raise ValueError()
                        if record['deadline_at']<=now:continue
                    out.append(task)
                return out
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            raise RuntimeRefused('live_work_unknown') from None

    def live_backlogged(self):
        if self.tasks():return True
        try:
            now=self.clock()
            if not _number(now):raise ValueError()
            with closing(self._connect(self.path)) as conn:
                return (conn.execute("SELECT 1 FROM turn_runs WHERE json_extract(payload_json,'$.state')='running' AND json_extract(payload_json,'$.deadline_at')>? LIMIT 1",(now,)).fetchone() is not None
                        or conn.execute("SELECT 1 FROM action_requests WHERE json_extract(payload_json,'$.state')='prepared' LIMIT 1").fetchone() is not None)
        except (OSError,sqlite3.Error,ValueError):
            raise RuntimeRefused('live_work_unknown') from None

    def desk_backlogged(self):
        try:
            if self.desk_path is None:raise ValueError()
            from sniper.persistence import db_memecoin_desk as desk
            with closing(self._connect(self.desk_path)) as conn:
                return bool(desk.list_awaiting_judge(conn,limit=1,not_cohort=self.exclude_cohort()) or desk.list_judge_calls_to_record(conn,limit=1))
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as exc:
            raise RuntimeRefused('desk_judge_work_unknown',cause=exc) from None

    def backlogged(self):
        return self.live_backlogged() or self.desk_backlogged()


# Only declared pilot requests are representable; capability/handler checks still own authority.
_REFS={'type':['array','null'],'maxItems':32,'items':{'type':'object','additionalProperties':False,
    'required':['kind','receipt_id'],'properties':{'kind':{'type':'string','enum':['memo','decision','proposal']},
    'receipt_id':{'type':'string','pattern':'^[0-9a-f]{64}$'}}}}
_REQUEST_ARGS={
    'desk.near_pass_list':{},
    'desk.near_pass_sheet':{'ticket_id':{'type':'string','minLength':1,'maxLength':160}},
    'msg.send':{'to_actor_id':{'type':'string','minLength':1,'maxLength':256},'text':{'type':'string','minLength':1,'maxLength':4096},'refs':_REFS},
    'task.set':{'title':{'type':'string','minLength':1,'maxLength':256},'note':{'type':'string','maxLength':2048}},
    'task.update':{'task_id':{'type':'string','pattern':'^[0-9a-f]{64}$'},'status':{'type':'string','enum':['open','blocked','done']},'note':{'type':'string','maxLength':2048}},
    'grade.read':{'cursor':{'type':'string','maxLength':256}},
    'judge.decide':{'decision':{'type':'string','enum':['ENTER','SKIP','WAIT','EXIT']},
                    'candidate':{'type':['string','null'],'pattern':'^[0-9a-f]{64}$'},
                    'order_id':{'type':['string','null'],'minLength':1,'maxLength':128},
                    'public_reason':{'type':'string','minLength':1,'maxLength':160}},
    'lesson.propose':{'for_role':{'type':'string','enum':['head','scout','judge']},'text':{'type':'string','minLength':1,'maxLength':4000},
                      'order_ids':{'type':'array','maxItems':32,'items':{'type':'string','minLength':1,'maxLength':128}}},
    'org.read':{},
    'health.read':{},
    'ticket.propose':{'problem':{'type':'string','minLength':1,'maxLength':4096},
                      'expected_impact':{'type':['number','null']},
                      'evidence_refs':{'type':'array','minItems':1,'maxItems':32,
                                       'items':{'type':'string','minLength':1,'maxLength':512}},
                      'done_when':{'type':'string','minLength':1,'maxLength':4096}},
    'ticket.approve':{'ticket_id':{'type':'string','minLength':1,'maxLength':512}},
    'org.propose':{'kind':{'type':'string','minLength':1,'maxLength':128},
                   'proposal':{'type':'string','minLength':1,'maxLength':4096},
                   'rationale':{'type':'string','minLength':1,'maxLength':4096}},
}
SCHEMA={'type':'object','additionalProperties':False,'required':['done','task','memo','requests','refs'],
        'properties':{'refs':_REFS,'done':{'type':'boolean'},'task':{'type':'string','maxLength':4096},
                      'memo':{'type':'string','maxLength':16384},
                      'requests':{'type':'array','maxItems':8,'items':{'anyOf':[
                          {'type':'object','additionalProperties':False,'required':['tool','args'],
                           'properties':{'tool':{'type':'string','const':name},
                                         'args':{'type':'object','additionalProperties':False,
                                                 'required':list(properties),'properties':properties}}}
                          for name,properties in _REQUEST_ARGS.items()]}}}}
MUTATION_BUDGET=6   # writes per turn; fixed so a new write kind never widens existing seats
WRITE_TOOLS=frozenset({'judge.decide','lesson.propose','ticket.propose','ticket.approve','org.propose','msg.send','task.set','task.update'})


def slot_of(actor):
    if actor.get('role')=='Head':return 'head'
    if actor.get('role')=='Scout':return 'scout'
    if actor.get('role')=='Postmortem':return 'postmortem'
    if actor.get('role')=='Judge' and actor.get('family') in ('claude','gpt'):return 'judge_'+actor['family']
    raise RuntimeRefused('actor_slot_unknown')


class AgentStep:
    def __init__(self,*,models_manifest,prompts,scheduler,transports,clock,actor,observe,read_tool,context_check,observe_call=None,validate_action=None,validate_memo_refs=None,acknowledge_context=None,discovery_only=False,claude_debug_stderr=True,evaluated_pinned=False):
        self.manifest,self.prompts,self.scheduler,self.transports=models_manifest,prompts,scheduler,transports
        self.claude_debug_stderr=claude_debug_stderr
        self.discovery_only=discovery_only
        self.evaluated_pinned=evaluated_pinned
        self.acknowledge_context=acknowledge_context
        self.observe_call=observe_call
        self.validate_action,self.validate_memo_refs=validate_action,validate_memo_refs
        self.clock,self.actor,self.observe,self.read_tool,self.context_check=clock,actor,observe,read_tool,context_check

    def __call__(self,context,previous,remaining_s):
        fields={k:None for k in ('dm_context','dm_count','dm_unread_backlog','task_context','ref_context','refs_count')}
        admission={'state':'unknown','requested_n':None,'refused_n':None,'truncated':None,'truncated_n':None}
        def refused(reason,state='off',*,request_refusal=False,**diagnostics):
            if request_refusal and admission['state']=='known':admission['refused_n']+=len(requests)
            return Step(done=True,value={'reason':reason,**fields,'request_admission':admission,**diagnostics},source_state=state)
        try:
            if self.context_check(context) is not True:return refused('actor_replaced')
            actor=self.actor(context.actor_id);slot=slot_of(actor);entry=self.manifest['seats'][slot];prompt=self.prompts[slot]
            if (actor.get('provider')!=entry['provider'] or actor.get('model')!=entry['model']
                    or actor.get('prompt_hash')!=prompt['sha256'] or context.prompt_hash!=prompt['sha256']
                    or hashlib.sha256(prompt['text'].encode()).hexdigest()!=prompt['sha256']):
                return refused('actor_pin_mismatch')
            index=previous.get('_runtime_step_index',0)
            if type(index)is not int or index<0:return refused('step_identity_unknown')
            rid=str(uuid5(NAMESPACE_URL,store.canonical_json([context.actor_id,context.generation,context.turn_id,index,context.prompt_hash,entry]).decode()))
            started=time.monotonic()
            frame=self.observe(context)
            work=frame.get('work_context')
            if isinstance(work,dict):
                fields.update({k:work.get(k) for k in ('dm_context','dm_count','task_context')})
                fields.update(dm_unread_backlog=work.get('unread_backlog'),ref_context=False,refs_count=0)
            user=store.canonical_json({'context':asdict(context),'input':quote_external(store.canonical_json(frame).decode(),'host_snapshot',self.clock()),
                'previous':quote_external(store.canonical_json(previous).decode(),'previous_turn_step',self.clock())}).decode()
            if len(user.encode())>65536:return refused('input_budget')
            dispatched=False
            def dispatch():
                nonlocal dispatched
                dispatched=True
                budget=min(remaining_s-(time.monotonic()-started),context.deadline_at-self.clock())
                transport=self.transports[entry['provider']]
                def invoke(system,user,*,model,deadline_s,schema_obj):
                    doc=dict(system=system,user=user,model=model,deadline_s=deadline_s,schema=schema_obj)
                    if entry['provider']=='claude':doc['debug_stderr']=self.claude_debug_stderr
                    return transport.client.call(transport.adapter,doc,
                        deadline_at=min(context.deadline_at,self.clock()+deadline_s),request_id=rid)
                return pilot_models.call(self.manifest,slot,prompt['text'],user,deadline_s=budget,schema_obj=SCHEMA,clock=self.clock,
                                         transports={entry['provider']:invoke},discovery_only=self.discovery_only,evaluated_pinned=self.evaluated_pinned)
            result=self.scheduler.submit(entry['provider'],dispatch,timeout_s=max(0,remaining_s),priority='live')
            if dispatched and self.observe_call is not None:
                self.observe_call(context,result,call_id=rid)
            if self.context_check(context) is not True:return refused('actor_replaced')
            if result.outcome!='success':
                reason=result.error_reason or result.outcome
                diagnostic={k:getattr(result,k) for k in ('provider_error_kind','provider_error_detail') if getattr(result,k) is not None}
                if reason=='provider_error' and result.provider_error_kind is not None:
                    reason+='_'+result.provider_error_kind
                if reason=='desk_judge_work_unknown' and not dispatched:
                    diagnostic.update(guard_error=result.guard_error,retryable_pre_dispatch=True)
                    admission.update(state='known',requested_n=0,refused_n=0,truncated=False,truncated_n=0)
                return refused(reason,'partial' if result.outcome=='partial' else 'off',**diagnostic)
            if self.acknowledge_context is not None:self.acknowledge_context(context)
            answer=json.loads(result.text)
            requests=answer['requests']
            before=previous.get('request_admission',{'state':'known','requested_n':0,'refused_n':0,'truncated':False,'truncated_n':0})
            if (not isinstance(before,dict) or before.get('state')!='known'
                    or any(type(before.get(k))is not int or before[k]<0 for k in ('requested_n','refused_n','truncated_n'))
                    or before.get('truncated') is not False):return refused('request_accounting_unknown')
            admission.update(state='known',requested_n=before['requested_n']+len(requests),
                refused_n=before['refused_n'],truncated=False,truncated_n=0)
            prior=previous.get('_runtime_mutation_keys',[])
            if (type(prior)is not list or len(prior)>MUTATION_BUDGET
                    or any(type(k)is not str or not k or len(k)>265 for k in prior)
                    or len(prior)!=len(set(prior))):return refused('mutation_identity_unknown',request_refusal=True)
            writes=[item['tool']+(':'+item['args']['to_actor_id'] if item['tool']=='msg.send' else '')
                    for item in requests if item['tool'] in WRITE_TOOLS]
            if len(writes)!=len(set(writes)) or set(writes)&set(prior):return refused('duplicate_mutation_kind',request_refusal=True)
            if len(prior)+len(writes)>MUTATION_BUDGET:return refused('mutation_budget',request_refusal=True)
            if answer['done'] and writes and len(writes)!=len(requests):
                return refused('mixed_final_requests',request_refusal=True)
            refs=[] if answer['refs'] is None else answer['refs']
            try:
                if refs and self.validate_memo_refs is None:raise ValueError('memo_refs_unavailable')
                if self.validate_memo_refs is not None:self.validate_memo_refs(context,refs)
            except ValueError as exc:return refused(str(exc),request_refusal=True,refused_refs_reason=str(exc),refs_count=None,ref_context=None)
            fields.update(ref_context=bool(refs),refs_count=len(refs))
            for item in requests:
                if item['tool'] in WRITE_TOOLS:
                    try:
                        if self.validate_action is not None:self.validate_action(context,item['tool'],item['args'])
                    except ValueError as exc:return refused(str(exc),request_refusal=True,refused_send_reason=str(exc) if item['tool']=='msg.send' else None)
            actions=[];receipts=[]
            for item in requests:
                if time.monotonic()-started>=remaining_s:
                    admission.update(state='unknown',requested_n=None,refused_n=None,truncated=None,truncated_n=None)
                    return refused('deadline','partial')
                if item['tool'] in WRITE_TOOLS:
                    actions.append(Action(item['tool'],item['args']))
                elif self.read_tool is not None:receipts.append(asdict(self.read_tool(context,item['tool'],item['args'])))
                else:return refused('tool_binding_unavailable',request_refusal=True)
            value={**fields,'request_admission':admission,'_runtime_step_index':index+1,'_runtime_mutation_keys':prior+writes,'task':answer['task'],'memo':answer['memo'],'receipts':receipts,
                   'served_model':result.served_model,'served_model_basis':result.served_model_basis,'attempt_id':result.attempt_id}
            if answer['done']:actions.append(Action('memo.commit',{'task':answer['task'],'memo':answer['memo'],'served_model':result.served_model,'served_model_basis':result.served_model_basis,'refs':refs}))
            return Step(done=answer['done'],actions=actions,value=value)
        except (ValueError,TypeError,KeyError,AttributeError,RuntimeRefused):
            admission.update(state='unknown',requested_n=None,refused_n=None,truncated=None,truncated_n=None)
            return refused('runtime_step_unknown')
