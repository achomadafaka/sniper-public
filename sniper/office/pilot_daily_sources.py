"""Optional, bounded read-only observations approved for discovery reports."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from pathlib import Path

from .pilot_daily import LIMIT, metric, numeric, readonly, unknown
from .store import canonical_json


def bounded_json(path):
    with Path(path).open('rb') as stream:
        raw=stream.read(4194305)
    if len(raw)>4194304:raise ValueError('file_budget_exceeded')
    return raw


def host_samples(path, start, end):
    source='host_baseline:office-host-baseline-v2'
    try:
        if path is None:raise ValueError('not_bound')
        lines=bounded_json(path).splitlines()
        if len(lines)>LIMIT:raise ValueError('row_budget_exceeded')
        samples=[json.loads(line) for line in lines]
        expected='host-'+datetime.fromtimestamp(start,timezone.utc).date().isoformat()+'.jsonl'
        if Path(path).name!=expected or any(not isinstance(p,dict) or not numeric(p.get('at_host')) for p in samples):
            raise ValueError('sample_clock_unknown')
        known=[p for p in samples if p.get('clock_state')=='known']
        if any(not numeric(p.get('corrected_at')) or not start<=p['corrected_at']<end or p.get('office_off') is not True for p in known):
            raise ValueError('sample_day_or_office_state_unknown')
        daily=samples
        result=dict(label='host sampler, 1/min, Office-off baseline',
            samples=metric(len(daily),source+':sample_count'),clock_unknown=metric(len(daily)-len(known),source+':clock_exclusions'),
            maintenance_note='Raw counts retain maintenance windows; exclusions require an Alpha-authored note.')
        for key in ('cpu_pressure','io_pressure','funded_heartbeat_age_s','memory_available_min','idle_cores_min'):
            values=[p.get(key) for p in known]
            valid=bool(values) and all(numeric(v) and v>=0 and (v<=1 if key in ('cpu_pressure','io_pressure') else True) for v in values)
            if not valid:
                result[key]=unknown(source+':'+key,'missing_invalid_or_empty_samples');continue
            values.sort()
            def quantile(fraction):
                at=(len(values)-1)*fraction; lo=int(at); hi=min(lo+1,len(values)-1)
                return values[lo]+(values[hi]-values[lo])*(at-lo)
            result[key]={name:metric(value,source+':'+key+'.'+name) for name,value in (
                ('min',values[0]),('max',values[-1]),('p50',quantile(.5)),('p90',quantile(.9)))}
        return result
    except (OSError,ValueError,TypeError,RecursionError,KeyError):
        return unknown(source,'missing_invalid_or_bounded_out')


def intake_polls(root, day, start, end):
    result={}
    for feed in ('dexscreener','fomo','pumpfun'):
        source='intake/'+feed+'/'+day+':polls'
        try:
            if root is None:raise ValueError('not_bound')
            with readonly(Path(root)/feed/(day+'.db')) as conn:
                rows=conn.execute('SELECT received_at,status,malformed,error FROM polls WHERE feed=? AND received_at>=? AND received_at<? ORDER BY received_at LIMIT ?', (feed,start,end,LIMIT+1)).fetchall()
            if len(rows)>LIMIT or any(not numeric(p['received_at']) for p in rows):raise ValueError('poll_bounds')
            ok=[p for p in rows if p['status']=='ok' and not p['error']]
            gaps=[b['received_at']-a['received_at'] for a,b in zip(ok,ok[1:])]
            malformed=[p['malformed'] for p in rows]
            result[feed]=dict(polls=metric(len(rows),source+':day_count'),ok=metric(len(ok),source+':ok'),failed=metric(len(rows)-len(ok),source+':status_or_error'),
                malformed=metric(sum(malformed),source+':malformed') if all(type(v) is int and v>=0 for v in malformed) else unknown(source+':malformed','invalid_malformed_count'),
                max_gap_s=metric(max(gaps),source+':successful_poll_max_gap') if gaps else unknown(source+':successful_poll_max_gap','fewer_than_two_successful_polls'),
                coverage_state=unknown(source+':coverage','cadence_and_coverage_producer_not_frozen'))
        except (OSError,ValueError,TypeError,RecursionError,sqlite3.Error):
            # SQLite/schema/transport failures are unknown, never empty successful polls.
            result[feed]=unknown(source,'missing_invalid_or_bounded_out')
    return result


def report_digest(report):
    copy=json.loads(canonical_json(report))
    copy['Frame'].pop('report_sha256',None)
    return hashlib.sha256(canonical_json(copy)).hexdigest()


def paper_mark_counter(path, *, now_host):
    source='D_paper_worker:desk_paper_engine.status'
    result=dict(name='mark_log_errors',scope='D paper operational log; cumulative since process start',
        total=unknown(source,'missing_invalid_stale_or_failed_worker'),
        today=unknown(source+':delta','day_boundary_samples_unavailable'),flagged=None)
    try:
        if path is None:raise ValueError('not_bound')
        from .launcher_client import read_regular
        value=json.loads(read_regular(path))
        ts=value['ts_host'];started=value['started_at_host'];run=value['run_id'];count=value['mark_log_errors']
        if (value['version']!='desk-paper-engine-status-v1' or not numeric(ts) or not 0<=now_host-ts<=5
                or not numeric(started) or not 0<=started<=ts or not isinstance(run,str) or len(run)!=32
                or any(c not in '0123456789abcdef' for c in run) or type(count) is not int or count<0 or value['engine_error'] is not False):
            raise ValueError('invalid_status')
        result.update(total=metric(count,source+':run_'+run),flagged=count>0,counter_epoch=run,
            sampled_at_host=metric(ts,source+':ts_host'),started_at_host=metric(started,source+':started_at_host'))
    except (OSError,ValueError,TypeError,KeyError,RecursionError):pass
    return result


def counter_deltas(counts, previous_path, day, generated_at):
    try:
        if previous_path is None:raise ValueError('not_bound')
        previous=json.loads(bounded_json(previous_path))
        frame=previous['Frame']
        if (frame['report_sha256']!=report_digest(previous)
                or frame['day']!=(date.fromisoformat(day)-timedelta(days=1)).isoformat()
                or not numeric(frame['generated_at']['value']) or frame['generated_at']['value']>=generated_at):
            raise ValueError('previous_report_invalid')
        old={p['name']:p for p in previous['Counters']}
    except (OSError,ValueError,TypeError,RecursionError,KeyError):
        old={}
    for item in counts:
        value=item['total']['value']; previous=old.get(item['name'],{})
        if not isinstance(previous.get('total'),dict):continue
        prior=previous['total'].get('value')
        if item['name']=='mark_log_errors':
            if (not isinstance(previous.get('sampled_at_host'),dict) or not item.get('counter_epoch') or item['counter_epoch']!=previous.get('counter_epoch')
                    or not numeric(previous.get('sampled_at_host',{}).get('value'))
                    or item.get('sampled_at_host',{}).get('value',-1)<=previous['sampled_at_host']['value']
                    or item.get('started_at_host')!=previous.get('started_at_host')):continue
        if type(value) is int and type(prior) is int and 0<=prior<=value:
            item['today']=metric(value-prior,'report_chain:verified_previous_report.'+item['name'])
            item['interval']='between report observations; not an exact midnight counter sample'
