#!/usr/bin/env python3
"""Bounded, separately pinned Wallet universe observation. No funded writer or signer."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone,timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sniper.services import wallet_universe_metrics as m
from sniper.persistence.db_core import get_readonly_connection


from sniper.util.clock import real_utc_now_iso


# Publication and scheduling need metadata, never saved transaction histories.
_FLOW_METADATA_SQL = """json_replace(
    json_remove(result,'$.source_view','$.venue_metadata','$.venue_metadata_partial',
                       '$.errors','$.source','$.research_source_proof'),
    '$.state',CASE WHEN json_type(result,'$.state')='object' AND json_extract(result,'$.state')!='{}'
                THEN json_object('version',json_extract(result,'$.state.version'),
                                 'binding',json_extract(result,'$.state.binding'),
                                 'to_block',json_extract(result,'$.state.to_block'),
                                 -- the one header current_result needs: the block after to_block
                                 'headers',CASE WHEN json_type(result,'$.state.to_block')='integer'
                                   THEN json_object(CAST(json_extract(result,'$.state.to_block')+1 AS TEXT),
                                        json_extract(result,'$.state.headers."'||(json_extract(result,'$.state.to_block')+1)||'"'))
                                   ELSE json_object() END)
                ELSE json_extract(result,'$.state') END)"""


def now_iso():
    from sniper.util.state_paths import state_path
    return real_utc_now_iso(path=state_path('data','clock_offset.json'))


def source_signature(chain,mint,key):
    if chain=='solana':return key if isinstance(key,str) and key and ':' not in key else None
    if isinstance(key,str) and re.fullmatch(r'0x[0-9a-fA-F]{64}',key):return key.lower()
    if isinstance(key,str):
        parts=key.split(':')
        if len(parts)==3 and parts[0]==chain and parts[2].lower()==mint.lower():
            return source_signature(chain,mint,parts[1])
    return None


def inputs(root):
    """Independent short read snapshot, all source buys before our admission. No write connection."""
    from sniper.services.wallet_observation import observation_owners
    ledger=get_readonly_connection(str(root/'sniper_tracker.db'))
    try:
        uids={r[0] for r in ledger.execute('SELECT source_user_id FROM memecoin_wallets WHERE excluded=0')}
        owners={(m.canonical_chain_id(c),m.normalize_address(c,a)):(uid,h) for (c,a),(uid,h) in
                observation_owners(ledger,uids).items() if uid in uids}
        floor=(m.aware(now_iso())-timedelta(days=14)).isoformat()
        clocks,clock_status=m.source_clocks(root/'data/wallet_universe_metrics.db')
        rows=ledger.execute('SELECT signature,source_user_id,address,network_id,mint,block_time,detected_at,source_slot,source_valuation_json '
            'FROM onchain_wallet_detections WHERE detected_at>=? ORDER BY detected_at LIMIT 50000',(floor,)).fetchall()
        first={}
        for sig,uid,address,network,mint,t0,detected,slot,valuation in rows:
            chain=m.canonical_chain_id(network);address=m.normalize_address(chain,address)
            mint=m.normalize_address(chain,mint)
            if owners.get((chain,address),(None,))[0]!=uid:continue
            key=(chain,uid,mint)
            proof_addresses=sorted(a for (c,a),(u,h) in owners.items() if c==chain and u==uid)
            unit=dict(chain=chain,uid=uid,address=address,mint=mint,signature=source_signature(chain,mint,sig),
                observation_key=sig,t0=t0,source_slot=slot,detected_at=detected,proof_addresses=proof_addresses)
            try:unit['source_initiation']=json.loads(valuation or '{}').get('source_initiation')
            except (ValueError,TypeError,AttributeError):unit['source_initiation']=None
            initiation=unit.get('source_initiation') or {}
            if (unit['source_slot'] is None and isinstance(initiation,dict) and
                source_signature(chain,mint,initiation.get('tx_hash'))==unit['signature'] and str(initiation.get('network_id'))==chain and
                m.normalize_address(chain,initiation.get('mint'))==mint and m.normalize_address(chain,initiation.get('beneficiary'))==address and
                initiation.get('source_user_id')==uid and type(initiation.get('block_number')) is int):
                unit['source_slot']=initiation['block_number']
            cached=clocks.get(m.clock_key(unit)) or {}
            if t0 is None and cached.get('method')=='wallet-universe-source-clock-v1' and type(cached.get('t0')) is int and cached['t0']>0:
                unit.update(t0=cached['t0'],source_slot=cached.get('source_slot'),clock_provenance=cached.get('observed_from'))
                t0=unit['t0']
            order=(m.aware(detected).timestamp(),sig)
            if key not in first or order<first[key][0]:first[key]=(order,unit)
        own=[dict(signature=m.normalize_address(r[1],r[0]),chain=m.canonical_chain_id(r[1]),at=r[2]) for r in ledger.execute("SELECT tx_sig,network_id,created_at FROM memecoin_fills WHERE tx_sig IS NOT NULL AND created_at>=?",(floor,))]
        units=[u for _order,u in first.values()]
        from sniper.services.wallet_universe_flow import METHOD
        for unit in units:
            unit['flow_method']=METHOD
            unit['own_signatures']=sorted({r['signature'] for r in own if r['chain']==unit['chain'] and unit['t0'] is not None and
                unit['t0']-120<=m.aware(r['at']).timestamp()<=unit['t0']+120})
        return owners,units,own,dict(source_rows=len(rows),source_cap_reached=len(rows)>=50000,
            census_valid=len(rows)<50000,window_from=floor,clock_cache=clock_status)
    finally:ledger.close()


def flat(root,chain):
    from sniper.services.memecoin_chains import network_ids_for,resolve_chain
    ids=network_ids_for(resolve_chain(chain));marks=','.join('?' for _ in ids)
    c=get_readonly_connection(str(root/'sniper_tracker.db'))
    try:
        if c.execute("SELECT 1 FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 AND network_id IN ("+marks+') LIMIT 1',ids).fetchone():return False
        return c.execute("SELECT 1 FROM memecoin_pending_entries WHERE status IN ('reserved','signed','submitted','submitted_unknown') AND network_id IN ("+marks+') LIMIT 1',ids).fetchone() is None
    finally:c.close()


def funded_endpoints(env):
    funded=[]
    for key,value in env.items():
        if 'RPC' in key and 'RESEARCH' not in key and not key.startswith('WALLET_UNIVERSE_') and value:
            funded.extend(v.strip() for v in value.split(',') if v.strip().startswith(('https://','http://')))
    return funded


def endpoints(env):
    funded=funded_endpoints(env)
    out={}
    candidates=[u.strip() for u in env.get('MEMECOIN_RESEARCH_HELIUS_RPC_URLS','').split(',') if u.strip()]
    # Singular legacy research key is intentionally not a fallback: its quota was retired.
    if candidates:out['solana']=candidates[0]
    for chain in ('56','4663','8453','5042','1','57073'):
        candidates=[env.get('WALLET_UNIVERSE_RPC_URL_'+chain),env.get('MEMECOIN_RESEARCH_DRPC_RPC_URL_'+chain),
                    env.get('MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_'+chain)]
        for url in candidates:
            if not url:continue
            try:out[chain]=m.isolated_url(url,funded);break
            except ValueError:continue
    if 'solana' in out:
        try:m.isolated_url(out['solana'],funded)
        except ValueError:out.pop('solana')
    return out


def research_reader(root,chain,url,limit):
    pace,_pacer=paced(root,chain,url)
    primary=m.ReadRpc(url,limit=limit,pace=pace)
    if chain!='56':return primary
    # BNB's official endpoint supplies exact old source receipts/headers. Its
    # disabled log API is never used as a history or market-data fallback.
    try:archive=m.isolated_url('https://bsc-dataseed-public.bnbchain.org',funded_endpoints(os.environ))
    except ValueError:return primary
    from sniper.services.wallet_universe_recovery import SharedResearchRead
    def recovery():
        recovery_pace,_=paced(root,chain,archive)
        return m.ReadRpc(archive,limit=limit,pace=recovery_pace)
    return SharedResearchRead(primary,recovery,limit)


def prices():
    import requests
    result={}
    with requests.Session() as session:
        for symbol in ('SOL','ETH','BNB'):
            try:
                reply=session.get('https://api.binance.com/api/v3/ticker/price',params=dict(symbol=symbol+'USDT'),timeout=5)
                reply.raise_for_status();body=reply.json();v=m.finite(body.get('price'))
                if body.get('symbol')!=symbol+'USDT' or not v:continue
                result[symbol]=dict(usd=v,observed_at=now_iso(),source='Binance '+symbol+'/USDT spot (USDT par)')
            except Exception:continue
    return result


def paced(root,chain,url):
    from sniper.services.memecoin_research_pacer import SharedPacer
    from sniper.services.memecoin_evm_exact_capture import budget
    if chain=='solana':pacer=SharedPacer.for_url(url,gap_s=1.2)
    elif budget(url)=='drpc.org (keyed)':pacer=SharedPacer.named('drpc-keyed',gap_s=1/1.5)
    else:pacer=SharedPacer.for_url(url,gap_s=1.0)
    def pace():
        if not flat(root,chain):raise ValueError('funded_book_open')
        if pacer.paused_until()>time.time():raise ValueError('rate_limit_pause')
        pacer.acquire()
        if not flat(root,chain):raise ValueError('funded_book_open')
    return pace,pacer


def pools_for(root,chain,mint):
    path=root/'data/memecoin_onchain_volume.db'
    if not path.exists():return []
    c=get_readonly_connection(str(path))
    try:
        rows=c.execute('SELECT p.pool,p.protocol,m.token0,m.token1,m.quote_side,m.token0_decimals,m.token1_decimals,m.fee,m.tick_spacing,m.hooks '
            'FROM onchain_pool_protocols p JOIN pool_meta m ON m.chain_id=p.chain_id AND m.pool=p.pool '
            'WHERE p.chain_id=? AND lower(p.mint)=?',(chain,mint.lower())).fetchall()
        known={t.lower() for t,s,d in m.assets_for(chain)}
        selected=[]
        for r in rows:
            pool=dict(zip(('pool','protocol','token0','token1','quote_side','token0_decimals','token1_decimals','fee','tick_spacing','hooks'),r))
            tokens={str(pool.get('token0') or '').lower(),str(pool.get('token1') or '').lower()}
            quote=next(iter(tokens-{mint.lower()}),None)
            if mint.lower() in tokens and (quote in known or pool['protocol']=='v4' and quote=='0x'+'0'*40):selected.append(pool)
        return selected
    finally:c.close()


def unit_key(unit):return hashlib.sha256(json.dumps(unit,sort_keys=True).encode()).hexdigest()


def current_result(unit,result):
    from sniper.services.wallet_universe_flow import METHOD
    if result.get('method')!=METHOD or result.get('state',{}).get('version')!=METHOD:return False
    binding=result['state'].get('binding') or {}
    identity={k:unit.get(k) for k in ('chain','uid','address','mint','signature','t0')}
    if binding.get('identity')!=identity:return False
    if unit['chain']!='solana' and (binding.get('source_initiation')!=unit.get('source_initiation') or
        binding.get('source_slot')!=unit.get('source_slot')):return False
    # A completed EVM range is current only with its upper-bracket proof (Codex night review W6):
    # otherwise the worker would skip a range sealed under the old maturity rule forever.
    if unit['chain']!='solana' and result.get('status')=='complete':
        from sniper.services.wallet_universe_flow import _upper_bracket_proven
        if not _upper_bracket_proven(result.get('state') or {},identity):return False
    excluded=sorted(set(unit.get('proof_addresses',[])) | {unit['address']}) if unit['chain']=='solana' else []
    return binding.get('exclusions')==excluded and binding.get('own')==sorted(set(unit.get('own_signatures',[])))


def quote_usd(chain,quote,marks,at,field='net_after_atomic'):
    asset=str(quote.get('asset') or '')
    if asset in ('native','0x'+'0'*40):symbol=m.native_symbol(chain)
    elif chain=='solana':
        from sniper.services.wallet_universe_flow import SOL_QUOTES
        spec=SOL_QUOTES.get(asset)
        if spec is None or spec[1]!=quote.get('decimals'):return None
        symbol='ETH' if spec[0]=='WETH' else spec[0]
    else:symbol=next((s for t,s,_d in m.assets_for(chain) if t.lower()==asset.lower()),None)
    px=m.price_for(symbol,marks,now=at)
    if px is None:return None
    from decimal import Decimal
    v=float(Decimal(str(quote[field]))/Decimal(10)**int(quote['decimals'])*Decimal(str(px)))
    return v if abs(v)<1e15 else None


def verified_chain(chain,rpc):
    if chain=='solana':
        from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
        expected=SOLANA_MAINNET_GENESIS;actual=rpc('getGenesisHash',[])
    else:expected=int(chain);actual=m.atomic(rpc('eth_chainId',[]))
    if actual!=expected:raise ValueError('wrong_chain')


def publish(root,db,units,owners,marks,report):
    from scripts.research.wallet_follow_funnel import publish_universe
    at=now_iso()
    # One WAL snapshot of both tables, retaining metadata rather than histories.
    # Release it before Python decoding, summary projection or disk publication.
    db.execute('BEGIN')
    try:
        capital_rows=db.execute('SELECT snapshot FROM capital').fetchall()
        flow_rows=db.execute(f'SELECT identity,{_FLOW_METADATA_SQL} AS result FROM flow_units').fetchall()
    finally:db.rollback()
    results={r['identity']:json.loads(r['result']) if r['result'] else {} for r in flow_rows}
    body=dict(version=1,generated_at=at,capital=[json.loads(r['snapshot']) for r in capital_rows],
              impact=summarize(db,units,owners,marks,at,results=results),collection=report)
    if len(json.dumps(body,allow_nan=False).encode())>m.MAX_ARTIFACT_BYTES:raise ValueError('artifact_over_bound')
    publish_universe(root/'data/wallet_universe_metrics.json',body)


def summarize(db,units,owners,marks,at,*,results=None):
    from sniper.services.wallet_universe_flow import METHOD, flow_horizons
    if results is None:
        results={r['identity']:json.loads(r['result']) if r['result'] else {} for r in
                 db.execute(f'SELECT identity,{_FLOW_METADATA_SQL} AS result FROM flow_units')}
    by=defaultdict(list)
    for unit in units:
        result=results.get(unit_key(unit),{})
        if result.get('status')=='complete' and not current_result(unit,result):
            result=dict(status='invalidated',detail='method_or_exclusion_changed')
        by[(unit['chain'],unit['uid'])].append((unit,result))
    rows=[]
    for (chain,uid),group in by.items():
        complete=[r for u,r in group if r.get('status')=='complete' and r.get('after_complete')]
        priced=[]
        for r in complete:
            values=[quote_usd(chain,q,marks,at) for q in r.get('quotes',[])]
            # A decoded complete zero-flow window may contain no other trades.
            if all(v is not None for v in values):priced.append(sum(values))
        recent=[]
        for u,r in sorted(group,key=lambda pair:pair[1].get('last_collect') or '',reverse=True):
            if r.get('status')!='complete':continue
            values=[quote_usd(chain,q,marks,at) for q in r.get('quotes',[])]
            recent.append(dict(signature=u['signature'],mint=u['mint'],t0=u['t0'],status='complete',
                value_usd=sum(values) if all(v is not None for v in values) else None,
                quotes=r.get('quotes'),scope=r.get('scope'),before_complete=r.get('before_complete'),
                horizons=r.get('horizons') or flow_horizons(r),
                source_position=r.get('source_position') or (r.get('source') or {}).get('position_evidence'),
                source_attribution='beneficiary_only' if (r.get('source_proof') or {}).get('kind')=='relay_beneficiary_purchase'
                else 'routed_payment' if (r.get('source_proof') or {}).get('kind')=='solana_routed_economic_payment'
                else 'authenticated_purchase'))
            if len(recent)==4:break
        rows.append(dict(chain=chain,uid=uid,address=group[0][0]['address'],proof_addresses=group[0][0]['proof_addresses'],
            addresses=sorted({u['address'] for u,r in group}),eligible=len(group),complete=len(complete),priced=len(priced),
            median_usd=statistics.median(priced) if priced else None,valuation='post_buy_net_quote_flow',
            states=dict(Counter(r.get('status','pending') for u,r in group)),
            details=dict(Counter(r.get('detail') or r.get('status','pending') for u,r in group if r.get('status')!='complete')),
            observed_at=max((r.get('last_collect') or '' for r in complete),default=None),computed_at=at,
            method=METHOD,scope='Net quote flow in 60s after source; USD at current snapshot prices',
            recent=recent))
    return rows


def ordered_pending(pending,complete_counts):
    by=defaultdict(list)
    for row in pending:by[row[0]['uid']].append(row)
    ranked=[]
    for uid,rows in by.items():
        rows.sort(key=lambda p:(0 if p[1].get('status')=='partial' else 1,p[2] or '',-(p[0]['t0'] or 0)))
        for wave,row in enumerate(rows):
            ranked.append((wave,complete_counts[uid],0 if row[1].get('status')=='partial' else 1,row[2] or '',uid,row))
    return [row for wave,count,resume,at,uid,row in sorted(ranked,key=lambda p:p[:5])]


def collect_chain(root,chain,url,owners,units,marks,*,max_calls,capital_only):
    """One sequential chain, with private transport/state and owned-ledger connection.

    Startup config, census and prices are immutable during this phase. The
    shared provider pacer remains the only request admission surface. No main
    ledger writer or artifact publisher belongs to this worker.
    """
    status=dict(capital_reads=0,flow_reads=0,deferred_flow_reads=0,calls=0,state='endpoint_unconfigured')
    if not url:return status
    db=None;rpc=None;started=time.monotonic()
    try:
        path=(root/'data/wallet_universe_metrics.db').resolve()
        db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=2)
        db.row_factory=sqlite3.Row
        if [r[0] for r in db.execute('SELECT identity FROM owner')]!=['wallet-universe-metrics-v1']:
            raise ValueError('foreign_ledger_refused')
        _pace,pacer=paced(root,chain,url)
        rpc=research_reader(root,chain,url,max_calls)
        verified_chain(chain,rpc)
        from sniper.services.wallet_universe_capital_reads import CapitalRead
        capital_read=CapitalRead(chain,rpc)
        seen={(r['chain'],r['address']):dict(r) for r in db.execute('SELECT * FROM capital')}
        addresses=[(a,uid) for (c,a),(uid,h) in owners.items() if c==chain]
        addresses.sort(key=lambda pair: (seen.get((chain,pair[0])) or {}).get('last_attempt') or '')
        for address,uid in addresses:
            if rpc.used>=max_calls*2//3:break
            prior=seen.get((chain,address));old=json.loads(prior['snapshot']) if prior else {}
            if old.get('method')==m.CAPITAL_METHOD and old.get('state')=='ok' and (m.aware(now_iso())-m.aware(old['observed_at'])).total_seconds()<1800:continue
            if not flat(root,chain):status['state']='funded_book_open';break
            capital_read.begin()
            row=m.observe_capital(chain,address,capital_read,marks,now=now_iso(),clock=now_iso);row['uid']=uid
            if row['state']!='ok' and old.get('state')=='ok' and old.get('uid')==uid:
                row=dict(old,last_failure=row['state'],last_attempt=now_iso())
            with db:db.execute('INSERT INTO capital VALUES (?,?,?,?,?) ON CONFLICT(chain,address) DO UPDATE SET '
                'uid=excluded.uid,snapshot=excluded.snapshot,last_attempt=excluded.last_attempt',
                (chain,address,uid,json.dumps(row,allow_nan=False),now_iso()))
            status['capital_reads']+=1
            if row.get('last_failure',row['state']) in ('rate_limit','credits_exhausted','provider_rejected'):
                pacer.pause(60);status['state']=row.get('last_failure',row['state']);break
        if not capital_only and status['state']=='endpoint_unconfigured' and rpc.used<max_calls and flat(root,chain):
            from sniper.services.wallet_universe_flow import collect_solana,collect_evm
            pending=[];count=Counter()
            for unit in units:
                if unit['chain']!=chain:continue
                stored=db.execute(f'SELECT {_FLOW_METADATA_SQL} AS result,updated_at FROM flow_units WHERE identity=?',(unit_key(unit),)).fetchone()
                result=json.loads(stored['result']) if stored and stored['result'] else {}
                if result.get('status')=='complete' and current_result(unit,result):count[unit['uid']]+=1;continue
                if result.get('state') and not current_result(unit,result):result={}
                try:
                    if result.get('next_attempt_at') and m.aware(result['next_attempt_at'])>m.aware(now_iso()):
                        status['deferred_flow_reads']+=1;continue
                except (ValueError,TypeError):pass
                pending.append((unit,result,stored['updated_at'] if stored else ''))
            pending=ordered_pending(pending,count)
            for unit,_summary,_updated in pending:
                if rpc.used>=max_calls or not flat(root,chain):break
                # Load only the selected window's complete resume state.
                stored=db.execute('SELECT result FROM flow_units WHERE identity=?',(unit_key(unit),)).fetchone()
                prior=json.loads(stored['result']) if stored and stored['result'] else {}
                if prior.get('state') and not current_result(unit,prior):prior={}
                unit_rpc=rpc.for_unit(unit) if hasattr(rpc,'for_unit') else rpc
                if not unit.get('signature'):
                    result=dict(status='identity_missing',detail='source_transaction_identity_missing')
                elif unit.get('t0') is None:
                    from sniper.services.wallet_universe_source import resolve_source_clock
                    try:
                        clock=resolve_source_clock(unit,unit_rpc)
                        with db:db.execute('INSERT INTO source_clocks VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET '
                            'clock=excluded.clock,observed_at=excluded.observed_at',(m.clock_key(unit),json.dumps(clock,allow_nan=False),now_iso()))
                        result=dict(status='partial',detail='source_clock_resolved_next_census',resolved_clock=clock)
                    except ValueError as exc:result=dict(status='clock_missing',detail=str(exc))
                else:
                    if unit['t0']+120>m.aware(now_iso()).timestamp():continue
                    excluded=[a for (c,a),(uid,h) in owners.items() if c==chain and uid==unit['uid']]
                    our_window=set(unit['own_signatures'])
                    read=None
                    try:
                        if chain=='solana':result=collect_solana(unit,rpc,state=prior.get('state'),excluded_addresses=excluded,own_signatures=our_window,max_pages=3)
                        else:
                            from sniper.services.wallet_universe_venues import prepare_pools
                            from sniper.services.wallet_universe_source import SourceRead
                            read=SourceRead(unit,unit_rpc,cached=prior.get('source_view'))
                            metadata=prior.get('venue_metadata')
                            if not metadata:
                                candidates={p['pool']:p for p in pools_for(root,chain,unit['mint'])}
                                candidates.update({p['pool']:p for p in prior.get('venue_metadata_partial') or []})
                                metadata=prepare_pools(chain,unit['mint'],unit['signature'],read,
                                    cached_pools=list(candidates.values()))
                            result=collect_evm(unit,read,state=prior.get('state'),pools=metadata,own_signatures=our_window)
                            result['venue_metadata']=metadata
                            result['source_view']=read.cached
                    except ValueError as exc:
                        result=dict(status='partial' if str(exc) in ('budget','provider_timeout','rate_limit','funded_book_open','rate_limit_pause') else 'censored',detail=str(exc))
                        if getattr(exc,'prepared_pools',None):result['venue_metadata_partial']=exc.prepared_pools
                        if read is not None:result['source_view']=read.cached
                if len(json.dumps(result,allow_nan=False).encode())>8*1024*1024:
                    result=dict(status='censored',detail='window_storage_bound')
                if result.get('status') in ('censored','clock_missing','identity_missing'):
                    retries=prior.get('retry_count',0)
                    retries=retries if type(retries) is int and 0<=retries<=32 else 0
                    result['retry_count']=min(32,retries+1)
                    result['next_attempt_at']=(m.aware(now_iso())+timedelta(seconds=min(3600,120*2**min(retries,5)))).isoformat()
                with db:db.execute('INSERT INTO flow_units VALUES (?,?,?,?) ON CONFLICT(identity) DO UPDATE SET '
                    'result=excluded.result,updated_at=excluded.updated_at',
                    (unit_key(unit),json.dumps(unit),json.dumps(result,allow_nan=False),now_iso()))
                status['flow_reads']+=1
                if result.get('detail') in ('rate_limit','credits_exhausted','provider_rejected','rate_limit_pause','funded_book_open'):
                    if result['detail']!='funded_book_open':pacer.pause(60)
                    status['state']=result['detail'];break
        if status['state']=='endpoint_unconfigured':status['state']='ok'
    except Exception as exc:
        status['state']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    finally:
        try:
            if rpc is not None:
                status['calls']=rpc.used
                rpc.close()
        finally:
            if db is not None:db.close()
            status['phase_seconds']=round(time.monotonic()-started,3)
    return status


def run_once(root,*,max_calls=120,release_sha='',capital_only=False):
    phase_started=time.monotonic()
    owners,units,own,source=inputs(root);marks={};urls=endpoints(os.environ)
    db=m.open_metrics(root/'data/wallet_universe_metrics.db',source=root/'sniper_tracker.db')
    report=dict(state='collecting',started_at=now_iso(),release_sha=release_sha,worker_pid=os.getpid(),source=source,chains={},prices=marks,
                max_calls_per_chain=max_calls,concurrent_chains=5,next_cycle_after_s=120)
    try:
        # Retain only the current census/proofs; old rows cannot consume unbounded disk or rejoin another owner.
        with db:
            db.execute('CREATE TEMP TABLE active_flows(identity TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO active_flows VALUES (?)',[(unit_key(u),) for u in units])
            db.execute('DELETE FROM flow_units WHERE identity NOT IN (SELECT identity FROM active_flows)')
            db.execute('DELETE FROM source_clocks WHERE observed_at<?',(source['window_from'],))
            for row in db.execute('SELECT chain,address,uid FROM capital').fetchall():
                if owners.get((row['chain'],row['address']),(None,))[0]!=row['uid']:
                    db.execute('DELETE FROM capital WHERE chain=? AND address=?',(row['chain'],row['address']))
        publish(root,db,units,owners,marks,report)
        marks.update(prices())
        chains=sorted({c for c,a in owners})
        report['chains']={chain:dict(state='collecting') for chain in chains}
        # Each chain owns its mutable readers and connection. All keyed dRPC
        # callers still share the same cross-process pacer. The context waits
        # for running workers before any next cycle or price/config mutation.
        with ThreadPoolExecutor(max_workers=min(5,max(1,len(chains))),thread_name_prefix='wallet-universe') as pool:
            pending={pool.submit(collect_chain,root,chain,urls.get(chain),owners,units,marks,
                max_calls=max_calls,capital_only=capital_only):chain for chain in chains}
            for future in as_completed(pending):
                chain=pending[future]
                try:report['chains'][chain]=future.result()
                except Exception as exc:
                    report['chains'][chain]=dict(state='chain_failed',error=type(exc).__name__)
                report['progress_at']=now_iso()
                publish(root,db,units,owners,marks,report)
        marks.update(prices());at=now_iso();report['finished_at']=at
        report['phase_seconds']=round(time.monotonic()-phase_started,3)
        report['state']='ok' if all(s['state']=='ok' for s in report['chains'].values()) else 'partial'
        publish(root,db,units,owners,marks,report)
        return report
    finally:db.close()


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-root',default=os.environ.get('SNIPER_STATE_ROOT','/home/bigpapi/Desktop/Sniper'))
    ap.add_argument('--max-calls',type=int,default=120,help='strict attempted RPC calls per chain/run')
    ap.add_argument('--loop',action='store_true');ap.add_argument('--capital-only',action='store_true')
    ap.add_argument('--release-sha',default='')
    args=ap.parse_args(argv)
    if not 1<=args.max_calls<=2000:raise ValueError('invalid_attempt_cap')
    root=Path(args.state_root).resolve();(root/'data').mkdir(exist_ok=True)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env
    os.environ['SNIPER_STATE_ROOT']=str(root);load_env_files(root);configure_memecoin_state_env()
    with (root/'data/wallet_universe_metrics.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            try:print(json.dumps(run_once(root,max_calls=args.max_calls,release_sha=args.release_sha,capital_only=args.capital_only)),flush=True)
            except Exception as exc:print(json.dumps(dict(state='collector_failed',error=type(exc).__name__)),flush=True)
            if not args.loop:break
            time.sleep(120)


if __name__=='__main__':main()
