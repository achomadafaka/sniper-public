"""Non-consumed ExtendAccount audit sidecar. Original tape losses never change."""
import hashlib
import json
import math
from datetime import datetime,timedelta,timezone

from sniper.services import memecoin_desk_pumpswap as P
from sniper.services.memecoin_desk_ps_losses import SWAP_TX_ENVELOPE_PROFILE

MAX_RAW=256*1024
DAY_CAP=200
RULE='bottom-200-sha256-signature-utc-day-v1'
SCHEMAS={'ps_extend_shadow': 'CREATE TABLE IF NOT EXISTS ps_extend_shadow('
    'signature TEXT,cause TEXT,day TEXT,record_json TEXT,raw BLOB,raw_status TEXT,rank TEXT,cap_hit INTEGER,sample_selected INTEGER,'
    'PRIMARY KEY(signature,cause)) WITHOUT ROWID',
    'ps_extend_shadow_errors':'CREATE TABLE IF NOT EXISTS ps_extend_shadow_errors('
    'day TEXT,kind TEXT,errors INTEGER,first_at REAL,last_at REAL,PRIMARY KEY(day,kind)) WITHOUT ROWID'}
INDEXES={'ps_extend_shadow':(
    'CREATE INDEX IF NOT EXISTS ps_extend_shadow_day ON ps_extend_shadow(day)',
    'CREATE INDEX IF NOT EXISTS ps_extend_shadow_expiry ON ps_extend_shadow(day) WHERE raw IS NOT NULL',
    'CREATE INDEX IF NOT EXISTS ps_extend_shadow_raw_day ON ps_extend_shadow(day,rank) WHERE sample_selected=1',)}


def expire(c,day):
    """At most 8 raw blobs per processor heartbeat; no proof-row deletion, even while idle."""
    cutoff=(datetime.fromisoformat(day).date()-timedelta(days=2)).isoformat()
    return c.execute("UPDATE ps_extend_shadow SET raw=NULL,raw_status='sampled_out' "
                     "WHERE (signature,cause) IN (SELECT signature,cause FROM ps_extend_shadow "
                     "WHERE raw IS NOT NULL AND day<? LIMIT 8)",(cutoff,)).rowcount


def note_error(c,at,kind):
    if kind not in ('record','status','expiry'):raise ValueError('shadow_error_kind')
    day=datetime.fromtimestamp(at,timezone.utc).date().isoformat()
    c.execute('INSERT INTO ps_extend_shadow_errors VALUES(?,?,1,?,?) '
              'ON CONFLICT(day,kind) DO UPDATE SET errors=errors+1,last_at=excluded.last_at',
              (day,kind,at,at))


def record(c,raw,*,signature,cause,received_at,known_at,slot,keys,reason,original_class):
    if (not all(type(t)in(int,float)and math.isfinite(t)for t in(received_at,known_at))
            or not 0<=received_at<=known_at or type(slot)is not int or slot<=0
            or not isinstance(signature,str)or not signature or cause not in('decode_loss','virtual_quote_unproven')
            or original_class not in('unknown_loss_receipts','own_pool_losses')or not isinstance(raw,bytes)):
        raise ValueError('extend_shadow_identity')
    day=datetime.fromtimestamp(known_at,timezone.utc).date()
    expire(c,day.isoformat())
    if c.execute('SELECT 1 FROM ps_extend_shadow WHERE signature=? AND cause=?',(signature,cause)).fetchone():return 'duplicate'
    rank=hashlib.sha256(signature.encode()).hexdigest()
    body=dict(shadow=True,original_class=original_class,shadow_class='proven_unrelated_extendaccount',
              applies_only_when='target_absent_from_complete_keys',profile=SWAP_TX_ENVELOPE_PROFILE,
              envelope_version=P.EXTEND_SHADOW_VERSION,idl_sha256=P.PUMP_AMM_IDL_SHA256,
              discriminator='6161d7905d92167c',event_bytes=96,program=P.AMM_PROGRAM,
              signature=signature,cause=cause,received_at=received_at,known_at=known_at,slot=slot,
              reason=reason,keys=keys,rule=RULE,day=day.isoformat(),raw_keep_days=3,
              raw_sha256=hashlib.sha256(raw).hexdigest() if len(raw)<=MAX_RAW else None)
    body['envelope_id']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    c.execute('SAVEPOINT extend_shadow')
    try:
        retained=c.execute('SELECT signature,cause,rank FROM ps_extend_shadow WHERE day=? AND sample_selected=1 ORDER BY rank DESC LIMIT 200',(day.isoformat(),)).fetchall()
        hit=int(len(retained)>=DAY_CAP)
        state='oversize'if len(raw)>MAX_RAW else 'captured'
        selected=not hit or rank<retained[0][2]
        if hit and selected:
            c.execute("UPDATE ps_extend_shadow SET raw=NULL,sample_selected=0,raw_status=CASE WHEN raw_status='captured' THEN 'sampled_out' ELSE raw_status END WHERE signature=? AND cause=?",retained[0][:2])
        if state=='captured'and not selected:state='sampled_out'
        c.execute('INSERT INTO ps_extend_shadow VALUES(?,?,?,?,?,?,?,?,?)',
                  (signature,cause,day.isoformat(),json.dumps(body,sort_keys=True,allow_nan=False),raw if state=='captured'else None,state,rank,hit,int(selected)))
        c.execute('RELEASE extend_shadow')
        return state
    except BaseException:
        c.execute('ROLLBACK TO extend_shadow');c.execute('RELEASE extend_shadow')
        raise


def status(c,day):
    rows=c.execute('SELECT raw_status,count(*),sum(cap_hit) FROM ps_extend_shadow WHERE day=? GROUP BY raw_status',(day,)).fetchall()
    errors=dict(c.execute('SELECT kind,errors FROM ps_extend_shadow_errors WHERE day=?',(day,)))
    # Retained rows and error receipts are not proof that every source receipt was captured.
    return dict(day=day,rule=RULE,denominator_complete=False,denominator_basis='retained_rows_only',
                audit_errors=errors.get('record',0),audit_errors_basis='durably_recorded_lower_bound',audit_dependency_errors=sum(n for k,n in errors.items()if k!='record'),
                candidates=sum(n for _,n,_ in rows),
                cap_hits=sum(h for _,_,h in rows),**{s:n for s,n,_ in rows})
