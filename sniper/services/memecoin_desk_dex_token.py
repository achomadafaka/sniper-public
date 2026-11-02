"""Frozen cohort-D public Dex page claims; JUDGE reads a local capture only."""
import hashlib
import json
from pathlib import Path

_PATH = Path(__file__).with_name('desk_context_definitions') / 'dex_token.json'
_BYTES = _PATH.read_bytes()
DEFINITION = json.loads(_BYTES)
KNOWN_KEYS = frozenset(DEFINITION['known_keys'])
UNKNOWN_KEYS = frozenset(DEFINITION['unknown_keys'])
NOT_SERVED_KEYS = frozenset(DEFINITION['not_served_keys'])
REASONS = frozenset(DEFINITION['reasons'])
FIELD_REASONS = frozenset(DEFINITION['field_reasons'])


def definition():
    return {'version': DEFINITION['version'], 'definition_sha256': hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()}


# Dedicated desk store; no main-ledger handles or network reads in a sheet callback.
import fcntl
import math
import re
import sqlite3
import time
from datetime import datetime, timezone

_DDL = """
CREATE TABLE IF NOT EXISTS requests(mint TEXT NOT NULL,pool TEXT NOT NULL,requested_at REAL NOT NULL,
 PRIMARY KEY(mint,pool));
CREATE TABLE IF NOT EXISTS captures(id INTEGER PRIMARY KEY,mint TEXT NOT NULL,pool TEXT NOT NULL,
 host_received_at REAL NOT NULL,clock_offset_s REAL NOT NULL,as_of REAL NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dex_capture_lookup ON captures(mint,pool,as_of);
CREATE TABLE IF NOT EXISTS worker_state(id INTEGER PRIMARY KEY CHECK(id=1),last_request REAL NOT NULL,
 day TEXT NOT NULL,requests_n INTEGER NOT NULL,challenges INTEGER NOT NULL,cooldown_until REAL NOT NULL,disabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS challenges(as_of REAL NOT NULL,cooldown_until REAL NOT NULL,disabled INTEGER NOT NULL);
"""


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _identity(candidate):
    if not isinstance(candidate,dict) or candidate.get('network_id')!='solana':
        raise ValueError('identity')
    for field in ('mint','pool'):
        if not isinstance(candidate.get(field),str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',candidate[field]):
            raise ValueError('identity')
    return candidate['mint'],candidate['pool']


def _path(desk_dir):
    return Path(desk_dir).resolve()/'dex_token'/'claims.db'


def _open(desk_dir):
    path=_path(desk_dir);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=.05)
    db.execute('PRAGMA journal_mode=WAL');db.executescript(_DDL)
    return db


def _unknown(reason,as_of):
    return dict(state='unknown',reason=reason,definition_sha256=definition()['definition_sha256'],as_of=as_of)


def enqueue(candidate,*,desk_dir,checks,collected,as_of):
    """Cheap-pass only, local queue only. A busy/unavailable queue fails without holding intake."""
    from sniper.services.memecoin_desk_g12_intake import cheap_pass
    db=None
    try:
        if not _number(as_of) or not cheap_pass(checks,collected):
            return False
        mint,pool=_identity(candidate)
        db=_open(desk_dir)
        db.execute('INSERT INTO requests VALUES(?,?,?) ON CONFLICT(mint,pool) DO UPDATE SET requested_at=excluded.requested_at',
                   (mint,pool,as_of));db.commit();return True
    except Exception:
        return False
    finally:
        if db is not None: db.close()


def _claims(capture,mint,pool):
    if not isinstance(capture,dict):
        return {'state':'unknown','reason':'read_error'}
    if capture.get('reason')=='challenged':
        return {'state':'unknown','reason':'challenged'}
    if capture.get('state')=='not_served':
        return {'state':'not_served','reason':None}
    if capture.get('state')!='known':
        reason=capture.get('reason')
        return {'state':'unknown','reason':reason if reason in {'read_error','not_served_unparsed'} else 'read_error'}
    if capture.get('mint')!=mint or capture.get('pool')!=pool:
        return {'state':'not_served','reason':None}
    for field in ('vol_24h','buys_24h','sells_24h'):
        value=capture.get(field)
        if not _number(value) or value<0 or (field!='vol_24h' and type(value) is not int):
            return {'state':'unknown','reason':'not_served_unparsed'}
    age=capture.get('pair_age_s')
    if age is not None and (not _number(age) or age<0):
        return {'state':'unknown','reason':'not_served_unparsed'}
    return dict(state='known',reason=None,label='provider_claim',field_unknown={'pair_age_s':'not_reported'} if age is None else {},
                **{k:capture[k] for k in ('vol_24h','buys_24h','sells_24h')},pair_age_s=age)


def dex_token_for(candidate,*,desk_dir,as_of):
    """Bounded causal local read; no connection creation on a missing store."""
    db=None;deadline=time.monotonic()+DEFINITION['read_budget_s']
    try:
        if not _number(as_of): return _unknown('clock_unknown',None)
        mint,pool=_identity(candidate)
        path=_path(desk_dir)
        if not path.exists(): return _unknown('no_capture',as_of)
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
        db.set_progress_handler(lambda:time.monotonic()>=deadline,1000);db.execute('BEGIN')
        challenge=db.execute('SELECT cooldown_until,disabled FROM challenges WHERE as_of<=? ORDER BY as_of DESC LIMIT 1',(as_of,)).fetchone()
        if challenge and (challenge[1] or as_of<challenge[0]): return _unknown('challenged',as_of)
        row=db.execute('SELECT as_of,payload FROM captures WHERE mint=? AND pool=? AND as_of<=? ORDER BY as_of DESC,id DESC LIMIT 1',
                       (mint,pool,as_of)).fetchone()
        if row is None: return _unknown('no_capture',as_of)
        at,raw=row
        if as_of-at>DEFINITION['freshness_s']: return _unknown('stale',at)
        payload=json.loads(raw)
        if not isinstance(payload,dict): return _unknown('read_error',at)
        state=payload.get('state')
        expected=KNOWN_KEYS if state=='known' else NOT_SERVED_KEYS if state=='not_served' else UNKNOWN_KEYS
        if set(payload)!=expected or payload.get('definition_sha256')!=definition()['definition_sha256'] or payload.get('as_of')!=at:
            return _unknown('read_error',at)
        if state=='known':
            normalized=_claims({**payload,'mint':mint,'pool':pool},mint,pool)
            if normalized.get('state')!='known' or any(normalized[k]!=payload[k] for k in normalized):
                return _unknown('read_error',at)
        elif state=='unknown' and payload.get('reason') not in REASONS:
            return _unknown('read_error',at)
        elif state=='not_served' and payload.get('reason') is not None:
            return _unknown('read_error',at)
        return payload
    except Exception:
        return _unknown('read_error',as_of if _number(as_of) else None)
    finally:
        if db is not None: db.close()


class Worker:
    """One process owns the queue, durable budget and global challenge circuit."""
    def __init__(self,desk_dir):
        self.desk=Path(desk_dir);path=_path(desk_dir);path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=(path.parent/'worker.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.db=_open(desk_dir)
        except Exception:
            self.lock.close();raise

    def __enter__(self): return self
    def __exit__(self,*args): self.db.close();self.lock.close()

    def step(self,*,now,offset_s,fetch):
        if not _number(now) or not _number(offset_s): return 'clock_unknown'
        at=now-offset_s
        self.db.execute('DELETE FROM requests WHERE requested_at<?',(at-DEFINITION['freshness_s'],));self.db.commit()
        row=self.db.execute('SELECT r.mint,r.pool,r.requested_at FROM requests r WHERE r.requested_at<=? '
            'AND NOT EXISTS(SELECT 1 FROM captures c WHERE c.mint=r.mint AND c.as_of>?) '
            'ORDER BY r.requested_at LIMIT 1',(at,at-DEFINITION['min_mint_interval_s'])).fetchone()
        if row is None:
            return 'paced' if self.db.execute('SELECT 1 FROM requests LIMIT 1').fetchone() else 'idle'
        mint,pool,requested=row
        if not 0<=at-requested<=DEFINITION['freshness_s']:
            self.db.execute('DELETE FROM requests WHERE mint=? AND pool=?',(mint,pool));self.db.commit();return 'expired'
        state=self.db.execute('SELECT last_request,day,requests_n,challenges,cooldown_until,disabled FROM worker_state WHERE id=1').fetchone()
        day=datetime.fromtimestamp(at,timezone.utc).date().isoformat()
        last,old_day,n,challenges,cooldown,disabled=state or (-1.,day,0,0,0.,0)
        if disabled or at<cooldown: return 'challenged'
        if old_day!=day: n=0
        if n>=DEFINITION['daily_request_cap']: return 'daily_cap'
        if at-last<DEFINITION['min_global_interval_s']:
            return 'paced'
        # Reserve before browser launch: a crash consumes budget and never triggers an immediate retry.
        self.db.execute('INSERT OR REPLACE INTO worker_state VALUES(1,?,?,?,?,?,?)',(at,day,n+1,challenges,cooldown,disabled))
        self.db.execute('DELETE FROM requests WHERE mint=? AND pool=?',(mint,pool))
        reserved=_unknown('read_error',at)
        self.db.execute('INSERT INTO captures(mint,pool,host_received_at,clock_offset_s,as_of,payload) VALUES(?,?,?,?,?,?)',
                        (mint,pool,now,offset_s,at,json.dumps(reserved)));self.db.commit()
        receipt=None
        try:
            raw=fetch(dict(network_id='solana',mint=mint,pool=pool))
            if isinstance(raw,tuple): raw,receipt=raw
            payload=_claims(raw,mint,pool)
        except Exception: payload={'state':'unknown','reason':'read_error'}
        # The transport returns the host receipt alongside its parsed page.

        captured=receipt-offset_s if _number(receipt) else at
        if payload.get('reason')=='challenged':
            challenges+=1;cooldown=captured+DEFINITION['challenge_cooldown_s'];disabled=int(challenges>=DEFINITION['consecutive_challenges_disable'])
        elif payload.get('state') in ('known','not_served'):
            challenges=0;cooldown=0
        payload.update(definition_sha256=definition()['definition_sha256'],as_of=captured)
        self.db.execute('INSERT INTO captures(mint,pool,host_received_at,clock_offset_s,as_of,payload) VALUES(?,?,?,?,?,?)',
                        (mint,pool,captured+offset_s,offset_s,captured,json.dumps(payload,allow_nan=False)))
        self.db.execute('UPDATE worker_state SET challenges=?,cooldown_until=?,disabled=? WHERE id=1',(challenges,cooldown,disabled))
        self.db.execute('INSERT INTO challenges VALUES(?,?,?)',(captured,cooldown,disabled))
        self.db.commit();return 'captured'


def page_claims(candidate,*,url,text,mints,period_24h):
    """Only rendered, explicitly 24-hour labels; unfamiliar markup stays unknown."""
    from urllib.parse import urlsplit
    from sniper.services.memecoin_desk_list_claims import numeric
    mint,pool=_identity(candidate)
    if re.search(r'verify you are human|captcha|access denied|just a moment|cloudflare|request blocked',text,re.I):
        return {'state':'unknown','reason':'challenged'}
    parsed=urlsplit(url)
    if parsed.hostname!='dexscreener.com' or parsed.path.rstrip('/')!='/solana/'+pool or mint not in mints:
        return {'state':'not_served','reason':None}
    if not period_24h:
        return {'state':'unknown','reason':'not_served_unparsed'}
    lines=[s.strip() for s in text.splitlines() if s.strip()]
    claims={}
    for label,field,count in [('Volume','vol_24h',False),('Buys','buys_24h',True),('Sells','sells_24h',True)]:
        found=[lines[i+1] for i,s in enumerate(lines[:-1]) if s.casefold()==label.casefold()]
        if len(found)!=1:
            return {'state':'unknown','reason':'not_served_unparsed'}
        try: claims[field]=numeric(found[0],count=count)
        except ValueError: return {'state':'unknown','reason':'not_served_unparsed'}
    age=None
    found=[lines[i+1] for i,s in enumerate(lines[:-1]) if s.casefold()=='pair created']
    if len(found)==1:
        match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*([smhd])(?:\s+ago)?',found[0],re.I)
        if match: age=float(match[1])*{'s':1,'m':60,'h':3600,'d':86400}[match[2].lower()]
    return dict(state='known',reason=None,mint=mint,pool=pool,pair_age_s=age,**claims)
