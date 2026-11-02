"""Completed-day discovery observations. No evaluation store, valuation or delivery."""
import calendar
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from . import auditor, store

BANNER = 'PILOT - DISCOVERY ONLY - NOT EVIDENCE - no evaluation epoch'
LIMIT = 10000
BYTE_LIMIT = 4194304


class ScopeRefused(ValueError):
    pass


def _pilot_scope(conn, start, end):
    """Prove scope from bounded metadata, independently of public-row display limits."""
    n,size=conn.execute('SELECT count(*),sum(length(CAST(payload_json AS BLOB))) FROM (SELECT payload_json FROM epochs LIMIT ?)',(LIMIT+1,)).fetchone()
    if n>LIMIT or (size or 0)>BYTE_LIMIT:
        raise ScopeRefused('pilot_epoch_scope_unknown')
    for kind,opened_type,opened,closed_type,closed in conn.execute("SELECT substr(json_extract(payload_json,'$.kind'),1,33),json_type(payload_json,'$.opened_at'),json_extract(payload_json,'$.opened_at'),json_type(payload_json,'$.closed_at'),json_extract(payload_json,'$.closed_at') FROM epochs"):
        if (kind not in ('discovery','evaluated') or opened_type not in ('integer','real')
                or not numeric(opened) or opened<0 or closed_type not in ('null','integer','real')
                or closed is not None and (not numeric(closed) or closed<opened)):
            raise ScopeRefused('pilot_epoch_scope_unknown')
        if kind=='evaluated' and opened<end and (closed is None or closed>start):
            raise ScopeRefused('pilot_report_contains_evaluated_epoch')
    return True


def metric(value, source, reason=None):
    return dict(state='unknown' if reason else 'observed', value=None if reason else value,
                source=source, **({'reason':reason} if reason else {}))


def unknown(source, reason='historical_observation_unavailable'):
    return metric(None,source,reason)


def numeric(value):
    return type(value) in (int,float) and math.isfinite(value)


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic()+2.
    conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def rows(conn, table):
    # Names are module constants; no caller SQL identifiers.
    key = 'seq' if table in store.APPEND_ONLY_TABLES else 'id'
    result = conn.execute('SELECT '+key+',substr(payload_json,1,65537) FROM '+table+' LIMIT ?', (LIMIT+1,))
    parsed=[]; size=0
    for row in result:
        size+=len(row[1].encode('utf-8'))
        if len(parsed)>=LIMIT or size>BYTE_LIMIT:
            raise ValueError('collection_budget_exceeded')
        if len(row[1])>65536:
            raise ValueError('row_size_exceeded')
        body=json.loads(row[1])
        if not isinstance(body,dict):
            raise ValueError('invalid_row')
        parsed.append((str(row[0]),body))
    return parsed


def build(root, *, day, generated_at, code_sha, pin_path=None, host_path=None, intake_root=None, previous_report=None, paper_status=None):
    day=date.fromisoformat(day)
    start=datetime.combine(day,datetime.min.time(),timezone.utc).timestamp()
    end=start+86400
    if not numeric(generated_at) or generated_at<end:
        raise ValueError('completed_utc_day_required')
    if not isinstance(code_sha,str) or len(code_sha)!=40 or any(c not in '0123456789abcdef' for c in code_sha):
        raise ValueError('code_sha_required')
    root=Path(root)
    def during(p, field):
        return numeric(p.get(field)) and start<=p[field]<end
    tables=('agents','heartbeats','turn_runs','tickets','org_changes','opportunity_log','opportunity_coverage','opportunity_gaps','epochs')
    data={}; errors={}; chains={}; scope_proven=False; store_opened=False
    try:
        with readonly(root/'office.db') as conn:
            store_opened=True
            if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0]!='office':
                raise ValueError('not_public_office_store')
            scope_proven=_pilot_scope(conn,start,end)
            for table in tables:
                try:data[table]=rows(conn,table)
                except (sqlite3.Error,ValueError,TypeError,RecursionError):errors[table]='unreadable_or_bounded_out'
            try:
                if pin_path is None:raise ValueError('missing_pin')
                with Path(pin_path).open('rb') as stream:
                    raw=stream.read(65537)
                if len(raw)>65536:raise ValueError('oversized_pin')
                pins=json.loads(raw)
                if not isinstance(pins,dict):raise ValueError('invalid_pin')
            except (OSError,ValueError,TypeError,RecursionError):pins={}
            for table in sorted(store.APPEND_ONLY_TABLES-store.PRIVATE_TABLES):
                source=table+':chain_exact_pinned_head'
                try:
                    n,size=conn.execute('SELECT count(*),sum(length(CAST(payload_json AS BLOB))) FROM (SELECT payload_json FROM '+table+' LIMIT ?)',(LIMIT+1,)).fetchone()
                    if n>LIMIT or (size or 0)>BYTE_LIMIT:
                        raise ValueError('chain_budget_exceeded')
                    good,_=store.verify_chain(conn,table)
                    row=conn.execute('SELECT seq,row_hash FROM '+table+' ORDER BY seq DESC LIMIT 1').fetchone()
                    head=list(row) if row else [0,'']
                    pin=pins.get(table)
                    valid_pin=isinstance(pin,list) and len(pin)==2 and type(pin[0]) is int and pin[0]>=0 and isinstance(pin[1],str)
                    chains[table]=dict(verification=metric('verified',source) if good and valid_pin and pin==head else unknown(source,'chain_invalid_or_pin_missing_stale'),
                                       pinned_head_seq=metric(pin[0],source) if isinstance(pin,list) and len(pin)==2 and type(pin[0]) is int else unknown(source,'pin_missing'))
                except (sqlite3.Error,ValueError,TypeError,RecursionError):chains[table]=dict(verification=unknown(source,'chain_read_failed'))
    except ScopeRefused:
        raise
    except (OSError,sqlite3.Error,ValueError,TypeError,RecursionError,IndexError):
        errors.update(dict.fromkeys(tables,'public_store_unavailable'))
    if not scope_proven and (store_opened or (root/'office.db').exists()):
        raise ScopeRefused('pilot_epoch_scope_unknown')
    def count(table, query, predicate=lambda p:True):
        source=table+':'+query
        if table not in data:return unknown(source,errors.get(table,'missing_source'))
        return metric(sum(predicate(p) for _,p in data[table]),source)
    beats=dict(data.get('heartbeats',[]))
    def turn_count(identity,state=None):
        source='turn_runs:day_'+(state or 'started')
        if 'turn_runs' not in data:return unknown(source,'turn_source_unavailable')
        selected=[p for _,p in data['turn_runs'] if p.get('actor_id')==identity and (state is None or p.get('state')==state)]
        field='created_at' if state is None else 'ended_at'
        if any(not numeric(p.get(field)) for p in selected):return unknown(source,'turn_timestamp_unknown')
        return metric(sum(during(p,field) for p in selected),source)
    agents=[]
    for identity,p in data.get('agents',[]):
        beat=beats.get(identity,{})
        at=beat.get('at')
        agents.append(dict(agent_id=identity,**{k:p[k] if isinstance(p.get(k),str) else 'unknown' for k in ('role','pod','provider','family','model','state')},
            generation=metric(p['generation'],'agents:current_generation') if type(p.get('generation')) is int else unknown('agents:current_generation'),
            observed_at=metric(generated_at,'host:generated_at'),historical_state=unknown('agents:day_end_state'),
            last_heartbeat_age_s=metric(generated_at-at,'heartbeats:latest_age') if numeric(at) and at<=generated_at else unknown('heartbeats:latest_age'),
            turns_started=turn_count(identity),turns_completed=turn_count(identity,'completed'),turns_failed=turn_count(identity,'failed')))
    seats=dict(items=agents,roster_size=count('agents','current_active_model_roster',lambda p:p.get('state')=='active' and p.get('provider') in ('claude','codex')),roster_max_model_agents=metric(4,'config/office/host_guard_pilot.json:v2.roster_max_model_agents'),
               changes=[dict(seq=metric(int(i),'org_changes:day_seq')) for i,p in data.get('org_changes',[]) if during(p,'at')])
    states={'opened':'proposed','approved by Head':'head_approved','delivered to crew':'queued','acked':'crew_acknowledged','closed':'office_verified','rejected':'rejected'}
    tickets=dict(counts={name:unknown('tickets:day_'+state,'mutable_state_not_day_event') for name,state in states.items()},
        by_kind=unknown('tickets:day_kind','ticket_kind_not_recorded'),oldest_open_age_s=unknown('tickets:day_oldest_open'),duplicates_rejected=unknown('tickets:duplicates','duplicate_rejections_not_persisted'))
    tickets['current_state_only']=True
    tickets['observed_at']=metric(generated_at,'host:generated_at')
    tickets['current_state_counts']={name:count('tickets','current_state_'+state,lambda p,state=state:p.get('state')==state) for name,state in states.items()}
    if any(not isinstance(p.get('state'),str) for _,p in data.get('tickets',[])):
        tickets['current_state_counts']={name:unknown('tickets:current_state_'+state,'ticket_state_unknown') for name,state in states.items()}
    candidates=dict(eligible_coins=count('opportunity_log','day_eligible',lambda p:during(p,'first_eligible_at')),
        **{k:unknown('opportunity_log:'+k,'candidate_disposition_contract_not_bound') for k in ('processed_by_agent','risk_veto','unprocessed','by_pod','by_shift')},
        source_coverage=unknown('opportunity_coverage:expected_source_day','expected_source_roster_not_bound'),
        gap_minutes=unknown('opportunity_gaps:expected_source_day','expected_source_roster_not_bound'))
    bad_clocks=[p for _,p in data.get('opportunity_log',[]) if not numeric(p.get('first_eligible_at')) or p['first_eligible_at']<0]
    candidates['excluded_clock_rows']=metric(len(bad_clocks),'opportunity_log:unassignable_clock_rows') if 'opportunity_log' in data else unknown('opportunity_log:unassignable_clock_rows','source_unavailable')
    if bad_clocks:
        candidates['eligible_coins']=unknown('opportunity_log:day_eligible','opportunity_clock_unknown')
    positions=[]; paper_error=None
    try:
        with readonly(root/'ledger'/'ledger.db') as conn:
            orders=conn.execute("SELECT order_id,network_id,mint,filled_at,closed_at,state FROM orders WHERE book_kind='discovery' AND filled_at<? AND (closed_at IS NULL OR closed_at>=?) LIMIT ?",(end,start,LIMIT+1)).fetchall()
            if len(orders)>LIMIT:raise ValueError('row_budget_exceeded')
            for p in orders:
                mark=conn.execute('SELECT binding_kind,source FROM marks WHERE order_id=? AND at<? ORDER BY at DESC,id DESC LIMIT 1',(p['order_id'],end)).fetchone()
                binding=mark['binding_kind'] if mark and mark['binding_kind'] else 'binding unknown'
                positions.append(dict(coin_id=dict(network_id=p['network_id'],mint=p['mint']),order_id=p['order_id'],
                    entry_time=metric(p['filled_at'],'orders:discovery_filled_day'),exit_time=metric(p['closed_at'],'orders:closed_at') if numeric(p['closed_at']) and p['closed_at']<end else unknown('orders:closed_at','not_closed_at_cut'),
                    state=p['state'] if numeric(p['closed_at']) and p['closed_at']<end else 'open_at_cut',binding_kind=binding,
                    price_source_stratum=mark['source'] if mark else 'unknown',censored_reason='pool_binding_unknown' if binding=='binding unknown' else 'strict_valuation_not_bound',
                    valuation=metric('unpriced','marks:no_strict_asof_valuation'),descriptive_net_usd=unknown('cashflows:position_only','authenticated_asof_execution_not_bound'),
                    label='non-evidence; buy-tip priced only; execution costs partly unknown'))
    except (OSError,sqlite3.Error,ValueError):paper_error='ledger_unavailable_or_bounded_out'
    paper=dict(positions=positions,state='unknown' if paper_error else 'observed',
        priced=unknown('marks:priced','strict_valuation_not_bound'),open=unknown('orders:open_at_cut','historical_state_not_bound'),
        censored_by_reason=unknown('marks:censored_by_reason',paper_error or 'strict_valuation_not_bound'))
    if not paper_error:
        paper['open']=metric(sum(p['state']=='open_at_cut' for p in positions),'orders:discovery_open_at_cut')
        reasons=sorted({p['censored_reason'] for p in positions})
        paper['censored_by_reason']={reason:metric(sum(p['censored_reason']==reason for p in positions),'marks:unpriced_'+reason) for reason in reasons}
    audit=beats.get('auditor',{})
    counters=audit.get('counters',{}) if numeric(audit.get('at')) and audit['at']<=generated_at else {}
    if not isinstance(counters,dict):counters={}
    counts=[]
    for name in auditor.COUNTERS:
        value=counters.get(name)
        known=type(value) is int and value>=0
        counts.append(dict(name=name,total=metric(value,'heartbeats:auditor_reported_'+name) if known else unknown('heartbeats:auditor_'+name),
            today=unknown('heartbeats:counter_day_delta','day_boundary_samples_unavailable'),flagged=known and value>0))
    from .pilot_daily_sources import host_samples, intake_polls, counter_deltas, report_digest, paper_mark_counter
    counts.append(paper_mark_counter(paper_status,now_host=time.time()))
    counts.sort(key=lambda p:not p['flagged'])
    counter_deltas(counts,previous_report,day.isoformat(),generated_at)
    result = {'Frame':dict(banner=BANNER,day=day.isoformat(),generated_at=metric(generated_at,'host:corrected_generated_at'),code_sha=code_sha,clock_sample=unknown('host:clock_sample','clock_receipt_not_bound')),
        'Seats':seats,'Tickets':tickets,'Candidates':candidates,'Paper fills (non-evidence)':paper,
        'Cost (informational)':dict(monthly_usd=metric(800,'config/office/cost_allocation_rule.json:aa0e9a58'),daily_usd=metric(800/calendar.monthrange(day.year,day.month)[1],'config/office/cost_allocation_rule.json:aa0e9a58.completed_utc_day'),netted=False),
        'Guard state':dict(manifest_in_force=unknown('host:guard_manifest_receipt'),pilot_flag=unknown('host:pilot_flag'),pauses=unknown('host:day_pauses'),breaches=unknown('host:day_breaches'),resume_times=unknown('host:day_resumes'),missing_samples=unknown('host:missing_samples'),headroom=unknown('host:headroom','cpu_io_fractions_memory_bytes_heartbeat_age_not_bound')),
        'Counters':counts,'Data health':dict(chains=chains or unknown('office:chain_verification','public_store_unavailable'),clock_sample_age_s=unknown('host:clock_sample_age'),model_outcomes=unknown('turn_runs:model_outcomes','provider_outcome_contract_not_bound'),fallbacks=unknown('turn_runs:fallbacks','fallback_event_contract_not_bound'))}
    result['Guard state']['headroom']=dict(current=unknown('host:current_headroom','historical_baseline_is_not_current_headroom'),office_off_baseline=host_samples(host_path,start,end))
    result['Frame']['evaluation_scope']=metric('no_overlapping_evaluated_epoch','epochs:bounded_scope_metadata') if scope_proven else unknown('epochs:bounded_scope_metadata','office_store_missing')
    result['Candidates']['intake_polls']=intake_polls(intake_root,day.isoformat(),start,end)
    result['Frame']['report_sha256']=report_digest(result)
    return result


def render(report):
    return '\n\n'.join(['# '+BANNER]+['## '+name+'\n\n```json\n'+json.dumps(value,sort_keys=True,indent=2,allow_nan=False).replace('`','\\u0060')+'\n```' for name,value in report.items()])+'\n'
