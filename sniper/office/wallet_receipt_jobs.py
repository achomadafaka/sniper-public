"""Approved retrospective research jobs. No forward cursors, watches, graph or funded writes."""
import base58
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from solders.pubkey import Pubkey
from .chain_data import FINAL_TTL, PACE, SIGNATURE, _Refused
from .wallet_launch_proof import launch_facts

SCHEMA='''
CREATE TABLE IF NOT EXISTS receipt_jobs(job_id TEXT PRIMARY KEY,signature TEXT NOT NULL,mint TEXT NOT NULL,creator_argument TEXT NOT NULL,slot INTEGER NOT NULL,curve TEXT NOT NULL,source_path TEXT NOT NULL,requested_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',reason TEXT,attempts INTEGER NOT NULL DEFAULT 0,last_try REAL,next_try REAL NOT NULL DEFAULT 0,known_at REAL,profile_json TEXT,UNIQUE(signature,mint));
CREATE INDEX IF NOT EXISTS receipt_jobs_due ON receipt_jobs(state,next_try,last_try,job_id);
CREATE TABLE IF NOT EXISTS receipt_schedule(id INTEGER PRIMARY KEY CHECK(id=1),lane TEXT NOT NULL,clock_floor REAL NOT NULL);
'''


def _finite(value):return type(value) in (int,float) and math.isfinite(value) and value>0


def _address(value):
    if not isinstance(value,str) or str(Pubkey.from_string(value))!=value:raise ValueError('bad_receipt_target')
    return value


def _target(target):
    launch=target['launch'];signature=launch['signature'];mint=_address(launch['mint']);creator=_address(target['expected_creator'])
    if (not isinstance(signature,str) or not SIGNATURE.fullmatch(signature) or len(base58.b58decode(signature))!=64
            or not any(base58.b58decode(signature)) or launch.get('creator')!=creator
            or type(launch['slot']) is not int or not 0<launch['slot']<2**63
            or not isinstance(target['source_path'],str) or not 0<len(target['source_path'])<=1024 or '\0' in target['source_path']):
        raise ValueError('bad_receipt_target')
    job_id=hashlib.sha256(json.dumps([signature,mint],separators=(',',':')).encode()).hexdigest()
    return (job_id,signature,mint,creator,launch['slot'],_address(launch['curve']),target['source_path'])


def _connect(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=0)
    c.row_factory=sqlite3.Row
    return c


def enqueue_targets(path,targets,*,now):
    """Explicit offline host action AFTER job approval; consumer never reads a targets file."""
    if not _finite(now) or not isinstance(targets,list) or not 1<=len(targets)<=100:return dict(state='refused',reason='bad_receipt_targets')
    try:rows=[_target(t) for t in targets]
    except (ValueError,TypeError,KeyError,AttributeError):return dict(state='refused',reason='bad_receipt_targets')
    c=None
    try:
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(path,timeout=0);c.executescript(SCHEMA);c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT OR IGNORE INTO receipt_schedule VALUES(1,'receipt',?)",(now,))
        added=existing=0
        for row in rows:
            old=c.execute('SELECT signature,mint,creator_argument,slot,curve FROM receipt_jobs WHERE job_id=?',(row[0],)).fetchone()
            if old is not None:
                if tuple(old)!=row[1:6]:raise ValueError('receipt_target_conflict')
                existing+=1;continue
            c.execute('INSERT INTO receipt_jobs(job_id,signature,mint,creator_argument,slot,curve,source_path,requested_at) VALUES(?,?,?,?,?,?,?,?)',(*row,now));added+=1
        c.commit();return dict(state='known',added=added,existing=existing)
    except ValueError as exc:
        if c is not None:c.rollback()
        return dict(state='refused',reason=str(exc))
    except (OSError,sqlite3.Error):
        if c is not None:c.rollback()
        return dict(state='unknown',reason='receipt_store_unknown')
    finally:
        if c is not None:c.close()


def _store_result(path,job,*,now,reason,profile,next_try):
    c=_connect(path)
    try:
        with c:
            changed=c.execute("UPDATE receipt_jobs SET state=?,reason=?,next_try=?,known_at=?,profile_json=? WHERE job_id=? AND state='pending' AND signature=? AND mint=? AND creator_argument=? AND slot=? AND curve=?",
                      ('known' if profile else 'pending',reason,next_try,now if profile else None,json.dumps(profile,sort_keys=True) if profile else None,
                       job['job_id'],job['signature'],job['mint'],job['creator_argument'],job['slot'],job['curve'])).rowcount
            if changed!=1:raise sqlite3.OperationalError('receipt_job_state_changed')
            c.execute('UPDATE receipt_schedule SET clock_floor=MAX(clock_floor,?) WHERE id=1',(now,))
    finally:c.close()


def _run_one(chain,path,job):
    now=chain.clock();floor=max(job['requested_at'],job['last_try'] or 0)
    if not _finite(now) or now<floor:return dict(state='unknown',reason='receipt_clock_unknown',pending=True,calls_attempted=0),False
    c=_connect(path)
    try:
        with c:
            c.execute("UPDATE receipt_jobs SET attempts=attempts+1,last_try=?,reason='retrieving' WHERE job_id=? AND state='pending'",(now,job['job_id']))
            c.execute('UPDATE receipt_schedule SET clock_floor=MAX(clock_floor,?) WHERE id=1',(now,))
    finally:c.close()
    params=[job['signature'],dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')]
    cache_key=hashlib.sha256(json.dumps(['getTransaction',params],sort_keys=True).encode()).hexdigest()
    profile=None;reason=None;advance=False;cached=False;provider_attempts=None;actual=None;retry=now+PACE
    try:
        tx,cached,fetched=chain.call('getTransaction',params,ttl=FINAL_TTL)
        advance=True;provider_attempts=int(not cached);observed=chain.clock()
        if not _finite(observed) or observed<now or not _finite(fetched) or fetched>observed:
            reason='receipt_clock_unknown'
        elif tx is None:reason='not_found_at_finalized'
        else:
            facts=launch_facts(tx,commitment='finalized')
            if (not isinstance(tx,dict) or tx.get('transaction',{}).get('signatures',[None])[0]!=job['signature']
                    or tx.get('slot')!=job['slot']):reason='identity_conflict'
            elif facts['state']!='known':reason=facts.get('reason','launch_evidence_unproven')
            else:
                matches=[f for f in facts['items'] if f['mint']==job['mint']]
                if len(matches)!=1:reason='launch_not_proven'
                else:
                    actual=matches[0]
                    if actual['creator_argument']!=job['creator_argument'] or actual['curve']!=job['curve']:reason='identity_conflict'
                    elif not 0<actual['block_time']<=fetched<=observed:reason='receipt_clock_unknown'
                    else:
                        profile=dict(actual,state='known',tool='watch_verified_creation',asked=dict(signature=job['signature']),network_id='solana',provenance='research_rpc',
                                     known_at=observed,fetched_at=fetched,retrospective=True,availability='retrospective_rpc',raw_cache_key=cache_key,
                                     receipt_sha256=hashlib.sha256(json.dumps(tx,sort_keys=True,allow_nan=False).encode()).hexdigest())
        if reason and _finite(fetched):retry=max(retry,fetched+FINAL_TTL)
    except _Refused as exc:
        reason=exc.reason;provider_attempts=int(exc.dialed);advance=exc.dialed
        if reason=='paced':retry=now  # Keep the lane due; a new delay can phase-lock it behind regular polls.
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,AttributeError):
        reason='receipt_store_or_evidence_unknown'
        advance=True  # Yield the consumed budget slot; physical admission remains unknown.
    observed=chain.clock()
    if not _finite(observed) or observed<now:
        profile=None;reason='receipt_clock_unknown';observed=now
    try:_store_result(path,job,now=observed,reason=reason,profile=profile,next_try=max(retry,observed if reason=='paced' else observed+PACE))
    except (OSError,sqlite3.Error):return dict(state='unknown',reason='receipt_store_unknown',pending=None,calls_attempted=1,provider_attempts=provider_attempts),advance
    last=dict(job_id=job['job_id'],signature=job['signature'],mint=job['mint'],state='known' if profile else 'unknown',reason=reason,known_at=observed if profile else None)
    if actual is not None:last['actual_identity']={k:actual[k] for k in ('developer','creator_argument','mint','slot')}
    return dict(state='partial',reason='receipt_job_processed',pending=None,calls_attempted=1,provider_attempts=provider_attempts,cache_hits=int(cached),last_job=last),advance


def poll(chain,collector,path,*,max_calls):
    """One owner, shared attempt budget; None means no new regular-poll snapshot.

    Regular progress uses Collector's existing attempt/cache counters. Other processes can
    race that preflight gate: this schedules this owner fairly, not all shared-key actors.
    """
    if type(max_calls) is not int or not 1<=max_calls<=100:raise ValueError('bad_call_budget')
    path=Path(path)
    if not path.is_file():return collector.tick(max_calls=max_calls),dict(state='known',reason='receipt_queue_absent',pending=False,calls_attempted=0)
    c=None;regular=None;regular_called=False;consumed=0
    try:
        now=chain.clock();c=_connect(path)
        schedule=c.execute('SELECT lane,clock_floor FROM receipt_schedule WHERE id=1').fetchone()
        if schedule is None or not _finite(now) or now<schedule['clock_floor']:raise ValueError('receipt_clock_unknown')
        job=c.execute("SELECT * FROM receipt_jobs WHERE state='pending' AND next_try<=? ORDER BY next_try,last_try,job_id LIMIT 1",(now,)).fetchone()
        pending=c.execute("SELECT 1 FROM receipt_jobs WHERE state='pending' LIMIT 1").fetchone() is not None
        c.close();c=None
        lane=schedule['lane'];regular=None;status=dict(state='partial' if pending else 'known',reason='receipt_retry_wait' if pending else 'receipt_queue_complete',pending=pending,calls_attempted=0)
        if job is not None and lane=='receipt':
            status,advanced=_run_one(chain,path,dict(job));consumed=status['calls_attempted']
            if advanced:lane='regular'
            remaining=max_calls-status['calls_attempted']
            if remaining>0:
                regular_called=True;regular=collector.tick(max_calls=remaining)
                if regular.get('calls_attempted',0)>0:lane='receipt'
        else:
            regular_called=True;regular=collector.tick(max_calls=max_calls)
            # Hold a paced regular lane until it makes progress; an empty lane yields.
            if not pending or regular.get('calls_attempted',0)>0 or regular.get('reason')=='polling_coverage_only':lane='receipt'
        c=_connect(path)
        with c:c.execute('UPDATE receipt_schedule SET lane=?,clock_floor=MAX(clock_floor,?) WHERE id=1',(lane,chain.clock()))
        return regular,status
    except (OSError,sqlite3.Error,ValueError) as exc:
        if not regular_called and max_calls>consumed:regular=collector.tick(max_calls=max_calls-consumed)
        return regular,dict(state='unknown',reason=str(exc) if isinstance(exc,ValueError) else 'receipt_store_unknown',pending=None,calls_attempted=consumed)
    finally:
        if c is not None:c.close()
