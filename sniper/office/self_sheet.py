"""Host-only ticketless inspection. Missing safety is unknown, never a fabricated feed PASS."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

LIMIT=200
TOOL_NAMES=('self_sheet_read',)
LENSES=('ps_trending',)


def _number(value):
    try:return type(value) in (int,float) and math.isfinite(value) and value>=0
    except OverflowError:return False


def _stamp(value):
    try:
        at=datetime.fromisoformat(value)
        return at.timestamp() if at.tzinfo is not None and _number(at.timestamp()) else None
    except (TypeError,ValueError,OverflowError,OSError):return None


def feed_lookup(path,network_id,mint,*,cut_host):
    """Complete indexed coin lookup, or typed unknown. A future ticket cannot label an earlier decision feed."""
    if not _number(cut_host):return dict(state='unknown',reason='clock_unknown')
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as conn:
            deadline=time.monotonic()+.1
            conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            conn.execute('BEGIN')
            rows=conn.execute("SELECT ticket_id,ts FROM desk_tickets INDEXED BY idx_desk_tickets_coin WHERE network_id=? AND mint=? ORDER BY ts LIMIT ?",(network_id,mint,LIMIT+1)).fetchall()
            valid=[(ticket,at) for ticket,value in rows[:LIMIT] if (at:=_stamp(value)) is not None]
            known=[(ticket,at) for ticket,at in valid if at<=cut_host]
            if known:
                ticket,at=min(known,key=lambda r:r[1])
                return dict(state='known',source='feed',ticket_id=ticket,known_at_host=at)
            if len(rows)>LIMIT:return dict(state='unknown',reason='feed_lookup_limited')
            if len(valid)!=len(rows):return dict(state='unknown',reason='feed_clock_unknown')
            if not rows and conn.execute('SELECT 1 FROM desk_ticket_heads WHERE network_id=? AND mint=? LIMIT 1',(network_id,mint)).fetchone():
                return dict(state='unknown',reason='feed_stage_unknown')
            return dict(state='known',source='self',known_at_host=None,ticket_id=None)
    except (OSError,sqlite3.Error,ValueError):return dict(state='unknown',reason='feed_lookup_unknown')


def build(*,network_id,mint,pool,now,registration):
    """Readable missing-evidence report. No provider, safety verdict, quote or receipt is invented."""
    # shortcut: no independent mint/sell evidence collector exists; add one only with its own host proof review.
    safety=dict(authority_mint=None,authority_freeze=None,top_holder_share_ex_pool=None,sell_probe=None,sells_exist_60s=None)
    body=dict(network_id=network_id,mint=mint,pool=pool,as_of=now,registration=registration,
              safety=safety,hard_filter=dict(state='unknown',reason='host_evidence_not_collected'),
              missing_required=['mint_authority','freeze_authority','top_holders_ex_pool','sells_exist','sell_probe','pool_state'],sheet_lenses_missing=list(LENSES))
    return dict(kind='self_sheet',state='unknown',reason='self_host_evidence_unknown',sheet=body,lenses={})


def definition_hash():
    """Bind the builder/admission implementation AND the read tool schema, before effect."""
    from . import seat_tools as T
    files=[Path(__file__),Path(__file__).with_name('v2_paper.py'),Path(__file__).with_name('v2_orders.py')]
    schema=json.dumps({name:T.TOOLS[name] for name in TOOL_NAMES},sort_keys=True).encode()
    return hashlib.sha256(b'\0'.join([*(p.read_bytes() for p in files),schema])).hexdigest()


def logged(conn,epoch):
    from . import v2_explore as X
    try:X.require_logged(conn,epoch_id=epoch,kind='ticketless_sheets',content_hash=definition_hash());return True
    except (OSError,ValueError,sqlite3.Error,KeyError):return False


def registration_key(epoch,network,mint):
    return hashlib.sha256(json.dumps(['candidate_registration_clock/v1',epoch,network,mint],separators=(',',':')).encode()).hexdigest()


def clock_binding(*,epoch,network,mint,pool,actor,host,sample):
    from sniper.services.memecoin_desk_contract import CLOCK_MAX_AGE_S
    if not isinstance(sample,dict) or not _number(host):return None
    offset,at=sample.get('offset_s'),sample.get('sampled_at_host')
    if type(offset) not in (int,float) or not _number(abs(offset)) or not _number(at) or not 0<=host-at<=CLOCK_MAX_AGE_S or not _number(host-offset):return None
    return dict(kind='candidate_registration_clock/v1',cache_key=registration_key(epoch,network,mint),epoch_id=epoch,network_id=network,mint=mint,pool=pool,
                registered_by=actor,known_at_host=host,corrected_known_at=host-offset,clock_sample=dict(offset_s=offset,sampled_at_host=at))


def registration_time(conn,epoch,network,mint,row):
    """No later clock sample can retroactively establish an old registration's corrected time."""
    from . import store as S
    try:hit=S.latest(conn,'receipts','cache_key',registration_key(epoch,network,mint))
    except sqlite3.Error:return dict(state='unknown',reason='registration_clock_unavailable')
    if hit is None:return dict(state='unknown',reason='registration_clock_unbound')
    try:
        body=json.loads(hit['payload_json'])
        if not isinstance(body,dict):raise ValueError('binding_shape')
        expected=clock_binding(epoch=epoch,network=network,mint=mint,pool=row[2],actor=row[1],host=row[0],sample=body.get('clock_sample'))
        if expected is None or body!=expected:raise ValueError('binding_invalid')
        return dict(state='known',known_at=body['corrected_known_at'],actor_id=row[1],clock_receipt=hit['row_hash'])
    except (ValueError,TypeError,KeyError):return dict(state='unknown',reason='registration_clock_unvalidated',clock_receipt=hit['row_hash'])
