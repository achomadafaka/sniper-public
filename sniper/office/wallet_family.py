"""Frozen local research observations, never an order selector or controller claim."""
import json
import math
import sqlite3
import time
from pathlib import Path

from sniper.office import wallet_watch as WW
from sniper.office.wallet_network import ADDRESS

TABLE = '''CREATE TABLE IF NOT EXISTS wallet_family_candidates(
 pool TEXT NOT NULL,source_signature TEXT NOT NULL,emitted_at REAL NOT NULL,
 source_receipt REAL NOT NULL,due REAL NOT NULL,early INTEGER,evidence_known_at REAL,
 actors_json TEXT NOT NULL,evidence_json TEXT NOT NULL,coverage_json TEXT NOT NULL,
 source_slot INTEGER,source_tx_index INTEGER,actor_order_json TEXT NOT NULL,
 policy TEXT NOT NULL CHECK(policy='research_only'),PRIMARY KEY(pool,source_signature))'''
PROGRAMS = {'6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P','pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'}


def _finite(v):
    return type(v) in (int,float) and math.isfinite(v) and v>0


def _watch_index(desk_dir,now,since):
    conn=None
    try:
        conn=sqlite3.connect((Path(desk_dir)/'pumpswap'/'tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
        plan=conn.execute('EXPLAIN QUERY PLAN SELECT * FROM ps_watch_events WHERE received_at<=? AND received_at>? ORDER BY received_at DESC LIMIT ?',(now,since,501)).fetchall()
        return None if any('SEARCH ps_watch_events USING' in r[3] for r in plan) else 'watch_index_missing'
    except (sqlite3.Error,OSError):
        return 'tape_unreadable'
    finally:
        if conn is not None:conn.close()


def _setups(desk_dir, now, since):
    """Only host decoder's validated setup rows; sponsorship is not funding."""
    conn=None
    try:
        conn=sqlite3.connect((Path(desk_dir)/'pumpswap'/'tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
        conn.row_factory=sqlite3.Row
        budget=[5000]
        def progress():
            budget[0]-=1
            return budget[0]<0
        conn.set_progress_handler(progress,1000)
        rows=conn.execute('SELECT * FROM wallet_setup_events WHERE received_at>? AND received_at<=? AND processed_at<=? ORDER BY received_at DESC LIMIT 501',(since,now,now)).fetchall()
        valid=[]
        for raw in rows[:500]:
            r=dict(raw)
            if (r['program'] in PROGRAMS and all(isinstance(r[k],str) and ADDRESS.fullmatch(r[k]) for k in ('payer','wallet'))
                    and isinstance(r['signature'],str) and 64<=len(r['signature'])<=88
                    and type(r['outer_ix']) is int and r['outer_ix']>=0
                    and _finite(r['received_at']) and _finite(r['processed_at']) and r['received_at']<=r['processed_at']):
                valid.append(dict(r,kind='account_setup_sponsor'))
        return valid,'partial' if len(rows)>500 or len(valid)!=len(rows) else 'known'
    except (sqlite3.Error,OSError,KeyError,TypeError):
        return [],'unknown'
    finally:
        if conn is not None:conn.close()


def _row(raw):
    r=dict(raw)
    for k in ('actors','evidence','coverage','actor_order'):r[k]=json.loads(r.pop(k+'_json'))
    r['early']=None if r['early'] is None else bool(r['early'])
    r['ownership_proven']=False
    return r


def materialize_candidates(net, desk_dir, now, lookback_s=60, limit=100):
    """Bounded watched BUY groups; first emission freezes all evidence across restarts.

    shortcut: watched rows cover only this bounded window; empty sells never prove no sale.
    """
    if not _finite(now) or not _finite(lookback_s) or lookback_s>3600 or type(limit) is not int or not 1<=limit<=500:
        return dict(state='refused',reason='bad_arguments',items=[])
    clock=net.clock()
    if not _finite(clock) or now>clock:return dict(state='refused',reason='future_or_invalid_clock',items=[])
    deadline=time.monotonic()+1.0
    why=_watch_index(desk_dir,now,now-lookback_s)
    if why:return dict(state='unknown',reason=why,items=[],as_of=now)
    tape=WW.events(desk_dir,now=now,since=now-lookback_s,limit=limit)
    if tape['state']!='known':return dict(state='unknown',reason=tape.get('reason','tape_unknown'),items=[],as_of=now)
    setups,setup_state=_setups(desk_dir,now,now-lookback_s)
    groups={}
    for r in tape['items']:
        if r.get('is_buy')==1 and _finite(r.get('received_at')) and all(isinstance(r.get(k),str) and r[k] for k in ('pool','signature','actor')):
            groups.setdefault((r['pool'],r['signature']),[]).append(r)
    # Equal receipt timestamps can interleave transaction rows at the limit boundary.
    boundary=min((r['received_at'] for r in tape['items']),default=None) if tape.get('truncated') else None
    deferred=0
    conn=None
    try:
        conn=net._conn();conn.execute('PRAGMA busy_timeout=0');conn.execute(TABLE)
        conn.execute('BEGIN IMMEDIATE')
        items=[];cache={};creator_cache={};pending=[]
        for (pool,sig),rows in sorted(groups.items()):
            old=conn.execute('SELECT * FROM wallet_family_candidates WHERE pool=? AND source_signature=?',(pool,sig)).fetchone()
            if old is not None:
                items.append(_row(old));continue
            if boundary is not None and any(r['received_at']<=boundary for r in rows):
                deferred+=1
                continue
            why=net._refusal(conn,now,rows=1)
            if why:
                conn.rollback()
                return dict(state='refused',reason=why,items=[],as_of=now)
            actors=sorted({r['actor'] for r in rows});funding=[];excluded=[];funding_states=[];hub_campaigns={}
            for actor in actors:
                if actor not in cache:
                    cache[actor]=(WW.funding_links(net,address=actor,hops=1,limit=10,as_of=now,verified_only=True)
                                  if time.monotonic()<deadline else dict(state='unknown',reason='read_budget'))
                links=cache[actor];funding_states.append(links['state'])
                funding.extend(links.get('funding_chain',[]));excluded.extend(links.get('excluded_funders',[]))
                funding.extend(links.get('hub_funding',[]))
                for campaign in links.get('hub_campaigns',[]):hub_campaigns[(campaign['funder'],campaign['bucket_start'])]=campaign
            funding=list({json.dumps(f,sort_keys=True):f for f in funding}.values())
            # Legacy graph verification only binds addresses in a fetch, not a transfer.
            for fact in funding:
                fact['transfer_proven']=(fact.get('source')=='host' and fact.get('proof_state')=='verified'
                    and (fact.get('proof_kind'),fact.get('method_version')) in {
                        ('chain_fetch','wallet-watch-receipt-v1'),('research_stream','wallet-watch-stream-v1')})
            setup=[r for r in setups if r['wallet'] in actors or r['payer'] in actors]
            mint=rows[0].get('mint')
            if mint not in creator_cache:
                creator_cache[mint]=(net.read(seed=mint,hops=1,limit=20,types=['created_mint'],as_of=now,verified_only=True)
                                     if isinstance(mint,str) and ADDRESS.fullmatch(mint) and time.monotonic()<deadline
                                     else dict(state='unknown',edges=[]))
            creator_result=creator_cache[mint]
            creators=[e for e in creator_result.get('edges',[]) if e['dst']==mint and e['type']=='created_mint']
            independent={e['src'] for e in creators}|set(actors)
            direct_hubs={f['dst'] for f in funding if f.get('hub_relation')=='direct_payment_only'}
            family=(independent|{f[k] for f in funding for k in ('src','dst')}|{r[k] for r in setup for k in ('payer','wallet')})-(direct_hubs-independent)
            sells=[dict(r) for r in tape['items'] if r.get('pool')==pool and r.get('is_buy')==0 and r.get('actor') in family]
            known=[f['known_at'] for f in funding+creators if _finite(f.get('known_at'))]+[s['processed_at'] for s in setup]
            evidence_at=max(known+[r['received_at'] for r in sells]) if known else None
            receipt=min(r['received_at'] for r in rows);due=receipt+10
            coverage=dict(funding='partial' if any(s!='known' for s in funding_states) else 'known' if funding else 'unknown',
                          strict_funding='known' if any(f['transfer_proven'] for f in funding) else 'unknown',
                          setup=setup_state,creators=creator_result['state'] if creators else 'unknown',sells='partial',
                          window_since=now-lookback_s,window_through=now,
                          funding_reasons=sorted({cache[a].get('reason') for a in actors if cache[a].get('reason')}),
                          funding_truncated=any(cache[a].get('funding_edges_truncated',False) for a in actors),tape='partial' if tape.get('truncated') else 'observed_only',
                          timing='emission_pending',note='Missing rows are not absence; same transaction actors are atomic; evidence is not control.')
            order=[dict(actor=r['actor'],ordinal=r.get('ordinal'),slot=r.get('slot'),tx_index=r.get('tx_index')) for r in sorted(rows,key=lambda r:r.get('ordinal',-1))]
            slots={r.get('slot') for r in rows};indices={r.get('tx_index') for r in rows}
            values=(pool,sig,now,receipt,due,None,evidence_at,
                    json.dumps(actors),json.dumps(dict(funding=funding,excluded_funders=excluded,setup=setup,creators=creators,sells=sells,hub_campaigns=[hub_campaigns[k] for k in sorted(hub_campaigns)])),json.dumps(coverage),
                    next(iter(slots)) if len(slots)==1 else None,next(iter(indices)) if len(indices)==1 else None,json.dumps(order),'research_only')
            conn.execute('INSERT INTO wallet_family_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
            pending.append((pool,sig,due,bool(known),coverage))
            net._count(conn,now,rows=1)
            items.append(_row(conn.execute('SELECT * FROM wallet_family_candidates WHERE pool=? AND source_signature=?',(pool,sig)).fetchone()))
        if net._size()>=net.max_bytes or conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>=net.max_bytes:
            raise ValueError('disk_cap')
        conn.commit()
        # Provisional rows persist with early=NULL; only a completed commit can prove emission timing.
        emitted=net.clock()
        valid_clock=_finite(emitted) and emitted>=now
        for pool,sig,due,has_evidence,coverage in pending:
            coverage['timing']='late' if valid_clock and emitted>=due else 'before_due' if valid_clock else 'clock_unknown'
            early=(False if emitted>=due else has_evidence or None) if valid_clock else None
            conn.execute('UPDATE wallet_family_candidates SET emitted_at=?,early=?,coverage_json=? WHERE pool=? AND source_signature=?',
                         (emitted if valid_clock else now,early,json.dumps(coverage),pool,sig))
        conn.commit()
        items=[_row(conn.execute('SELECT * FROM wallet_family_candidates WHERE pool=? AND source_signature=?',
                                (r['pool'],r['source_signature'])).fetchone()) for r in items]
        return dict(state='partial' if deferred else 'known',reason='atomic_group_truncated' if deferred else None,
                    as_of=now,items=items,truncated=tape.get('truncated',False),deferred_groups=deferred)
    except (sqlite3.Error,OSError,ValueError):
        if conn is not None:conn.rollback()
        return dict(state='unknown',reason='candidate_store_unavailable',items=[],as_of=now)
    finally:
        if conn is not None:conn.close()
