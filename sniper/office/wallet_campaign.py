"""Bounded, persisted campaign observations. Timing and payments never prove control."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from google.protobuf.message import DecodeError

from .chain_data import SIGNATURE
from .wallet_network import ADDRESS
from .wallet_family import PROGRAMS
from .wallet_watch import _known_infrastructure
from . import wallet_watch as W

VERSION=1
LAUNCH_VERSION='wallet-campaign-launch-v1'
RETROSPECTIVE_LAUNCH_VERSION='wallet-campaign-retrospective-launch-v1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS wallet_campaigns(version INTEGER NOT NULL,campaign_id TEXT PRIMARY KEY,seed TEXT NOT NULL,bucket_start REAL NOT NULL,bucket_end REAL NOT NULL,first_recorded_at REAL,preparation_json TEXT NOT NULL,prior_pattern_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS campaign_seed_time ON wallet_campaigns(seed,bucket_start,first_recorded_at);
CREATE INDEX IF NOT EXISTS campaign_recorded ON wallet_campaigns(first_recorded_at);
CREATE TABLE IF NOT EXISTS wallet_campaign_preparations(source_key TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,member TEXT NOT NULL,kind TEXT NOT NULL,event_at REAL NOT NULL,known_at REAL NOT NULL,proof_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS campaign_preparations_time ON wallet_campaign_preparations(campaign_id,known_at);
CREATE INDEX IF NOT EXISTS campaign_preparations_member ON wallet_campaign_preparations(campaign_id,member);
CREATE TABLE IF NOT EXISTS wallet_campaign_versions(campaign_id TEXT NOT NULL,revision INTEGER NOT NULL,recorded_at REAL,source_as_of REAL NOT NULL,doc_json TEXT NOT NULL,PRIMARY KEY(campaign_id,revision));
CREATE TABLE IF NOT EXISTS wallet_campaign_launches(version TEXT NOT NULL,launch_id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,mint TEXT NOT NULL,source_signature TEXT NOT NULL,source_receipt REAL,known_at REAL NOT NULL,emitted_at REAL,curve TEXT,coverage_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS campaign_launches ON wallet_campaign_launches(campaign_id);
CREATE INDEX IF NOT EXISTS campaign_launch_pending ON wallet_campaign_launches(emitted_at);
CREATE INDEX IF NOT EXISTS campaign_version_pending ON wallet_campaign_versions(recorded_at);
CREATE TABLE IF NOT EXISTS wallet_campaign_cursor(source TEXT PRIMARY KEY,cursor INTEGER NOT NULL,last_read REAL NOT NULL,identity TEXT,pending_json TEXT NOT NULL);
'''


class _Stop(Exception):pass


def _finite(value):return type(value) in (int,float) and math.isfinite(value) and value>0

def _address(value):return isinstance(value,str) and ADDRESS.fullmatch(value) is not None

def _json(value):return json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))

def _id(*parts):return hashlib.sha256(_json([VERSION,*parts]).encode()).hexdigest()


def _connect(path,deadline,*,write=False):
    c=sqlite3.connect(Path(path).resolve().as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=0)
    c.row_factory=sqlite3.Row
    c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
    return c


def _write(net,c,now,sql,args,*,rows=1):
    reason=net._refusal(c,now,rows=rows)
    if reason:raise _Stop(reason)
    c.execute(sql,args)
    if rows:net._count(c,now,rows=rows)


def _strict(edge):
    return (edge['source']=='host' and edge['proof_state']=='verified' and
        (edge['proof_kind'],edge['method_version']) in {('chain_fetch','wallet-watch-receipt-v1'),('research_stream','wallet-watch-stream-v1')})


def _preparation(net,c,now,seed,member,event_at,known_at,kind,key,proof):
    if c.execute('SELECT 1 FROM wallet_campaign_preparations WHERE source_key=?',(key,)).fetchone():return None
    bucket=math.floor(event_at/60)*60;cid=_id('campaign',seed,bucket)
    if not c.execute('SELECT 1 FROM wallet_campaigns WHERE campaign_id=?',(cid,)).fetchone():
        prior=c.execute('SELECT count(*) FROM wallet_campaigns WHERE seed=? AND bucket_start<? AND first_recorded_at<?',(seed,bucket,now)).fetchone()[0]
        _write(net,c,now,'INSERT INTO wallet_campaigns VALUES(?,?,?,?,?,NULL,?,?)',
            (VERSION,cid,seed,bucket,bucket+60,_json(proof),_json(dict(preparations=prior,as_of=now,kind='same_seed_prior_windows',ownership_proven=False))))
    _write(net,c,now,'INSERT INTO wallet_campaign_preparations VALUES(?,?,?,?,?,?,?)',(key,cid,member,kind,event_at,known_at,_json(proof)))
    return cid


def _source(net,c,tape,now,quota,name,identity,known_infrastructure,deadline):
    old=c.execute('SELECT * FROM wallet_campaign_cursor WHERE source=?',(name,)).fetchone()
    if old and old['identity']!=identity:raise _Stop('campaign_source_replaced')
    cursor=old['cursor'] if old else 0;pending=json.loads(old['pending_json']) if old else []
    db=c if name=='edges' else tape
    if db is None:return set(),dict(state='unknown',reason='setup_tape_unknown')
    table,pk=('edges','edge_id') if name=='edges' else ('wallet_setup_events','setup_id')
    try:
        maximum=db.execute(f'SELECT {pk} FROM {table} ORDER BY {pk} DESC LIMIT 1').fetchone()
        if maximum and maximum[0]<cursor:raise _Stop('campaign_source_replaced')
        # Pending clocks get a bounded share; a future event never silently advances out of evidence.
        retried=pending[:max(1,quota//2)];remaining=quota-len(retried)
        if name=='edges':
            new=db.execute("SELECT * FROM edges INDEXED BY edges_overview WHERE network_id='solana' AND (source='import')=0 AND source='host' AND edge_id>? ORDER BY edge_id LIMIT ?",(cursor,remaining+1)).fetchall()
        else:new=db.execute(f'SELECT * FROM {table} WHERE {pk}>? ORDER BY {pk} LIMIT ?',(cursor,remaining+1)).fetchall()
        backlog=len(new)>remaining;new=new[:remaining]
        rows=[db.execute(f'SELECT * FROM {table} WHERE {pk}=?',(key,)).fetchone() for key in retried]+new
    except sqlite3.Error:return set(),dict(state='unknown',reason='campaign_source_unknown')
    waiting=pending[len(retried):];touched=set();scanned=0
    for row in rows:
        if time.monotonic()>deadline:raise _Stop('campaign_read_budget')
        if row is None:continue
        key=row[pk];scanned+=1
        if name=='edges':
            if (row['type']=='created_mint' and row['source']=='host' and row['proof_state']=='verified'
                    and _finite(row['known_at']) and row['known_at']<=now):
                touched.update(r[0] for r in c.execute('SELECT DISTINCT campaign_id FROM wallet_campaign_preparations WHERE member=? UNION SELECT campaign_id FROM wallet_campaigns WHERE seed=? LIMIT 101',(row['src'],row['src'])))
            valid=(row['type']=='funded_by' and _strict(row) and _address(row['src']) and _address(row['dst']) and row['src']!=row['dst'] and isinstance(row['proof_ref'],str) and 0<len(row['proof_ref'])<=128)
            seed,member,event_at,known_at,kind=row['dst'],row['src'],row['observed_at'],row['known_at'],'funding'
            proof=dict(source_key='edge:'+str(key),seed=seed,member=member,kind=kind,event_at=event_at,known_at=known_at,proof_ref=row['proof_ref'],method=row['method_version'],availability='source_clock_only')
        else:
            valid=(row['program'] in PROGRAMS and _address(row['payer']) and _address(row['wallet']) and row['payer']!=row['wallet']
                and isinstance(row['signature'],str) and SIGNATURE.fullmatch(row['signature']) and type(row['outer_ix']) is int and 0<=row['outer_ix']<128 and type(row['slot']) is int and row['slot']>0
                and _finite(row['received_at']) and _finite(row['processed_at']) and row['received_at']<=row['processed_at'])
            seed,member,event_at,known_at,kind=row['payer'],row['wallet'],row['received_at'],row['processed_at'],'setup'
            proof=dict(source_key='setup:'+row['signature']+':'+str(row['outer_ix']),seed=seed,member=member,kind=kind,event_at=event_at,known_at=known_at,signature=row['signature'],outer_ix=row['outer_ix'],program=row['program'],availability='source_clock_only')
        defer=valid and (_finite(known_at) and known_at>now or _finite(event_at) and event_at>now or known_infrastructure is None)
        if defer:
            if len(waiting)>=100:raise _Stop('campaign_pending_cap')
            waiting.append(key)
        elif valid and _finite(event_at) and _finite(known_at) and seed not in known_infrastructure and member not in known_infrastructure:
            cid=_preparation(net,c,now,seed,member,event_at,known_at,kind,proof['source_key'],proof)
            if cid:touched.add(cid)
        cursor=max(cursor,key)
    if scanned or (old and waiting!=pending):
        _write(net,c,now,'INSERT INTO wallet_campaign_cursor VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read,pending_json=excluded.pending_json',
            (name,cursor,now,identity,_json(sorted(set(waiting)))),rows=0 if old else 1)
    return touched,dict(state='partial' if waiting or backlog else 'known',pending=len(waiting),has_more=backlog,cursor=cursor,scanned=scanned)


def _buys(tape,c,cid,start,now):
    if tape is None:return [],dict(state='unknown',reason='buy_tape_unknown')
    members={r[0] for r in c.execute('SELECT member FROM wallet_campaign_preparations WHERE campaign_id=? LIMIT 101',(cid,))}
    try:
        rows=tape.execute('SELECT * FROM ps_watch_events WHERE received_at>=? AND received_at<=? ORDER BY received_at,signature,ordinal LIMIT 101',(start,min(now,start+86400))).fetchall()
    except sqlite3.Error:return [],dict(state='unknown',reason='buy_tape_unknown')
    buys=[]
    for r in rows[:100]:
        if (r['actor'] in members and r['is_buy']==1 and _address(r['mint']) and _finite(r['received_at'])
                and isinstance(r['signature'],str) and 64<=len(r['signature'])<=88):
            buys.append(dict(signature=r['signature'],mint=r['mint'],actor=r['actor'],received_at=r['received_at'],slot=r['slot'],tx_index=r['tx_index'],ordinal=r['ordinal']))
    return buys,dict(state='partial',truncated=len(rows)>100 or len(members)>100,atomic_buys=[dict(signature=r['signature'],mint=r['mint']) for r in rows[:100] if r['is_buy']==1 and _address(r['mint'])],note='Observed watched BUY rows only; first observed is not proven first buy.')


def _seed_buy_from_receipt(tx,mint):
    # Positive decoded Pump events only. Missing/unsupported events never prove no seed buy.
    from sniper.services.memecoin_desk_universe import pump_event_lines,b58decode,PUMP_PROGRAM
    from sniper.research.memecoin_opportunity.pumpfun import decode_pumpfun_event
    from sniper.research.memecoin_opportunity.types import EventKind
    keys=[r['pubkey'] if isinstance(r,dict) else r for r in tx['transaction']['message']['accountKeys']]
    inner=[]
    for group in tx['meta'].get('innerInstructions') or []:
        instructions=[]
        for ix in group['instructions']:
            if 'data' in ix and ix.get('programId') in keys:instructions.append((keys.index(ix['programId']),b58decode(ix['data'])))
        inner.append((group['index'],instructions))
    for _,line in pump_event_lines(account_keys=[b58decode(k) for k in keys],inner=inner,logs=tx['meta'].get('logMessages') or []):
        event=decode_pumpfun_event(PUMP_PROGRAM,line)
        if event.event_kind==EventKind.BUY and event.token_id==mint:return 'present'
    return 'unknown'


def _launch_fact(edge,chain,tape,c,now,source):
    if chain is None:return None
    record=chain.execute('SELECT result,shown_at,tool,state FROM fetches WHERE fetch_id=?',(edge['proof_ref'],)).fetchone()
    if not record or record['state']!='known' or len(record[0])>8192 or not _finite(record[1]) or record[1]>now:return None
    fact=json.loads(record[0]);asked=fact.get('asked') or {}
    retrospective=(edge['proof_kind']=='chain_fetch' and edge['method_version']=='confirmed_pump_create_v1')
    profile=('watch_verified_creation','research_rpc','finalized') if retrospective else ('watch_stream_launch','research_stream','confirmed')
    if not (record['tool']==profile[0] and fact.get('state')=='known' and fact.get('provenance')==profile[1] and fact.get('commitment')==profile[2]
            and fact.get('tool')==profile[0] and fact.get('method')=='confirmed_pump_create_v1'
            and fact.get('mint')==edge['dst'] and fact.get('developer')==edge['src'] and _address(fact.get('curve'))
            and isinstance(fact.get('signature'),str) and SIGNATURE.fullmatch(fact['signature']) and asked.get('signature')==fact['signature']
            and type(fact.get('slot')) is int and fact['slot']>0 and type(fact.get('block_time')) is int and 0<fact['block_time']<=now and edge['observed_at']==fact['block_time']):return None
    if retrospective:return dict(fact,retrospective=True,seed_buy_state='unknown',source_receipt=None,known_at=max(edge['known_at'],record[1]),availability='retrospective_rpc')
    receipt=None;availability=None;seed_buy_state='unknown'
    if asked.get('source')==source and type(asked.get('flow_id')) is int:
        try:
            timing=c.execute('SELECT received_at,capture_processed_at,graph_commit_after,signature,flow_state FROM watch_flow_timing WHERE source=? AND flow_id=?',(source,asked['flow_id'])).fetchone()
            if timing and timing['signature']==fact['signature'] and timing['flow_state']=='known' and all(_finite(t) for t in tuple(timing)[:3]) and timing[0]<=timing[1]<=timing[2]<=now:receipt=timing[0];availability=timing[2]
        except sqlite3.Error:pass
        if tape is not None:
            try:
                raw=tape.execute('SELECT * FROM wallet_flow_transactions WHERE flow_id=? AND signature=?',(asked['flow_id'],fact['signature'])).fetchone()
                if receipt is None and raw and raw['slot']==fact.get('slot') and raw['commitment']=='confirmed' and _finite(raw['received_at']) and _finite(raw['processed_at']) and raw['received_at']<=raw['processed_at']<=now:receipt=raw['received_at']
                if raw and 'payload_json' in raw.keys() and raw['payload_json'] and len(raw['payload_json'])<=256*1024:
                    from sniper.feeds.yellowstone import geyser_pb2 as pb
                    from .wallet_stream_proof import confirmed_receipt,decode_payload
                    decoded=decode_payload(raw['payload_json'],archive_dir=Path(source).parent/'wallet-flow-archive')
                    if len(decoded)<=128*1024:
                        tx=confirmed_receipt(pb.SubscribeUpdate.FromString(decoded),commitment='confirmed',block_time=fact.get('block_time'))
                        if tx['slot']==fact.get('slot') and tx['transaction']['signatures'][0]==fact['signature']:
                            seed_buy_state=_seed_buy_from_receipt(tx,fact['mint'])

            except (sqlite3.Error,ValueError,TypeError,KeyError,AttributeError,IndexError,DecodeError):pass
    return dict(fact,seed_buy_state=seed_buy_state,source_receipt=receipt,known_at=max(edge['known_at'],record[1],availability or 0),availability='graph_commit_upper' if availability is not None else 'source_clock_only')


def _inventory(c,cid,now):
    try:
        columns={r[1] for r in c.execute('PRAGMA table_info(watch_token_accounts)')}
        required={'source','flow_id','owner','known_at'}
        if not required<=columns:return dict(state='unknown',reason='inventory_availability_unknown',items=[])
        rows=c.execute("SELECT a.*,t.graph_commit_after AS availability_upper FROM watch_token_accounts a JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id WHERE a.owner IN (SELECT member FROM wallet_campaign_preparations WHERE campaign_id=?) AND a.known_at>0 AND a.known_at<=? AND t.signature=a.signature AND t.flow_state='known' AND t.graph_commit_after>0 AND t.graph_commit_after<=? ORDER BY a.known_at DESC LIMIT 101",(cid,now,now)).fetchall()
        return dict(state='partial' if rows else 'unknown',items=[dict(r) for r in rows[:100]],truncated=len(rows)>100,whole_wallet_holdings=False,note='Observed account snapshots, not whole-wallet holdings or proceeds.')
    except sqlite3.Error:return dict(state='unknown',reason='inventory_availability_unknown',items=[])


def _refresh(net,c,tape,chain,source,cid,now):
    row=c.execute('SELECT * FROM wallet_campaigns WHERE campaign_id=?',(cid,)).fetchone()
    preps=c.execute('SELECT * FROM wallet_campaign_preparations WHERE campaign_id=? AND known_at<=? ORDER BY known_at,source_key LIMIT 101',(cid,now)).fetchall()
    roles=[]
    for p in preps[:100]:
        for role,address in ((('funder',row['seed']),('funding_recipient',p['member'])) if p['kind']=='funding' else (('setup_sponsor',row['seed']),('setup_user',p['member']))):
            roles.append(dict(role=role,address=address,known_at=p['known_at'],proof=json.loads(p['proof_json']),ownership_proven=False))
    buys,buy_coverage=_buys(tape,c,cid,row['bucket_start'],now)
    created=c.execute("SELECT DISTINCT e.* FROM wallet_campaign_preparations p CROSS JOIN edges e INDEXED BY edges_src ON e.network_id='solana' AND e.src=p.member WHERE p.campaign_id=? AND e.type='created_mint' AND e.source='host' AND e.proof_state='verified' AND ((e.proof_kind='research_stream' AND e.method_version='wallet-watch-launch-v1') OR (e.proof_kind='chain_fetch' AND e.method_version='confirmed_pump_create_v1')) AND e.known_at>0 AND e.known_at<=? AND e.observed_at>0 AND e.observed_at<=? AND p.event_at<=e.observed_at AND e.observed_at<=p.event_at+86400 UNION SELECT * FROM edges WHERE network_id='solana' AND src=? AND type='created_mint' AND source='host' AND proof_state='verified' AND ((proof_kind='research_stream' AND method_version='wallet-watch-launch-v1') OR (proof_kind='chain_fetch' AND method_version='confirmed_pump_create_v1')) AND known_at>0 AND known_at<=? AND observed_at>0 AND observed_at<=? AND EXISTS(SELECT 1 FROM wallet_campaign_preparations p WHERE p.campaign_id=? AND p.event_at<=edges.observed_at AND edges.observed_at<=p.event_at+86400) LIMIT 101",(cid,now,now,row['seed'],now,now,cid)).fetchall()
    invalid_launch=False
    for e in created[:100]:
        if not row['bucket_start']<=e['observed_at']<=row['bucket_end']+86400:continue
        try:fact=_launch_fact(e,chain,tape,c,now,source)
        except (ValueError,TypeError,KeyError,sqlite3.Error):fact=None
        if fact is None:invalid_launch=True;continue
        lid=_id('launch',cid,fact['signature'],fact['mint'])
        if not c.execute('SELECT 1 FROM wallet_campaign_launches WHERE launch_id=?',(lid,)).fetchone():
            state='post_atomic_seed' if fact['seed_buy_state']=='present' or any(b['signature']==fact['signature'] and b['mint']==fact['mint'] for b in buy_coverage.get('atomic_buys',[])) else 'late' if any(b['mint']==fact['mint'] for b in buys) else 'unknown'
            coverage=dict(retrospective=fact.get('retrospective',False),quote_eligible=False if fact.get('retrospective') else None,attachment='within_preparation_bucket' if e['observed_at']<row['bucket_end'] else 'outside_preparation_bucket',attachment_max_age_s=86400,seed_buy_state=fact['seed_buy_state'],creation_verified=True,launch_proof_ref=e['proof_ref'],launch_method=e['method_version'],phase='already_trading' if state=='late' else state,receipt_state='known' if fact['source_receipt'] is not None else 'unknown',prebuy_state=state,availability=fact['availability'],coverage='selective_top_level_pump_only',ownership_proven=False)
            _write(net,c,now,'INSERT INTO wallet_campaign_launches VALUES(?,?,?,?,?,?,?,NULL,?,?)',(RETROSPECTIVE_LAUNCH_VERSION if fact.get('retrospective') else LAUNCH_VERSION,lid,cid,fact['mint'],fact['signature'],fact['source_receipt'],fact['known_at'],fact['curve'],_json(coverage)))
        roles.append(dict(role='developer',address=e['src'],mint=e['dst'],known_at=fact['known_at'],proof_ref=e['proof_ref'],ownership_proven=False))
    launches=[]
    for l in c.execute('SELECT * FROM wallet_campaign_launches WHERE campaign_id=? ORDER BY launch_id LIMIT 101',(cid,)):
        item=dict(l);item['coverage']=json.loads(item.pop('coverage_json'));launches.append(item)
    mints=sorted({l['mint'] for l in launches})
    if any(l['coverage']['phase']=='post_atomic_seed' for l in launches):phase='post_atomic_seed'
    elif any(b['mint'] in mints for b in buys):phase='late' if row['first_recorded_at'] is None or min(b['received_at'] for b in buys if b['mint'] in mints)<=row['first_recorded_at'] else 'observed_preparation_before_buy'
    else:phase='unknown' if mints else 'mint_unresolved'
    doc=dict(version=VERSION,campaign_id=cid,seed=row['seed'],bucket_start=row['bucket_start'],bucket_end=row['bucket_end'],
        ownership_proven=False,relation='temporal_coincidence',prior_pattern=json.loads(row['prior_pattern_json']),roles=roles,mints=mints,launches=launches,buys=buys,
        status='observed_launch' if launches else 'no_observed_launch',prebuy_state=phase,
        coverage=dict(preparations_truncated=len(preps)>100,launches_truncated=len(created)>100 or len(launches)>100,launch_evidence_unknown=invalid_launch,
            launch_absence_proven=False,buy=buy_coverage,inventory=_inventory(c,cid,now),note='Epoch buckets can split campaigns or mix launches; no observed launch is not proven inactivity.'))
    # Emission clocks are separate metadata, not part of the immutable observation identity.
    identity=dict(doc,launches=[{k:v for k,v in l.items() if k!='emitted_at'} for l in launches])
    encoded=_json(identity)
    old=c.execute('SELECT revision,doc_json FROM wallet_campaign_versions WHERE campaign_id=? ORDER BY revision DESC LIMIT 1',(cid,)).fetchone()
    if old and old['doc_json']==encoded:return False
    _write(net,c,now,'INSERT INTO wallet_campaign_versions VALUES(?,?,NULL,?,?)',(cid,old['revision']+1 if old else 1,now,encoded))
    return True


def materialize_campaigns(net,desk_dir,now,limit=100):
    """Persist bounded preparations and descriptive observations; no provider or order calls."""
    if not _finite(now) or type(limit) is not int or not 1<=limit<=100:return dict(state='refused',reason='bad_arguments',items=[],changed=0)
    clock=net.clock()
    if not _finite(clock) or now>clock:return dict(state='refused',reason='future_or_invalid_clock',items=[],changed=0)
    deadline=time.monotonic()+1;c=tape=chain=None;changed=[]
    path=(Path(desk_dir)/'pumpswap/tape.db').resolve();source=str(path)
    try:
        c=_connect(net.path,deadline,write=True)
        reason=net._refusal(c,now)
        if reason:raise _Stop(reason)
        c.executescript(SCHEMA);c.execute('BEGIN IMMEDIATE')
        stored_clock=c.execute("SELECT last_read FROM wallet_campaign_cursor WHERE source='clock'").fetchone()
        if stored_clock and (not _finite(stored_clock[0]) or clock<stored_clock[0]):raise _Stop('campaign_clock_regressed')
        clock_floor=max(clock,stored_clock[0] if stored_clock else clock)
        if stored_clock is None or clock_floor>stored_clock[0]:
            _write(net,c,now,"INSERT INTO wallet_campaign_cursor VALUES('clock',0,?,NULL,'[]') ON CONFLICT(source) DO UPDATE SET last_read=excluded.last_read",(clock_floor,),rows=0 if stored_clock else 1)
        c.commit()  # Entry floor survives failed payloads and process restarts.
        c.execute('BEGIN IMMEDIATE')
        try:tape=_connect(path,deadline)
        except (OSError,sqlite3.Error):pass
        try:chain=_connect(net.chain_db,deadline) if net.chain_db else None
        except (OSError,sqlite3.Error):pass
        infrastructure=_known_infrastructure()
        turn_old=c.execute("SELECT cursor FROM wallet_campaign_cursor WHERE source='turn'").fetchone();turn=turn_old[0] if turn_old else 0
        quotas=[limit//3]*3
        for i in range(limit%3):quotas[(turn+i)%3]+=1
        touched=set();coverage={}
        for i,name in enumerate(('edges','setup')):
            if quotas[i]:
                identity=str(net.path.resolve()) if name=='edges' else (str(path.stat().st_dev)+':'+str(path.stat().st_ino) if tape else 'missing')
                ids,coverage[name]=_source(net,c,tape,now,quotas[i],name,identity,infrastructure,deadline);touched|=ids
        old=c.execute("SELECT cursor FROM wallet_campaign_cursor WHERE source='refresh'").fetchone();cursor=old[0] if old else 0
        refresh=c.execute('SELECT rowid,campaign_id FROM wallet_campaigns WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,quotas[2])).fetchall()
        if refresh:cursor=refresh[-1][0]
        else:cursor=0
        for r in refresh:touched.add(r['campaign_id'])
        for cid in sorted(touched):
            if time.monotonic()>deadline:raise _Stop('campaign_read_budget')
            if _refresh(net,c,tape,chain,source,cid,now):changed.append(cid)
        if refresh or old:
            _write(net,c,now,"INSERT INTO wallet_campaign_cursor VALUES('refresh',?,?,NULL,'[]') ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read",(cursor,now),rows=0 if old else 1)
        if limit%3:_write(net,c,now,"INSERT INTO wallet_campaign_cursor VALUES('turn',?,?,NULL,'[]') ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read",((turn+1)%3,now),rows=0 if turn_old else 1)
        before_commit=net.clock()
        if not _finite(before_commit) or before_commit<clock_floor:raise _Stop('campaign_clock_regressed')
        if before_commit>clock_floor:
            _write(net,c,now,"UPDATE wallet_campaign_cursor SET last_read=? WHERE source='clock'",(before_commit,),rows=0)
        clock_floor=max(clock_floor,before_commit)
        if net._size()>=net.max_bytes or c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]>=net.max_bytes:raise _Stop('disk_cap')
        c.commit()
        # Payload is already durable. A failed/interrupted finalization stays NULL and is not quote eligible.
        emitted=net.clock()
        if _finite(emitted) and emitted>=clock_floor:
            if emitted>clock_floor:
                c.execute('BEGIN IMMEDIATE')
                _write(net,c,emitted,"UPDATE wallet_campaign_cursor SET last_read=? WHERE source='clock'",(emitted,),rows=0)
                c.commit()  # Marker failure cannot discard a higher observed post-payload clock.
            c.execute('BEGIN IMMEDIATE')
            for table,column in (('wallet_campaigns','first_recorded_at'),('wallet_campaign_versions','recorded_at'),('wallet_campaign_launches','emitted_at')):
                pending=c.execute(f'SELECT rowid FROM {table} WHERE {column} IS NULL LIMIT 100').fetchall()
                for record in pending:
                    if time.monotonic()>deadline:raise _Stop('campaign_read_budget')
                    _write(net,c,emitted,f'UPDATE {table} SET {column}=? WHERE rowid=?',(emitted,record[0]))
            c.commit()
        return dict(state='partial',reason='observed_sources_only',changed=len(changed),items=[dict(campaign_id=cid) for cid in changed],coverage=coverage)
    except _Stop as exc:
        if c is not None:c.rollback()
        return dict(state='unknown',reason=str(exc),changed=0,items=[])
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:
        if c is not None:c.rollback()
        return dict(state='unknown',reason='campaign_read_budget' if time.monotonic()>deadline else 'campaign_store_unknown',changed=0,items=[])
    finally:
        for connection in (chain,tape,c):
            if connection is not None:connection.close()


def _label_service_boundaries(doc,infrastructure,tips):
    """Annotate a decoded read copy; immutable historical revisions remain unchanged."""
    doc['service_boundary']=W._service_boundary([doc.get('seed')],infrastructure,tips)
    boundary=doc['service_boundary']
    if boundary['state']=='unknown':doc['prediction_state']='infrastructure_catalogue_unknown'
    elif boundary['control_link_eligible'] is False:doc['prediction_state']='blocked_known_infrastructure'
    for role in doc.get('roles',[]):
        proof=role.get('proof') or {}
        role['service_boundary']=W._service_boundary([doc.get('seed'),role.get('address'),proof.get('seed'),proof.get('member')],infrastructure,tips)
    preparations=[r for r in doc.get('roles',[]) if r.get('role') in ('funder','funding_recipient','setup_sponsor','setup_user')]
    doc['family_link_eligible']=False if preparations and all(r['service_boundary']['control_link_eligible'] is False for r in preparations) else None
    if doc['family_link_eligible'] is False:doc['prediction_state']='blocked_known_infrastructure'
    return doc


def read_campaigns(net,as_of=None,limit=100):
    """Read persisted revisions only, without creating a directory, table or connection writer."""
    now=net.clock();cutoff=now if as_of is None else as_of
    if not _finite(now) or not _finite(cutoff) or cutoff>now or type(limit) is not int or not 1<=limit<=100:return dict(state='refused',reason='bad_arguments',items=[])
    deadline=time.monotonic()+1;c=None
    try:
        c=_connect(net.path,deadline)
        infrastructure=_known_infrastructure();tips=W._known_tip_receivers() if infrastructure is not None else frozenset()
        campaigns=c.execute('SELECT * FROM wallet_campaigns WHERE first_recorded_at<=? ORDER BY first_recorded_at DESC,campaign_id LIMIT ?',(cutoff,limit+1)).fetchall()
        items=[]
        for campaign in campaigns[:limit]:
            version=c.execute('SELECT * FROM wallet_campaign_versions WHERE campaign_id=? AND recorded_at<=? AND source_as_of<=? ORDER BY revision DESC LIMIT 1',(campaign['campaign_id'],cutoff,cutoff)).fetchone()
            if version is None:continue
            doc=json.loads(version['doc_json']);doc.update(first_recorded_at=campaign['first_recorded_at'],known_at=version['recorded_at'],revision=version['revision'],source_as_of=version['source_as_of'])
            for launch in doc['launches']:
                row=c.execute('SELECT emitted_at FROM wallet_campaign_launches WHERE launch_id=?',(launch['launch_id'],)).fetchone()
                launch['emitted_at']=row[0] if row and row[0] is not None and row[0]<=cutoff else None
            items.append(_label_service_boundaries(doc,infrastructure,tips))
        total=c.execute('SELECT count(*) FROM wallet_campaigns WHERE first_recorded_at<=?',(cutoff,)).fetchone()[0]
        pending=dict(state='unknown',reason='pending_marker_history_not_recorded')
        if as_of is None or cutoff==now:
            counts={key:c.execute(f'SELECT count(*) FROM {table} WHERE {column} IS NULL').fetchone()[0]
                for key,table,column in (('campaigns','wallet_campaigns','first_recorded_at'),('revisions','wallet_campaign_versions','recorded_at'),('launches','wallet_campaign_launches','emitted_at'))}
            pending=dict(state='known',**counts,total_markers=sum(counts.values()))
        partial=pending['state']!='known' or pending['total_markers']>0
        return dict(state='partial' if partial else 'known',items=items,truncated=len(campaigns)>limit,as_of=cutoff,pending_emissions=pending,
            denominator=dict(preparations=total,finalized_only=True,coverage='Finalized observed preparation windows; idle retained. Pending markers are separate; unseen source windows unknown.'))
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return dict(state='unknown',reason='campaign_report_unknown',items=[])
    finally:
        if c is not None:c.close()
