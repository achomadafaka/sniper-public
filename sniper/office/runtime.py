"""Pinned discovery-pilot composition. All authority is host-owned; model text is data."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sqlite3
import time

from sniper.office import judge_rounds as JR,judge_decisions as J,eval_settle as ES,eval_transition as X,operating_view as OV,lessons as LS,memory_store as MS,broker as B,capabilities as C,ledger as L,loops,pilot_models,pilot_seed,store as S,tickets,trader,runtime_observations as O,work as W,epoch_transition as E,claude_compat
from sniper.office.child_launcher import validate_peer
from sniper.office.host_auth import approved_path
from sniper.office.launcher_client import read_regular
from sniper.office.runtime_steps import AgentStep,DueWork,RuntimeRefused,slot_of
from sniper.office.supervisor import Lease,Supervisor,_get,_put
from sniper.office.supervisor_client import build as build_transports
from scripts.office_watchdog import PILOT_SHA256,caps_applied,external_guard

PROC_ROOT=Path('/proc')
CGROUP_ROOT=Path('/sys/fs/cgroup')
VERSION='office-runtime-pilot-v1'
FIELDS={'version','enabled','discovery_only','evaluated_epoch_allowed','research_enabled','content_addressed_release_root',
        'state_root','config_root','store_path','ledger_path','clock_file','guard_file','office_cgroup','supervisor_identity',
        'transport','cadence','model_manifest','pilot_seed_launcher_config','provider_caps','host_children_max',
        'turn_deadline_s','max_steps','lease_ttl_s','poll_s','desk_db'}


def _finite(v):
    return type(v) in (int,float) and math.isfinite(v)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_config(path):
    try:
        path=approved_path(str(path))
        pin_path=path.with_suffix('.sha256')
        raw=read_regular(path);pin=read_regular(pin_path).decode().strip()
        if len(pin)!=64 or _hash(raw)!=pin:raise ValueError()
        if not os.statvfs(path.parent).f_flag & os.ST_RDONLY:raise ValueError()
        config=json.loads(raw)
        if type(config)is not dict or set(config)-{'claude_sdk','claude_debug_stderr'}!=FIELDS or config['version']!=VERSION:raise ValueError()
        if 'claude_debug_stderr' in config and type(config['claude_debug_stderr'])is not bool:raise ValueError()
        # discovery runtime, or the eval-1 paper runtime (both flags flipped together; bound to the eval-1 prompt profile below)
        if config['enabled'] is not True or config['research_enabled'] is not False or (config['discovery_only'],config['evaluated_epoch_allowed']) not in ((True,False),(False,True)):raise ValueError()
        for key in ('turn_deadline_s','lease_ttl_s','poll_s'):
            if not _finite(config[key]) or config[key]<=0:raise ValueError()
        if config['turn_deadline_s']>120 or config['poll_s']>60 or config['lease_ttl_s']<=config['turn_deadline_s']+config['poll_s']:raise ValueError()
        if type(config['max_steps'])is not int or not 1<=config['max_steps']<=16:raise ValueError()
        if type(config['host_children_max'])is not int or not 1<=config['host_children_max']<=4:raise ValueError()
        if (type(config['provider_caps'])is not dict or set(config['provider_caps'])!={'claude','codex'}
                or any(type(v)is not int or not 1<=v<=4 for v in config['provider_caps'].values())):raise ValueError()
        return config,pin
    except (OSError,ValueError,TypeError,KeyError,UnicodeError):
        raise RuntimeRefused('runtime_config_unapproved') from None


def _sample(path):
    try:
        raw=read_regular(path);doc=json.loads(raw);host=time.time()
        offset,at=doc['offset_s'],doc['at']
        if not all(_finite(v) for v in (host,offset,at)) or not 0<=host-at<=3600 or abs(offset)>172800:raise ValueError()
        return {'corrected_utc':host-offset,'offset_s':offset,'sampled_at':at,'clock_sample_id':_hash(raw)}
    except (OSError,ValueError,TypeError,KeyError):
        raise RuntimeRefused('clock_unknown') from None


def _identity(config):
    identity=config['supervisor_identity']
    if (type(identity)is not dict or set(identity)!={'crew_uid','expected_cgroup','supervisor_script'}
            or type(identity['crew_uid'])is not int or identity['crew_uid']!=os.geteuid()
            or Path(identity['expected_cgroup']).name!='office-supervisor.service'
            or Path(identity['expected_cgroup']).parent.name!='office.slice'):
        raise RuntimeRefused('supervisor_identity')
    root=Path(config['content_addressed_release_root'])
    if Path(identity['supervisor_script']).resolve()!=root/'scripts/office_supervisor.py':
        raise RuntimeRefused('supervisor_identity')
    try:
        return validate_peer(os.getpid(),os.geteuid(),proc_root=PROC_ROOT,expected_uid=identity['crew_uid'],
                             expected_cgroup=identity['expected_cgroup'],supervisor_script=identity['supervisor_script'])
    except (OSError,ValueError,TypeError):
        raise RuntimeRefused('supervisor_identity') from None


def _pinned(config_root,ref,filename):
    if type(ref)is not dict or set(ref)!={'path','sha256'} or Path(ref['path'])!=config_root/filename:
        raise RuntimeRefused('runtime_file_reference')
    raw=read_regular(approved_path(ref['path']))
    if _hash(raw)!=ref['sha256']:raise RuntimeRefused('runtime_file_changed')
    return json.loads(raw)


JUDGE_SHEET_MAX_AGE_S=1800.  # ponytail: fixed window; tie to Judge cadence if turns get slower
JUDGE_SHEET_BUDGET=30000     # raw bytes; escaped ~1.2x beside ~10 KB messages stays under the 64 KiB input_budget
def ticket_cohort_refusal(desk_path,ticket_id,cohort_id):
    """eval-1 reads only tickets produced under its frozen near-pass cohort (prereg sec 9); None when allowed."""
    c=sqlite3.connect(Path(desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.)
    try:row=c.execute('SELECT cohort_id FROM desk_ticket_heads WHERE ticket_id=?',(ticket_id,)).fetchone()
    finally:c.close()
    if row is None:return 'ticket_unknown'
    return None if row[0]==cohort_id else 'eval_cohort_mismatch'


# Am14 (prereg sec 22): the host declares which facts are required. One source of truth: the sheet itself carries
# required / missing_required / optional_unknown (tools/sheet.py REQUIRED, Lambda 1a32ec1d2); the frame names that
# contract, its version and the list, hashed with the book policy. Every other absent field is an optional unknown.
REQUIRED_FACTS_VERSION='office-eval1-required-facts-v1'


def required_facts(book_policy_sha256):
    from sniper.office.tools import sheet as SH
    body={'version':REQUIRED_FACTS_VERSION,'facts':list(SH.REQUIRED),'per_sheet':'candidates.items[].sheet.required',
          'missing':'candidates.items[].sheet.missing_required','optional':'candidates.items[].sheet.optional_unknown',
          'rule':'WAIT on missing input only for facts in missing_required; sheet.missing and optional_unknown are optional unknowns'}
    return dict(body,sha256=hashlib.sha256(S.canonical_json([body,book_policy_sha256])).hexdigest())


def eval_housekeeping(health,*,settle,rounds):
    """eval-1 work after each turn: settlement (narrow typed failures) then the host round pass, which may do desk, tape,
    RPC and file I/O and therefore never raises out of flush (a crash would loop the supervisor)."""
    try:settle()
    except (sqlite3.Error,ValueError,KeyError,TypeError,RuntimeRefused):
        health=dict(health,state='partial',last_error='eval_settle_failed')
    try:rounds()
    except Exception as exc:
        health=dict(health,state='partial',last_error='rounds_failed:'+type(exc).__name__)
    return health


def candidate_item(rec,row):
    return {'receipt_row_hash':row,'cache_key':rec['cache_key'],'sheet_hash':rec['sheet_hash'],'lenses_hash':rec['lenses_hash'],
            'known_at':rec['clock_sample']['corrected_utc'],'mint':rec['request'].get('mint'),'pool':rec['request'].get('pool'),
            'feed':rec['request'].get('feed','pass'),'sheet':rec['sheet'],'lenses':rec['lenses']}


def candidate_by_row(conn,row_hash):
    """The verified candidate item a Judge decided on (eval_orders.enter re-verifies it again), or None."""
    from sniper.office.tools import sheet as SH
    hit=conn.execute('SELECT payload_json,prev_hash,row_hash FROM receipts WHERE row_hash=?',(row_hash,)).fetchone()
    if hit is None:return None
    rec=json.loads(hit[0])
    rec=SH._verify({'payload_json':hit[0],'prev_hash':hit[1],'row_hash':hit[2]},rec.get('cache_key'),rec.get('definition_bundle_hash'),rec.get('strategy_epoch'))
    return candidate_item(rec,row_hash) if rec.get('state')=='known' else None


def judge_candidates(conn,*,cut,limit=4,cohort_id=None,budget=JUDGE_SHEET_BUDGET):
    """Newest causal (corrected_utc <= cut), fresh, re-verified sheet receipts for a Judge frame. A corrupt or
    over-budget receipt is omitted and counted, never a silent empty list."""
    from sniper.office.tools import sheet as SH
    rows=conn.execute("SELECT payload_json,prev_hash,row_hash FROM receipts INDEXED BY receipts_sheet_at "
        "WHERE json_extract(payload_json,'$.kind')='sheet' AND json_extract(payload_json,'$.clock_sample.corrected_utc') BETWEEN ? AND ? "
        "ORDER BY json_extract(payload_json,'$.clock_sample.corrected_utc') DESC LIMIT ?",(cut-JUDGE_SHEET_MAX_AGE_S,cut,limit)).fetchall()
    items=[];omitted=0;used=0;other=0
    for payload,prev,row in rows:
        rec=json.loads(payload)
        rec=SH._verify({'payload_json':payload,'prev_hash':prev,'row_hash':row},rec.get('cache_key'),rec.get('definition_bundle_hash'),rec.get('strategy_epoch'))
        if rec.get('state')!='known':omitted+=1;continue
        if cohort_id is not None and rec.get('strategy_epoch')!=cohort_id:other+=1;continue   # eval-1: frozen cohort only (prereg sec 9)
        item=candidate_item(rec,row)
        size=len(S.canonical_json(item))
        if size>JUDGE_SHEET_BUDGET or used+size>budget:omitted+=1;continue   # one sheet never exceeds the input budget
        used+=size;items.append(item)
    out={'state':'known','items':items,'omitted_n':omitted}
    if cohort_id is not None:out.update(cohort_id=cohort_id,other_cohort_n=other)
    return out


HELIUS_KEY_FILE=Path.home()/'.config/sniper/office-research.env'


def pool_reader(office,key_file=None):
    """Exact pool-state fallback for the near-pass sheet; off (None) unless the research Helius URL is in this process's env
    or in the operator's key file (owner-only, mode 600). The key never goes in the unit argv, which /proc shows to anyone."""
    url=os.environ.get('MEMECOIN_RESEARCH_HELIUS_RPC_URL')
    if not url:
        try:
            fd=os.open(key_file or HELIUS_KEY_FILE,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                info=os.fstat(fd)
                ok=stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=4096
                lines=os.read(fd,4096).decode().splitlines() if ok else []
            finally:os.close(fd)
        except (OSError,UnicodeDecodeError):lines=[]   # absent or unreadable file = read off; removing the file is the rollback
        url=next((l.split('=',1)[1].strip() for l in lines if l.startswith('MEMECOIN_RESEARCH_HELIUS_RPC_URL=')),'')
    if not url:return None
    from sniper.office.pool_state_rpc import PoolStateReader
    return PoolStateReader(url,office/'pool_state_rpc.json')


def near_pass_tools(conn,desk_path,desk_dir,clock_file,pool_state_reader=None,cohort_of=lambda:None):
    """Scout tools for the near-pass feed (Gamma prereg near-pass-v1): a bounded list of eligible desk tickets and the
    near_pass sheet for one of them. Read-only on the desk store; the sheet receipt goes to the Office store."""
    from sniper.office import desk_near_pass as N
    def ro():
        c=sqlite3.connect(Path(desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.);c.row_factory=sqlite3.Row
        return c
    def listing(args,ctx):
        c=ro()
        try:out=N.list_near_pass(c,now=time.time(),cohort_id=cohort_of())
        finally:c.close()
        return B.ToolResult(out,{'items':len(out['items'])},'solana',['desk.tickets'],out.get('coverage','unknown'),
                            outcome='success' if out['state']=='known' else 'unknown')
    def one(args,ctx):
        from sniper.persistence import db_memecoin_desk as store
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_memecoin import desk_buyer_ledger
        from sniper.services import memecoin_desk_asof as asof
        from sniper.services.memecoin_desk_intake import clock_sample
        host=time.time();offset,sampled_at=clock_sample(clock_file,now=host)
        if offset is None:
            return B.ToolResult({'state':'unknown','reason':'clock_unknown'},{},'solana',[],'unknown',outcome='unknown')
        try:
            f=get_readonly_connection()
            try:ledger=desk_buyer_ledger(f)
            finally:f.close()
        except Exception as exc:
            ledger=exc
        c=ro()
        try:
            r=N.near_pass_sheet(c,args['ticket_id'],store_conn=conn,desk_dir=desk_dir,now=host,
                                clock_sample={'offset_s':offset,'sampled_at':sampled_at},buyer_ledger=ledger,
                                refresh=asof.refresh,store=store,pool_state_reader=pool_state_reader)
        finally:c.close()
        known=r.get('state')=='known'
        keep={k:r.get(k) for k in ('kind','state','reason','cache_key','sheet_hash','sheet','lenses','row_hash','source_provenance')}
        return B.ToolResult(keep,{},'solana',['desk.tickets','pumpswap.tape'],'ticket_market_cut',
                            outcome='success' if known else ('refused' if r.get('state')=='refused' else 'unknown'))
    return {'desk.near_pass_list':B.Tool(listing,{},'office-near-pass-list-v1'),
            'desk.near_pass_sheet':B.Tool(one,{'ticket_id':str},N.SHEET_CODE_VERSION)}


def build(*,store_path,ledger_path,config_path,guard_path):
    config,pin=_read_config(config_path)
    try:
        root=approved_path(config['content_addressed_release_root'])
        if root!=Path(__file__).resolve().parents[2]:raise ValueError()
        state=approved_path(config['state_root']);office=state/'data/office'
        config_root=approved_path(config['config_root'])
        if config_root!=root/'config/office':raise ValueError()
        expected={'store_path':office/'office.db','ledger_path':office/'ledger/ledger.db',
                  'clock_file':state/'data/clock_offset.json','guard_file':state/'data/office-control/guard/guard.json'}
        if any(approved_path(config[key])!=value for key,value in expected.items()):raise ValueError()
        if Path(store_path)!=expected['store_path'] or Path(ledger_path)!=expected['ledger_path'] or Path(guard_path)!=expected['guard_file']:raise ValueError()
        group=approved_path(config['office_cgroup'])
        if not group.is_relative_to(CGROUP_ROOT) or group.name!='office.slice':raise ValueError()
        virtual='/'+str(group.relative_to(CGROUP_ROOT))
        if Path(config['supervisor_identity']['expected_cgroup']).parent!=Path(virtual):raise ValueError()
        cookie=_identity(config)
        seed_files=config['pilot_seed_launcher_config'].get('files',{})
        file_set,models_name=(pilot_seed.EVAL1_FILES,'pilot_models_eval1.json') if 'pilot_models_eval1.json' in seed_files else (pilot_seed.FILES,'pilot_models.json')
        model_doc=_pinned(config_root,config['model_manifest'],models_name)
        manifest=pilot_models.load(config['model_manifest']['path'],config['model_manifest']['sha256'])
        cadence=_pinned(config_root,config['cadence'],'cadence.json')
        seed_config=config['pilot_seed_launcher_config']
        if seed_config.get('schema')!='office-pilot-seed-launcher-v1' or set(seed_config.get('prompts',{}))!=set(manifest['seats']):raise ValueError()
        for name,sha in seed_config['files'].items():
            if name not in file_set or _hash(read_regular(approved_path(str(config_root/name))))!=sha:raise ValueError()
        if set(seed_config['files'])!=set(file_set) or seed_config['files'][models_name]!=config['model_manifest']['sha256']:raise ValueError()
        required_epoch=pilot_seed.approved_profile(config_root,seed_config)
        if (required_epoch==X.NEW)!=(config['evaluated_epoch_allowed'] is True) or (required_epoch==X.NEW)!=(file_set is pilot_seed.EVAL1_FILES):raise ValueError()
        book=json.loads(read_regular(config_root/'book_policy_pilot.json'))
        if _hash(read_regular(config_root/'host_guard_pilot.json'))!=PILOT_SHA256:raise ValueError()
        if type(config['transport'])is not dict or set(config['transport'])!={'path','sha256'}:raise ValueError()
        transport_doc=json.loads(read_regular(approved_path(config['transport']['path'])))
        if _hash(read_regular(config['transport']['path']))!=config['transport']['sha256']:raise ValueError()
        if transport_doc.get('crew_uid')!=config['supervisor_identity']['crew_uid']:raise ValueError()
        if approved_path(transport_doc['request_root'])!=office/'launcher/requests':raise ValueError()
        if approved_path(transport_doc['socket_path'])!=state/'data/office-control/launcher-sock/launcher.sock':raise ValueError()
        desk_path=approved_path(config['desk_db'])
        if desk_path!=state/'data/memecoin_desk.db':raise ValueError()
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise RuntimeRefused('runtime_bindings_invalid') from None

    def clock():
        try:return _sample(config['clock_file'])['corrected_utc']
        except RuntimeRefused:return None
    initialized=None;epoch_bytes=None
    conn=ledger=None
    def readiness(*,before_seed=False):
        try:
            if _hash(read_regular(config_path))!=pin or _identity(config)!=cookie:return 'runtime_identity_changed'
            if clock() is None:return 'clock_unknown'
            guard=json.loads(read_regular(config['guard_file']))
            if guard.get('guard_policy_version')!='office-host-guard-pilot-v3':return 'guard_policy_unknown'
            reason=external_guard(config['guard_file'])
            if reason:return reason
            if not caps_applied(config['office_cgroup']):return 'caps_unapplied'
            if not before_seed:
                if initialized is None:return 'runtime_not_initialized'
                row=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(initialized['epoch_id'],)).fetchone()
                if row is None or row[0]!=epoch_bytes:return 'epoch_changed'
            return None
        except (OSError,ValueError,TypeError,KeyError):
            return 'runtime_readiness_unknown'
    reason=readiness(before_seed=True)
    if reason:raise RuntimeRefused(reason)
    if config.get('claude_sdk')!=claude_compat.runtime_pins(root):raise RuntimeRefused('claude_sdk_manifest_mismatch')
    reason=claude_compat.startup_refusal()
    if reason:raise RuntimeRefused(reason)
    try:
        conn=S.open_store(store_path);ledger=L.open_ledger(ledger_path)
        observations=O.Buffer(conn,ledger=ledger,clock=clock)
        lease=Lease(conn,clock=clock,ttl_s=config['lease_ttl_s'])
        due=DueWork(store_path,cadence,clock=clock,desk_path=desk_path,exclude_cohort=lambda:eval_cohort())
        def model_guard():
            reason=readiness()
            if reason:return reason
            try:return 'desk_judge_backlog' if due.desk_backlogged() else None
            except RuntimeRefused as exc:
                from sniper.office.scheduler import GuardRefused
                raise GuardRefused('desk_judge_work_unknown',exc.detail) from None
        from sniper.office.scheduler import Scheduler
        scheduler=Scheduler(config['provider_caps'],config['host_children_max'],external_guard=model_guard)
        transports=build_transports(config['transport']['path'],expected_sha256=config['transport']['sha256'],clock=clock)
        runner=None
        def step(context,previous,remaining):return runner(context,previous,remaining)
        def freeze_cut():
            sample=_sample(config['clock_file']);now=sample['corrected_utc']
            return {'basis':'corrected_utc','receipt_cut':now,'knowledge_cut':now,'slot_cut':None,'clock_sample':sample}
        def actor(aid):return _get(conn,'agents',aid)
        memory=MS.OfficeMemory(conn)
        def evaluated():
            row=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(initialized['epoch_id'],)).fetchone() if initialized else None
            return None if row is None else json.loads(row[0]).get('evaluation_allowed')
        def lessons_live():
            row=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(initialized['epoch_id'],)).fetchone() if initialized else None
            return row is not None and json.loads(row[0]).get('lessons_live') is True
        turn_views={}   # ponytail: per-turn cache of what the frame showed; cleared each flush
        book_doc=book
        policy=L.OfficeBookPolicy(**{**book_doc['policy'],'chains':('solana',)})
        def eval_cohort():
            if not eval_active():return None
            row=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(X.NEW,)).fetchone()
            return json.loads(row[0])['record']['near_pass_cohort']['id']
        def eval_identity():
            """Am14 frame identity block: frozen epoch record + book policy terms, never a result or equity number."""
            epoch=json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?',(X.NEW,)).fetchone()[0])
            rec=epoch['record']
            return {'evaluation_allowed':epoch['evaluation_allowed'],'operator_waived_gates':epoch['operator_waived_gates'],
                    'lessons_live':epoch['lessons_live'],'book_id':epoch['book_id'],'capital_usd':rec['book']['capital_usd'],
                    'clip_usd':rec['book']['clip_usd'],'max_concurrent':rec['book']['max_concurrent'],'daily_loss_usd':rec['book']['daily_loss_usd'],
                    'record_sha256':hashlib.sha256(S.canonical_json(rec)).hexdigest(),'book_policy_sha256':rec['book_policy']['sha256'],
                    'required_facts':required_facts(rec['book_policy']['sha256'])}
        def eval_active():return bool(initialized) and initialized.get('epoch_id')==X.NEW
        def fx():
            from scripts.office_paper_watcher import read_fx
            sample=_sample(config['clock_file'])
            return read_fx(state/'data'/'desk',now_host=time.time(),offset=sample['offset_s'])
        def entry_call(order_id):
            row=ledger.execute('SELECT decision_bundle_hash FROM orders WHERE order_id=?',(order_id,)).fetchone()
            if row is None:return None
            bundle=conn.execute("SELECT json_extract(payload_json,'$.decision_id') FROM decision_bundles WHERE row_hash=?",(row[0],)).fetchone()
            rows=J.rows_for(conn,bundle[0]) if bundle else []
            if not rows:return None
            memo=ES._memo(conn,rows[0])
            return {'public_reason':rows[0]['public_reason'],'disproof':J.disproof(memo[0]) if memo else None}
        def work_enabled():return bool(initialized) and initialized.get('epoch_id') in ('office-pilot-discovery-2',X.NEW)
        def observe(context):
            own=actor(context.actor_id)
            messages=conn.execute("SELECT payload_json FROM messages WHERE json_extract(payload_json,'$.actor_id')=? AND json_extract(payload_json,'$.known_at')<=? ORDER BY id DESC LIMIT 10",(context.actor_id,context.cut['knowledge_cut'])).fetchall()
            messages=[json.loads(row[0]) for row in messages]
            frame={'actor':own,'epoch_id':initialized['epoch_id'],'mode':'discovery_only','messages':messages}
            if eval_active():frame.update(mode='paper_eval',epoch=eval_identity())
            if work_enabled():
                refs=[]
                for msg in messages:
                    rid=msg.get('result',{}).get('receipt_id')
                    if rid:
                        receipt=O._receipt(conn,rid)
                        kind=O.REF_KINDS.get(receipt.get('kind'))
                        if kind:refs.append({'kind':kind,'receipt_id':rid})
                with lease.guard(context.lease_generation):
                    frame['work_context']=W.observe(conn,context,now=clock(),supplied_refs=refs)
                if context.role=='judge' and eval_active():
                    # Am11: one host-assigned candidate per round, the same for both Judges (frame carries only it)
                    rnd=JR.frame_round(conn,cut=context.cut['knowledge_cut'])     # rounds are opened by the host pass in flush
                    frame['candidates']={'state':'known','items':[rnd['item']] if rnd['item'] else [],'round_id':rnd['round_id'],
                                         'reason':rnd['reason'],'sheet_age_s_at_cut':rnd.get('sheet_age_s'),'cohort_id':eval_cohort()}
                elif context.role=='judge':frame['candidates']=judge_candidates(conn,cut=context.cut['knowledge_cut'],cohort_id=eval_cohort())
            if eval_active() and context.role in ('head','judge'):
                sol_usd,sol_at=fx()
                frame['book']=OV.view(ledger,book_id=X.BOOK,now=context.cut['knowledge_cut'],sol_usd=sol_usd,sol_at=sol_at,
                    daily_loss_usd=policy.daily_loss_usd,max_concurrent=policy.max_concurrent,entry_call=entry_call)
            if context.role in LS.TARGETS:frame['lessons']=LS.for_role(memory,role=context.role,cut=context.cut['knowledge_cut'],live=lessons_live())
            if eval_active() and context.role in ('head','judge'):
                turn_views[context.decision_id]={'equity_snapshot_id':frame['book']['equity_snapshot_id'],
                    'open_orders':{p['order_id'] for p in frame['book']['positions']},
                    'candidates':{c['receipt_row_hash'] for c in frame.get('candidates',{}).get('items',[])},
                    'lesson_ids':[i['entry_id'] for i in frame.get('lessons',{}).get('items',[])]}
            return frame
        def acknowledge(context):
            if work_enabled():
                with lease.guard(context.lease_generation):W.acknowledge(conn,context,now=clock())
        def validate_refs(context,refs):
            return O.validate_refs(conn,refs,before=context.cut['knowledge_cut'],supplied=W.visible_refs(conn,context))
        def validate_action(context,kind,args):
            if kind=='judge.decide':
                seen=turn_views.get(context.decision_id)
                if not eval_active() or context.role!='judge' or seen is None:raise ValueError('judge_decide_unavailable')
                J.validate(args,candidates=seen['candidates'],open_orders=seen['open_orders'])
            if kind in W.ACTIONS:
                if not work_enabled():raise ValueError('work_epoch_not_active')
                W.validate(conn,context,kind,args)
        def authorize(context,action):
            if action.kind=='memo.commit':
                if set(action.payload)-{'refs','served_model_basis'}!={'task','memo','served_model'}:return False
                if action.payload.get('served_model_basis','unknown') not in ('unknown','echoed','requested_pinned'):return False
                try:validate_refs(context,action.payload.get('refs') or [])
                except ValueError:return False
                return True
            if action.kind in W.ACTIONS:
                try:validate_action(context,action.kind,action.payload)
                except ValueError:return False
            req=B.ToolRequest(context.actor_id,action.kind,action.payload,context.decision_id,context.cut)
            return C.refusal(context,req) is None and action.kind in {'ticket.propose','ticket.approve','org.propose','lesson.propose','judge.decide',*W.ACTIONS}
        def lookup(context,key,request):
            prior=_get(conn,'messages',key)
            if prior is None:return None
            if prior.get('actor_id')!=context.actor_id or prior.get('request')!=request:raise ValueError('runtime_action_identity')
            return prior['result']
        def mutate(context,key,request,*,kind):
            result={'state':'recorded','authority':'untrusted_office_request'}
            if context.role=='judge' and context.book_id!='pilot-floor':raise ValueError('book_binding')
            if request.get('tool')=='unused':raise ValueError('request_invalid')
            with S.write_transaction(conn):
                refs=validate_refs(context,request.get('refs') or [])
                at=clock()
                result['receipt_id']=S.append(conn,'receipts',{'kind':kind,'actor_id':context.actor_id,
                    'generation':context.generation,'role':context.role,'decision_id':context.decision_id,'epoch_id':context.epoch_id,
                    'known_at':at,'outcome':'success','refs':refs,'refs_count':len(refs),'record_kind':'messages','record_id':key})
                _put(conn,'messages',key,{'actor_id':context.actor_id,'role':context.role,'book_id':context.book_id,
                    'decision_id':context.decision_id,'known_at':at,'request':request,'result':result})
            return result
        def written(context,key,request,*,kind):
            validate_action(context,kind,request)
            return W.record(conn,context,key,kind,request,now=clock())
        handlers={name:(lambda ctx,key,req,kind=name:mutate(ctx,key,req,kind=kind)) for name in ('memo.commit','org.propose')}
        handlers.update({name:(lambda ctx,key,req,kind=name:written(ctx,key,req,kind=kind)) for name in W.ACTIONS})
        def lesson(context,key,request):
            prior=_get(conn,'messages',key)
            if prior is not None:
                if prior.get('actor_id')!=context.actor_id or prior.get('request')!=request:raise ValueError('runtime_action_identity')
                return prior['result']
            with S.write_transaction(conn):
                entry=LS.propose(memory,clock,author=context.actor_id,for_role=request['for_role'],text=request['text'],
                                 order_ids=request['order_ids'],evaluated=evaluated())
                result={'state':'recorded','authority':'untrusted_office_request','entry_id':entry}
                _put(conn,'messages',key,{'actor_id':context.actor_id,'role':context.role,'book_id':context.book_id,
                    'decision_id':context.decision_id,'known_at':clock(),'request':request,'result':result})
            return result
        handlers['lesson.propose']=lesson
        def decide(context,key,request):
            prior=_get(conn,'messages',key)
            if prior is not None:
                if prior.get('actor_id')!=context.actor_id or prior.get('request')!=request:raise ValueError('runtime_action_identity')
                return prior['result']
            validate_action(context,'judge.decide',request)
            seen=turn_views[context.decision_id]
            with S.write_transaction(conn):
                rid=J.record(conn,context=context,slot=slot_of(actor(context.actor_id)),args=request,now=clock(),
                             equity_snapshot_id=seen['equity_snapshot_id'],lesson_ids=seen['lesson_ids'])
                result={'state':'recorded','authority':'untrusted_office_request','receipt_id':rid}
                _put(conn,'messages',key,{'actor_id':context.actor_id,'role':context.role,'book_id':context.book_id,
                    'decision_id':context.decision_id,'known_at':clock(),'request':request,'result':result})
            return result
        handlers['judge.decide']=decide
        lookups={name:lookup for name in ('memo.commit','org.propose')}
        lookups['lesson.propose']=lookup
        lookups['judge.decide']=lookup
        lookups.update({name:(lambda ctx,key,req,kind=name:W.lookup(conn,ctx,key,kind,req)) for name in W.ACTIONS})
        supervisor=Supervisor(conn,lease=lease,clock=clock,step=step,freeze_cut=freeze_cut,book_id='pilot-floor',epoch_id=lambda _:initialized['epoch_id'] if initialized else None,
            handlers=handlers,lookups=lookups,authorize=authorize)
        registry={
            'org.read':B.Tool(lambda args,ctx:B.ToolResult({'agents':[json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM agents LIMIT 6')]},{},None,['office.agents'],'observed'),{},'office-runtime-org-v1'),
            'health.read':B.Tool(lambda args,ctx:B.ToolResult(json.loads(read_regular(config['guard_file'])),{},None,['office.guard'],'observed'),{},'office-runtime-guard-v1')}
        def grades(args,ctx):
            reader=L.open_readonly(ledger_path)
            try:page=LS.grade_page(reader,epoch_id=initialized['epoch_id'],evaluated=evaluated(),viewer_role=ctx.role,cursor=args['cursor'])
            except ValueError as exc:return B.ToolResult({'state':'refused','reason':str(exc)},{},None,[],'unknown',outcome='refused')
            finally:reader.close()
            return B.ToolResult(page,{'rows':len(page['rows'])},None,['office.ledger'],'observed',
                                outcome='success' if page['state']=='known' else 'refused')
        registry['grade.read']=B.Tool(grades,{'cursor':str},'office-grade-read-v1')
        for name,tool in near_pass_tools(conn,desk_path,state/'data'/'desk',config['clock_file'],cohort_of=lambda:eval_cohort(),**({'pool_state_reader':reader} if (reader:=pool_reader(office)) else {})).items():
            def gated(args,ctx,handler=tool.handler,name=name):
                if not work_enabled():
                    return B.ToolResult({'reason':'work_epoch_not_active'},{},None,[],'unknown',outcome='refused')
                if name=='desk.near_pass_sheet' and eval_active():
                    reason=ticket_cohort_refusal(desk_path,args.get('ticket_id'),eval_cohort())
                    if reason:return B.ToolResult({'state':'refused','reason':reason},{},'solana',[],'unknown',outcome='refused')
                return handler(args,ctx)
            registry[name]=B.Tool(gated,tool.argument_types,tool.definition_version,tool.authorize)
        broker=B.Broker(conn,ledger,registry,generation_check=supervisor.valid_context)
        runner=AgentStep(models_manifest=manifest,prompts=seed_config['prompts'],scheduler=scheduler,transports=transports,clock=clock,
            actor=actor,observe=observe,read_tool=lambda ctx,name,args:broker.execute(B.ToolRequest(ctx.actor_id,name,args,ctx.decision_id,ctx.cut),context=ctx),
            context_check=supervisor.valid_context,observe_call=observations.capture_call,validate_action=validate_action,validate_memo_refs=validate_refs,acknowledge_context=acknowledge,claude_debug_stderr=config.get('claude_debug_stderr',True),discovery_only=config['discovery_only'] is True and config['evaluated_epoch_allowed'] is False,
            evaluated_pinned=config['evaluated_epoch_allowed'] is True and config['discovery_only'] is False)
        def initialize(gen):
            nonlocal initialized,epoch_bytes
            def trusted(by):
                if by!='crew' or readiness(before_seed=True) is not None:return False
                try:lease.check(gen)
                except Exception:return False
                return True
            if required_epoch==X.NEW:
                initialized=X.adopted_runtime(conn,prompts=seed_config['prompts'],models=manifest,book_policy=book['policy'])
                if initialized is None:raise RuntimeRefused('eval_transition_required')
                reason=X.book_bound(ledger)
                if reason:raise RuntimeRefused(reason)
                epoch_bytes=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(X.NEW,)).fetchone()[0]
                return
            resolved,_=pilot_seed.resolve_manifest(config_root=config_root,launcher_config=lambda by:deepcopy(seed_config),launcher_check=trusted,by='crew')
            initialized=E.adopted(conn,resolved)
            if initialized is None:
                if required_epoch!=E.OLD:raise RuntimeRefused('epoch_transition_required')
                initialized=pilot_seed.seed_pilot_org(conn,config_root=config_root,launcher_config=lambda by:deepcopy(seed_config),launcher_check=trusted,clock=clock,by='crew')
                bootstrap=book['bootstrap']
                trader.bootstrap_pilot(ledger,epoch_id=bootstrap['epoch_id'],book_id=bootstrap['book_id'],clip_usd=book['policy']['clip_usd'],
                    rule_version=bootstrap['rule_version'],capital_usd=bootstrap['capital_usd'],actor=bootstrap['actor'],now=clock())
            elif initialized['epoch_id']!=required_epoch:raise RuntimeRefused('prompt_epoch_mismatch')
            epoch_bytes=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(initialized['epoch_id'],)).fetchone()[0]
        def tasks():
            reason=readiness()
            if reason:raise RuntimeRefused(reason)
            return due.tasks()
        def backlogged():
            reason=readiness()
            if reason:raise RuntimeRefused(reason)
            return scheduler.live_backlogged() or due.backlogged()
        next_work_prune=0.
        def advance_rounds():
            """Am14 host pass: close the open round, re-sheet the newest unassigned admitted ticket, open the next round."""
            from sniper.office import desk_near_pass as N
            def tickets(now):
                c=sqlite3.connect(Path(desk_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1.);c.row_factory=sqlite3.Row
                try:return N.list_near_pass(c,now=time.time(),cohort_id=eval_cohort())['items']
                finally:c.close()
            def sheet(ticket_id):
                got=registry['desk.near_pass_sheet'].handler({'ticket_id':ticket_id},None).result
                if got.get('state')=='known' and got.get('row_hash'):
                    item=candidate_by_row(conn,got['row_hash'])
                    return item if item is not None else {'unavailable':True,'state':'unknown','reason':'receipt_unverified'}
                return {'unavailable':True,'state':got.get('state'),'reason':got.get('reason')}   # the tool's own reason (Gamma 01:02Z)
            judges=[a['agent_id'] for a in (json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM agents LIMIT 6')) if a['role']=='Judge']
            # no outer transaction: a sheet refresh does tape/RPC I/O; advance writes each round atomically itself
            JR.advance(conn,epoch_id=X.NEW,now=clock(),judges=judges,tickets=tickets,sheet=sheet,clock=clock)
        def flush():
            nonlocal next_work_prune
            health=observations.flush()
            if eval_active():
                def settle():
                    sol_usd,sol_at=fx()
                    ES.settle(conn,ledger,policy=policy,now=clock(),sol_usd=sol_usd,sol_at=sol_at,candidate_of=lambda h:candidate_by_row(conn,h))
                    ES.record_epoch_end(conn,ledger,now=clock())
                health=eval_housekeeping(health,settle=settle,rounds=advance_rounds)
                turn_views.clear()
            try:
                if time.monotonic()>=next_work_prune:
                    W.prune(conn,now=clock())
                    next_work_prune=time.monotonic()+3600.
            except (sqlite3.Error,ValueError):
                health=dict(health,state='partial',last_error='work_retention_failed')
            return health
        def close():
            flush()
            conn.close();ledger.close()
        return {'supervisor':supervisor,'scheduler':scheduler,'transports':transports,'initialize':initialize,'tasks':tasks,
                'flush_observations':flush,'observation_health':observations.health,'live_backlogged':backlogged,'desk_judge_backlogged':due.desk_backlogged,'close':close,
                'deadline_s':config['turn_deadline_s'],'max_steps':config['max_steps']}
    except BaseException:
        if conn is not None:conn.close()
        if ledger is not None:ledger.close()
        raise
