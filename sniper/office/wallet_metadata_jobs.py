"""Finite research receipt recovery; only the capture producer writes tape timestamps."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import base58
from .chain_data import FINAL_TTL, PACE, SIGNATURE, _Refused
from . import wallet_receipt_jobs as R

SCHEMA='''
CREATE TABLE IF NOT EXISTS metadata_schedule(id INTEGER PRIMARY KEY CHECK(id=1),source TEXT NOT NULL,identity TEXT NOT NULL,lane TEXT NOT NULL,clock_floor REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metadata_jobs(slot INTEGER PRIMARY KEY,signature TEXT NOT NULL,requested_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',reason TEXT,next_try REAL NOT NULL DEFAULT 0,fetched_at REAL,known_at REAL,cache_key TEXT);
'''

def _finite(x):return type(x) in (int,float) and math.isfinite(x) and x>0

def _identity(path):
    s=Path(path).stat();return str(s.st_dev)+':'+str(s.st_ino)

def _params(signature):return [signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')]

def _key(signature):return hashlib.sha256(json.dumps(['getTransaction',_params(signature)],sort_keys=True).encode()).hexdigest()

def _connect(path,mode='rw'):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode='+mode,uri=True,timeout=0);c.row_factory=sqlite3.Row;return c

def _time(tx,signature,slot,fetched,known):
    if (not isinstance(signature,str) or not SIGNATURE.fullmatch(signature) or len(base58.b58decode(signature))!=64 or not any(base58.b58decode(signature))
            or not _finite(fetched) or not _finite(known) or fetched>known or not isinstance(tx,dict)
            or type(tx.get('slot')) is not int or tx['slot']!=slot
            or tx.get('transaction',{}).get('signatures',[None])[0]!=signature
            or type(tx.get('blockTime')) is not int or not 0<tx['blockTime']<=fetched):raise ValueError('metadata_receipt_unproven')
    return tx['blockTime']

def seed(path,tape,*,slots,now):
    """Explicit finite canary: once created, this queue never enrolls replacement slots."""
    if type(slots) is not int or not 1<=slots<=10 or not _finite(now):raise ValueError('bad_metadata_canary')
    path=Path(path);tape=Path(tape).resolve()
    if path.is_file():return
    identity=_identity(tape);rows=[];source=_connect(tape,'ro')
    try:
        slot=-1
        for _ in range(slots):
            row=source.execute('SELECT slot,signature,received_at FROM wallet_flow_transactions INDEXED BY wallet_flow_pending_slot WHERE block_time IS NULL AND slot>? ORDER BY slot LIMIT 1',(slot,)).fetchone()
            if row is None:break
            slot=row['slot']
            if (type(slot) is not int or slot<=0 or not isinstance(row['signature'],str) or not SIGNATURE.fullmatch(row['signature'])
                    or len(base58.b58decode(row['signature']))!=64 or not any(base58.b58decode(row['signature']))):raise ValueError('metadata_target_unproven')
            if _finite(row['received_at']) and row['received_at']<=now-60:rows.append((slot,row['signature'],now))
    finally:source.close()
    if _identity(tape)!=identity:raise ValueError('metadata_source_replaced')
    path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path,timeout=0)
    try:
        c.executescript(SCHEMA)
        with c:
            c.execute("INSERT INTO metadata_schedule VALUES(1,?,?,'metadata',?)",(str(tape),identity,now))
            c.executemany('INSERT INTO metadata_jobs(slot,signature,requested_at) VALUES(?,?,?)',rows)
    finally:c.close()

def read_times(path,chain_path,tape,*,slots,now):
    """Read-only independent validation of full finalized cache receipts, not job assertions."""
    if not Path(path).is_file():return dict(state='known',reason='metadata_queue_absent',items={})
    c=cache=None;items={}
    try:
        c=_connect(path,'ro');cache=_connect(chain_path,'ro')
        schedule=c.execute('SELECT * FROM metadata_schedule WHERE id=1').fetchone()
        if (schedule is None or schedule['source']!=str(Path(tape).resolve()) or schedule['identity']!=_identity(tape)
                or not _finite(now) or not _finite(schedule['clock_floor']) or now<schedule['clock_floor']):raise ValueError('metadata_source_or_clock_unknown')
        for slot in set(slots):
            job=c.execute("SELECT * FROM metadata_jobs WHERE slot=? AND state='known'",(slot,)).fetchone()
            if job is None:continue
            if (job['cache_key']!=_key(job['signature']) or not _finite(job['requested_at']) or not _finite(job['known_at'])
                    or not job['requested_at']<=job['known_at']<=now):raise ValueError('metadata_job_unproven')
            hit=cache.execute('SELECT at,expires,doc FROM cache WHERE key=?',(job['cache_key'],)).fetchone()
            if hit is None or hit['at']!=job['fetched_at'] or not _finite(hit['expires']) or hit['expires']<=now:raise ValueError('metadata_cache_unknown')
            if not isinstance(hit['doc'],str) or len(hit['doc'])>2*1024*1024:raise ValueError('metadata_cache_bound')
            items[slot]=_time(json.loads(hit['doc']),job['signature'],slot,hit['at'],job['known_at'])
        return dict(state='known',reason='metadata_cache_checked',items=items)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,AttributeError):
        return dict(state='unknown',reason='metadata_cache_unproven',items={})
    finally:
        if c is not None:c.close()
        if cache is not None:cache.close()

def _run_one(chain,path,job):
    now=chain.clock();known=now;reason=None;fetched=None;key=None;advance=True;cached=False;attempts=None;retry=now+PACE
    try:
        tx,cached,fetched=chain.call('getTransaction',_params(job['signature']),ttl=FINAL_TTL)
        attempts=int(not cached);known=chain.clock()
        if not _finite(known) or known<now or now<job['requested_at']:raise ValueError('metadata_clock_unknown')
        _time(tx,job['signature'],job['slot'],fetched,known);key=_key(job['signature'])
    except _Refused as exc:
        reason=exc.reason;attempts=int(exc.dialed);advance=exc.dialed or reason!='paced'
        if reason=='paced':retry=now
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,AttributeError):
        reason='metadata_receipt_unproven'
        if _finite(fetched):retry=max(retry,fetched+FINAL_TTL)
    if not _finite(known) or known<now:known=now;reason='metadata_clock_unknown';key=None
    c=_connect(path)
    try:
        with c:
            n=c.execute("UPDATE metadata_jobs SET state=?,reason=?,next_try=?,fetched_at=?,known_at=?,cache_key=? WHERE slot=? AND signature=? AND state='pending'",
                        ('known' if key and reason is None else 'pending',reason,retry,fetched,known if key and reason is None else None,key,job['slot'],job['signature'])).rowcount
            if n!=1:raise sqlite3.OperationalError('metadata_job_changed')
    finally:c.close()
    return dict(state='partial',reason=reason or 'metadata_slot_recovered',slot=job['slot'],calls_attempted=1,provider_attempts=attempts,cache_hits=int(cached)),advance

def poll(chain,collector,receipt_path,path,tape,*,slots,max_calls):
    """One existing remote owner; finite metadata alternates with existing receipt/regular lanes."""
    if type(max_calls) is not int or not 1<=max_calls<=100 or type(slots) is not int or not 0<=slots<=10:raise ValueError('bad_metadata_budget')
    if slots==0:
        regular,receipts=R.poll(chain,collector,receipt_path,max_calls=max_calls)
        return regular,receipts,dict(state='known',reason='metadata_recovery_disabled',calls_attempted=0)
    c=None;consumed=0;other_called=False;regular=receipts=None
    try:
        now=chain.clock();seed(path,tape,slots=slots,now=now);c=_connect(path)
        schedule=c.execute('SELECT * FROM metadata_schedule WHERE id=1').fetchone()
        if schedule is None or schedule['source']!=str(Path(tape).resolve()) or schedule['identity']!=_identity(tape):raise ValueError('metadata_source_replaced')
        if not _finite(now) or now<schedule['clock_floor']:raise ValueError('metadata_clock_unknown')
        job=c.execute("SELECT * FROM metadata_jobs WHERE state='pending' AND next_try<=? ORDER BY next_try,slot LIMIT 1",(now,)).fetchone()
        pending=c.execute("SELECT 1 FROM metadata_jobs WHERE state='pending' LIMIT 1").fetchone() is not None
        lane=schedule['lane'];c.close();c=None
        status=dict(state='partial' if pending else 'known',reason='metadata_retry_wait' if pending else 'metadata_complete',calls_attempted=0)
        if job is not None and lane=='metadata':
            consumed=1  # A post-admission store failure must never spend this slot twice.
            status,advanced=_run_one(chain,path,dict(job))
            if advanced:lane='regular'
            if max_calls>consumed:
                other_called=True;regular,receipts=R.poll(chain,collector,receipt_path,max_calls=max_calls-consumed)
        else:
            other_called=True;regular,receipts=R.poll(chain,collector,receipt_path,max_calls=max_calls)
            if (regular or {}).get('calls_attempted',0) or (receipts or {}).get('calls_attempted',0) or (regular or {}).get('reason')=='polling_coverage_only':lane='metadata'
        c=_connect(path)
        with c:c.execute('UPDATE metadata_schedule SET lane=?,clock_floor=MAX(clock_floor,?) WHERE id=1',(lane,chain.clock()))
        return regular,receipts,status
    except (OSError,sqlite3.Error,ValueError,TypeError):
        if not other_called and max_calls>consumed:regular,receipts=R.poll(chain,collector,receipt_path,max_calls=max_calls-consumed)
        return regular,receipts,dict(state='unknown',reason='metadata_store_or_clock_unknown',calls_attempted=consumed)
    finally:
        if c is not None:c.close()
