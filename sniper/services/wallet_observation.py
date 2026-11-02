"""Explicit paper-only wallet intake. No funded callbacks, mutable globals or signing."""
from datetime import datetime,timezone,timedelta
import time
import json
from sniper.services.memecoin_chains import canonical_chain_id


def _aware(raw):
    value=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if value.tzinfo is None:
        raise ValueError('aware timestamp required')
    return value


def validate_manifest(manifest,proven):
    if manifest.get('enabled') is not True:
        raise ValueError('manifest disabled')
    chain=canonical_chain_id(manifest.get('chain'))
    if chain!='solana':
        raise ValueError('unsupported research chain')
    start=_aware(manifest['forward_start'])
    if _aware(manifest['selection_at'])>=start:
        raise ValueError('selection must precede forward collection')
    owners=manifest.get('owners') or []
    cap=manifest.get('max_wallets')
    if type(cap) is not int or cap<=0 or not owners or len(owners)>cap:
        raise ValueError('explicit wallet cap required')
    out,seen=[],set()
    for row in owners:
        key=(chain,row['address'])
        if key in seen or row.get('paused') or row.get('excluded') or (proven.get(key) or (None,))[0]!=row['uid']:
            raise ValueError('owner unverified, duplicate or paused/excluded')
        seen.add(key)
        out.append(dict(row,chain=chain,forward_start=start.isoformat(),selection_at=manifest['selection_at']))
    return out


def decode_observation(tx,owner,*,received_at):
    from sniper.services.memecoin_solana_amounts import (
        QUOTE_MINTS,PUMP_PROGRAM_ID,NATIVE_SOL,account_states,resolve_pump_curve_accounts,
        resolve_pool_accounts,pump_curve_rows,_invokes_program)
    from sniper.services.memecoin_live_marks import CONSTANT_PRODUCT_PROGRAMS
    uid,address=owner['uid'],owner['address']
    sig=((tx.get('transaction') or {}).get('signatures') or ['unknown'])[0]
    source=datetime.fromtimestamp(tx['blockTime'],timezone.utc).isoformat() if tx.get('blockTime') is not None else None
    def row(mint,idx,action,state,quantity=None,quote=None,setup=None):
        return dict(origin=f'solana:{uid}:{sig}:{idx}:{mint or "unknown"}',uid=uid,chain='solana',address=address,
                    signature=sig,event_index=str(idx),mint=mint,action=action,state=state,
                    quantity_atomic=quantity,quote_atomic=quote,source_at=source,received_at=received_at,
                    setup_json=json.dumps(setup or {},sort_keys=True),payload_json=json.dumps(dict(signature=sig),sort_keys=True))
    meta=tx.get('meta')
    if not isinstance(meta,dict) or meta.get('err') is not None:
        return [row(None,-1,'unknown','partial')]
    if source is None:
        return [row(None,-1,'unknown','partial')]
    message=(tx.get('transaction') or {}).get('message') or {}
    keys=message.get('accountKeys') or []
    count=(message.get('header') or {}).get('numRequiredSignatures',0)
    signers={k.get('pubkey') for k in keys if isinstance(k,dict) and k.get('signer') is True}
    signers.update(k if isinstance(k,str) else k.get('pubkey') for k in keys[:count])
    if address not in signers:
        return [row(None,-1,'unknown','identity_mismatch')]
    deltas={}
    # Include closed token accounts; each account index is counted once per side.
    for side,sign in [('preTokenBalances',-1),('postTokenBalances',1)]:
        if side not in meta:
            return [row(None,-1,'unknown','partial')]
        seen=set()
        for balance in meta[side]:
            if balance.get('owner')!=address: continue
            key=balance.get('accountIndex')
            if key in seen: return [row(None,-1,'unknown','partial')]
            seen.add(key)
            try:
                amount=int(balance['uiTokenAmount']['amount'])
            except (KeyError,TypeError,ValueError): return [row(None,-1,'unknown','partial')]
            mint=balance.get('mint')
            if not mint or amount<0: return [row(None,-1,'unknown','partial')]
            deltas[mint]=deltas.get(mint,0)+sign*amount
    results=[]
    programs=[p for p in CONSTANT_PRODUCT_PROGRAMS if _invokes_program(tx,p) is True]
    for mint,delta in sorted(deltas.items()):
        if mint in QUOTE_MINTS or not delta: continue
        setup=None; quote=None
        curve=resolve_pump_curve_accounts(tx,mint,exclude_owner=address)
        if curve:
            curve_owner,vault,_qmint,_qvault=curve
            decoded=pump_curve_rows(tx,mint,vault,curve_owner,slot=tx.get('slot'),signature=sig)
            swap=next((r for r in decoded if r['kind']=='sol_curve_swap'),None)
            if swap and (int(swap['amount0'])>0)!=(delta>0):
                quote=abs(int(swap['amount1']))
                setup=dict(venue='pump_curve',quote_asset=NATIVE_SOL,program=PUMP_PROGRAM_ID,pool=vault)
        elif len(programs)==1:
            pool=resolve_pool_accounts(tx,mint,exclude_owner=address)
            if pool:
                authority,coin_vault,qmint,qvault=pool
                states=account_states(tx); cd=states[coin_vault][2]; qd=states[qvault][2]
                if cd and qd and (cd>0)!=(qd>0) and (cd>0)!=(delta>0):
                    quote=abs(qd);setup=dict(venue=CONSTANT_PRODUCT_PROGRAMS[programs[0]],
                                            quote_asset=qmint,program=programs[0],pool=coin_vault)
        if setup is None:
            results.append(row(mint,len(results),'unknown','unsupported'))
            continue
        setup.update(version=1,observed_at=source,market_cap=None,age_s=None,quote_depth=None,
                     token_controls=None,enrichment_state='not_collected',amount_source='decoded_pool_delta',
                     attribution='owner token delta; complex multi-swap netting remains unsupported')
        results.append(row(mint,len(results),'buy' if delta>0 else 'sell','decoded',str(abs(delta)),str(quote),setup))
    return results or [row(None,-1,'unknown','unsupported')]


def collect_tick(conn,owners,rpc_call,*,now_iso,max_calls):
    from sniper.persistence.db_wallet_observation import record_observation,advance_cursor
    if type(max_calls) is not int or max_calls<=0:
        raise ValueError('positive read cap required')
    counts=dict(calls=0,inserted=0,duplicates=0,states={})
    def call(method,params):
        if method not in ('getSignaturesForAddress','getTransaction'):
            raise ValueError('read method required')
        counts['calls']+=1
        return rpc_call(method,params)
    # Least recently serviced owner first; one busy wallet cannot monopolize the budget.
    serviced=dict(conn.execute('SELECT owner,as_of FROM collection_status'))
    owners=sorted(owners,key=lambda o:serviced.get(o['uid']+':'+o['address'],'') or '')
    for owner in owners:
        if counts['calls']>=max_calls: break
        key=owner['uid']+':'+owner['address'];state='ok'
        try:
            paging=conn.execute('SELECT frontier,head FROM paging WHERE owner=?',(key,)).fetchone()
            cursor=conn.execute('SELECT signature FROM cursors WHERE owner=?',(key,)).fetchone()
            queue=conn.execute('SELECT COUNT(*) FROM backlog WHERE owner=?',(key,)).fetchone()[0]
            # Finish an already-enumerated backlog before opening the next range.
            if not queue or (paging and paging[0]):
                limit=100
                opts=dict(limit=limit)
                if cursor and cursor[0]: opts['until']=cursor[0]
                if paging and paging[0]: opts['before']=paging[0]
                signatures=call('getSignaturesForAddress',[owner['address'],opts])
                if not isinstance(signatures,list): raise ValueError('partial RPC signature response')
                floor=_aware(owner['forward_start']).timestamp()
                if any(not isinstance(r,dict) or not r.get('signature') for r in signatures):
                    raise ValueError('partial RPC signature response')
                head=paging[1] if paging else (signatures[0]['signature'] if signatures else None)
                reached=any(r.get('blockTime') is not None and r['blockTime']<floor for r in signatures)
                frontier=signatures[-1]['signature'] if len(signatures)==limit and not reached else None
                with conn:
                    for r in signatures:
                        if r.get('blockTime') is None or r['blockTime']>=floor:
                            conn.execute('INSERT OR IGNORE INTO backlog VALUES (?,?,?,?)',
                                         (key,r['signature'],r.get('slot'),r.get('blockTime')))
                    conn.execute('INSERT OR REPLACE INTO paging VALUES (?,?,?)',(key,frontier,head))
                if frontier:
                    state='partial'
                elif not signatures and not queue:
                    state='quiet'
            paging=conn.execute('SELECT frontier,head FROM paging WHERE owner=?',(key,)).fetchone()
            if paging and paging[0] is None:
                for signature,block_time in conn.execute('SELECT signature,block_time FROM backlog WHERE owner=? '
                                                         'ORDER BY slot,signature',(key,)).fetchall():
                    if counts['calls']>=max_calls:
                        state='partial';break
                    tx=call('getTransaction',[signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1)])
                    if not isinstance(tx,dict): raise ValueError('partial transaction')
                    got=((tx.get('transaction') or {}).get('signatures') or [None])[0]
                    if got!=signature: raise ValueError('transaction identity mismatch')
                    # Enumeration may omit blockTime; the decoded transaction clock remains authoritative.
                    rows=[] if tx.get('blockTime') is not None and tx['blockTime']<_aware(owner['forward_start']).timestamp() else decode_observation(tx,owner,received_at=now_iso)
                    with conn:
                        for event in rows:
                            inserted=record_observation(conn,event)
                            counts['inserted' if inserted else 'duplicates']+=1
                        conn.execute('DELETE FROM backlog WHERE owner=? AND signature=?',(key,signature))
                remaining=conn.execute('SELECT COUNT(*) FROM backlog WHERE owner=?',(key,)).fetchone()[0]
                if not remaining:
                    with conn:
                        if paging[1]: advance_cursor(conn,owner=key,signature=paging[1],observed_at=now_iso)
                        conn.execute('DELETE FROM paging WHERE owner=?',(key,))
        except Exception as exc:
            code=getattr(getattr(exc,'response',None),'status_code',getattr(exc,'code',None))
            state=('rate_limited' if code==429 else 'banned' if code in (401,403) else
                   'timeout' if isinstance(exc,TimeoutError) or 'timeout' in type(exc).__name__.lower() else
                   'identity_mismatch' if 'identity' in str(exc) else 'partial' if isinstance(exc,ValueError) else 'unavailable')
        with conn:
            conn.execute('INSERT OR REPLACE INTO collection_status VALUES (?,?,?,?)',(key,state,None,now_iso))
        counts['states'][key]=state
    return counts


def validate_budget(budget,funded_urls):
    import math
    from urllib.parse import urlsplit
    if not budget.get('research_capacity'):
        raise ValueError('separate research capacity required')
    for key in ('max_calls','max_wallets','max_calls_day','max_quotes_day','max_active'):
        value=budget.get(key)
        if type(value) is not int or value<=0:
            raise ValueError('positive finite '+key+' required')
    cadence=budget.get('min_call_s')
    if isinstance(cadence,bool) or not isinstance(cadence,(int,float)) or not math.isfinite(cadence) or cadence<1.2:
        raise ValueError('paced research reads require min_call_s >= 1.2')
    parsed=urlsplit(budget.get('rpc_url') or '')
    if parsed.scheme!='https' or not parsed.hostname:
        raise ValueError('HTTPS research endpoint required')
    if any(urlsplit(url).hostname==parsed.hostname for url in funded_urls if url):
        raise ValueError('research capacity overlaps funded provider host')
    if budget['max_calls']>budget['max_calls_day']:
        raise ValueError('tick exceeds daily research budget')


def schedule_research_quotes(mdb,event,*,policies,now_iso,budget):
    from sniper.services import memecoin_signal_copy_quotes as cq
    from sniper.services.memecoin_policy import resolve_paper_arm_policy
    base=dict(origin=event['origin'],state='budget_exhausted',policy_ids=[],quote_jobs=0)
    if budget['remaining_calls']<=0: return base
    if event['chain']!='solana' or event['state']!='decoded' or event['action']!='buy':
        return dict(base,state='unsupported')
    now=_aware(now_iso)
    source=event.get('source_at')
    if not source: return dict(base,state='source_clock_unknown')
    age=(now-_aware(source)).total_seconds()
    if age<0 or age>90: return dict(base,state='stale_source')
    blocked=cq.admission_block(mdb,now,max_active=budget['max_active'],max_daily=budget['max_quotes_day'])
    if blocked: return dict(base,state=blocked)
    if not policies or len(policies)!=len(set(policies)):
        raise ValueError('explicit unique policies required')
    mdb.execute('CREATE TABLE IF NOT EXISTS research_policies (origin TEXT,arm TEXT,policy_id TEXT,'
                'policy_version INTEGER,policy_json TEXT,PRIMARY KEY(origin,arm))')
    old=mdb.execute('SELECT arm,policy_id,policy_version,policy_json FROM research_policies WHERE origin=?',
                    (event['origin'],)).fetchall()
    if old and {r[0] for r in old}!=set(policies):
        raise ValueError('frozen research policy set mismatch')
    assignments=list(map(tuple,old)) if old else [
        (arm,*resolve_paper_arm_policy(arm,network_id='solana',opened_at=now_iso)) for arm in policies]
    mdb.execute('CREATE TABLE IF NOT EXISTS research_events (origin TEXT PRIMARY KEY,source_at TEXT,received_at TEXT)')
    clocks=(event['origin'],event['source_at'],event.get('received_at'))
    previous=mdb.execute('SELECT origin,source_at,received_at FROM research_events WHERE origin=?',(event['origin'],)).fetchone()
    if previous and tuple(previous)!=clocks: raise ValueError('frozen event clocks mismatch')
    with mdb:
        mdb.execute('INSERT OR IGNORE INTO research_events VALUES (?,?,?)',clocks)
        for arm,pid,version,payload in assignments:
            mdb.execute('INSERT OR IGNORE INTO research_policies VALUES (?,?,?,?,?)',
                        (event['origin'],arm,pid,version,payload))
    cq.schedule_entry(mdb,dict(fomo_event_id=event['origin'],source_user_id=event['uid'],
                              network_id=event['chain'],mint=event['mint']),now=now,cohort='wallet-universe-v1')
    return dict(base,state='queued',policy_ids=[p[1] for p in assignments],quote_jobs=1)


def run_research_quote_tick(mdb,quote_fn,*,now_iso,max_quotes):
    """Unsigned quote callback, own ledger only. Never passes a custody wallet or executor."""
    from sniper.services import memecoin_signal_copy_quotes as cq
    from sniper.services.memecoin_solana_amounts import USDC
    now=_aware(now_iso);tick_start=time.monotonic()
    clock=lambda:now+timedelta(seconds=time.monotonic()-tick_start)
    if type(max_quotes) is not int or max_quotes<=0: raise ValueError('finite quote cap required')
    mdb.execute('CREATE TABLE IF NOT EXISTS research_quote_envelopes '
                '(origin TEXT,leg TEXT,horizon INTEGER,raw_json TEXT,PRIMARY KEY(origin,leg,horizon))')
    count=0
    for row in cq.claim_due(mdb,now=now,limit=max_quotes):
        if row.get('cohort')!='wallet-universe-v1': continue
        amount=str(int(cq.ENTRY_USD*1000000)) if row['leg']=='entry' else cq.expected_tokens(mdb,row['origin_event_id'])
        im,om=(USDC,row['mint']) if row['leg']=='entry' else (row['mint'],USDC)
        event=mdb.execute('SELECT source_at,received_at FROM research_events WHERE origin=?',(row['origin_event_id'],)).fetchone()
        ep={'received_at':event[1] if event else None}
        def expiry(at):
            if row['leg']=='entry':
                if not event or not event[0]: return 'source_clock_unknown'
                age=(at-_aware(event[0])).total_seconds()
                return 'stale_source' if age<0 or age>90 else None
            return 'missed_horizon' if (at-cq.parse_iso(row['due_at'])).total_seconds()>2.5 else None
        started=clock();failure=expiry(started)
        if failure:
            cq.record(mdb,row,status='failed',started=started,failure=failure,anchor=now,ep=ep)
            count+=1
            continue
        try:
            if amount is None: raise ValueError('no acquired quantity')
            raw=quote_fn(im,om,str(amount))
            completed=clock();failure=expiry(completed)
            if failure:
                cq.record(mdb,row,status='failed',started=started,quoted=completed,failure=failure,anchor=now,ep=ep)
                count+=1
                continue
            q=cq.normalise_quote(raw,input_asset=im,output_asset=om)
            if (not q or raw.get('inputMint')!=im or raw.get('outputMint')!=om or
                q['input_amount_atomic']!=str(amount) or not q['output_amount_atomic'].isascii() or
                not q['output_amount_atomic'].isdigit() or int(q['output_amount_atomic'])<=0):
                raise ValueError('quote identity/amount mismatch')
            # Keep the provider floor/fees separately; expected output is never relabeled a fill.
            mdb.execute('INSERT OR IGNORE INTO research_quote_envelopes VALUES (?,?,?,?)',
                        (row['origin_event_id'],row['leg'],row['horizon_s'],json.dumps(raw)))
            cq.record(mdb,row,status='ok',started=started,quoted=completed,quote=q,ep=ep,
                      anchor=cq.parse_iso(row.get('anchor_at')) or now)
            if row['leg']=='entry': cq.schedule_exits(mdb,row,completed)
        except Exception as exc:
            code=getattr(getattr(exc,'response',None),'status_code',getattr(exc,'code',None))
            failure=('rate_limited' if code==429 else 'banned' if code in (401,403) else
                     'timeout' if isinstance(exc,TimeoutError) else 'partial' if isinstance(exc,ValueError) else 'unavailable')
            cq.record(mdb,row,status='failed',started=started,failure=failure,anchor=now,ep=ep)
            count+=1
            if failure in ('rate_limited','banned','timeout','unavailable'): break
            continue
        count+=1
    return count


def observation_owners(ledger,uids):
    """Current verified identity, including watch-only pump profiles; no authority filtering."""
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.services.memecoin_address_registry import armed_verified_owners
    owners=watcher_address_owners(ledger)
    owners.update({('solana',address):identity for address,identity in
                   (armed_verified_owners(ledger,armed=uids,network_id='solana') or {}).items()})
    return owners


def project_research_evidence(observation,quotes,*,chain):
    """Offline only. Quote grids are measurements, never continuous policy proof."""
    from collections import defaultdict
    if canonical_chain_id(chain)!='solana': return {}
    rows=observation.execute('SELECT uid,origin,mint,action,state,source_at,received_at,setup_json '
        'FROM observations WHERE chain=? ORDER BY received_at DESC LIMIT 10001',('solana',)).fetchall()
    groups=defaultdict(list);result={}
    for row in rows[:10000]: groups[row[0]].append(row)
    for uid,events in groups.items():
        origins={r[1] for r in events}
        statuses=observation.execute('SELECT owner,state,as_of FROM collection_status WHERE owner LIKE ?',
                                     (uid+':%',)).fetchall()
        measured=[];policies=[]
        if quotes is not None:
            measured=[dict(r) for r in quotes.execute('SELECT * FROM copy_quote_result WHERE source_user_id=?',(uid,))
                      if r['origin_event_id'] in origins]
            policies=[dict(r) for r in quotes.execute('SELECT * FROM research_policies') if r['origin'] in origins]
        buys=[r for r in events if r[3]=='buy' and r[4]=='decoded']
        result[uid]=dict(state='partial' if len(rows)>10000 else 'stored',cohort='wallet-universe-v1',
            observed=len(events),buys=len(buys),sells=sum(r[3]=='sell' and r[4]=='decoded' for r in events),
            distinct_coins=len({r[2] for r in buys}),diagnostic=sum(r[4]!='decoded' for r in events),
            last_buy=next((dict(origin=r[1],mint=r[2],source_at=r[5],received_at=r[6]) for r in buys),None),
            collection=[dict(owner=r[0],state=r[1],as_of=r[2]) for r in statuses],
            quote_priced=sum(r['status']=='ok' for r in measured),quote_censored=sum(r['status']!='ok' for r in measured),
            policy_state='insufficient_quote_grid',
            policy_reason='Continuous rails, explicit net costs and source-follow sell matching are not established',
            frozen_policies=policies,quotes=measured,
            origins=[dict(origin=r[1],mint=r[2],action=r[3],state=r[4],source_at=r[5],received_at=r[6],
                          setup=json.loads(r[7])) for r in events])
    return result
