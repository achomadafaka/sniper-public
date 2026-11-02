"""Owned descriptive observations. Never called by funded execution or signing."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, parse_qs

from sniper.services.memecoin_chains import canonical_chain_id

USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
WSOL='So11111111111111111111111111111111111111112'
USDT='Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'
SOL_ASSETS=((USDC,'USDC',6),(WSOL,'SOL',9),(USDT,'USDT',6))
MAX_CAPITAL_AGE_S=3600
CAPITAL_METHOD='wallet-universe-capital-v2'
MAX_ARTIFACT_BYTES=4*1024*1024
READ_METHODS=frozenset({'getGenesisHash','getBalance','getTokenAccountsByOwner',
    'getTransactionsForAddress','getTransaction','getBlock','getBlockTime','getSlot',
    'eth_chainId','eth_blockNumber','eth_getBalance','eth_call','eth_getBlockByNumber',
    'eth_getTransactionReceipt','eth_getTransactionByHash','eth_getLogs','eth_getCode'})


def aware(value):
    value=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('invalid_clock')
    return value.astimezone(timezone.utc)


def normalize_address(chain,address):
    return str(address) if canonical_chain_id(chain)=='solana' else str(address).lower()


def finite(value):
    if isinstance(value,bool):return None
    try:
        v=float(value)
        return v if math.isfinite(v) and v>=0 else None
    except (TypeError,ValueError,OverflowError): return None


def price_for(symbol,prices,*,now):
    if symbol in ('USDC','USDT','USDG','USD'): return 1.0
    p=prices.get(symbol) or {}
    v=finite(p.get('usd'))
    try: age=(aware(now)-aware(p.get('observed_at'))).total_seconds()
    except (ValueError,TypeError): return None
    return v if v is not None and v>0 and 0<=age<=900 else None


def credential_ids(url):
    parsed=urlsplit(url)
    secrets={v for key,values in parse_qs(parsed.query).items()
             if key.lower() in ('api-key','apikey','key','dkey','token') for v in values}
    if parsed.hostname and parsed.hostname.endswith('quiknode.pro'):
        secrets.update(p for p in parsed.path.split('/') if len(p)>=16)
    parts=[p for p in parsed.path.split('/') if p]
    if parts and parsed.hostname and parsed.hostname.endswith(('g.alchemy.com','infura.io','ankr.com')):
        if len(parts)>1:secrets.add(parts[-1])
    # Unknown providers may also carry a credential in their path.
    secrets.update(p for p in parts if len(p)>=16 and p not in ('getTransactionsForAddress',))
    return secrets


def isolated_url(url,funded_urls):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('invalid_research_url')
    identity=credential_ids(url)
    for funded in funded_urls:
        if funded and (url.rstrip('/')==funded.rstrip('/') or identity & credential_ids(funded)):
            raise ValueError('funded_provider_overlap')
    return url


class ReadRpc:
    """Direct research transport: checked results, strict attempts, no hidden retries/funded pool."""
    def __init__(self,url,*,limit=160,post=None,pace=None,allowed=None):
        self.url,self.limit,self.used=url,int(limit),0
        if post is None:
            import requests
            self.session=requests.Session()
            post=self.session.post
        self.post=post
        self.pace=pace or (lambda:None)
        self.allowed=READ_METHODS if allowed is None else allowed
        self.id=0
        self.last_request_at=None
        self.cache={}

    def __call__(self,method,params):
        if method not in self.allowed: raise ValueError('read_method_refused')
        if self.used>=self.limit: raise ValueError('budget')
        self.pace()
        self.used+=1;self.id+=1
        from sniper.util.clock import real_utc_now_iso
        from sniper.util.state_paths import state_path
        self.last_request_at=real_utc_now_iso(path=state_path('data','clock_offset.json'))
        try:
            response=self.post(self.url,json=dict(jsonrpc='2.0',id=self.id,method=method,params=params),timeout=12,allow_redirects=False)
            if 300<=response.status_code<400:raise ValueError('provider_redirect')
            if response.status_code in (401,403): raise ValueError('provider_rejected')
            if response.status_code==429: raise ValueError('rate_limit')
            if response.status_code>=400: raise ValueError('provider_http_error')
            data=response.json()
        except (TimeoutError,): raise ValueError('provider_timeout') from None
        except ValueError as exc:
            if str(exc) in ('provider_rejected','rate_limit','provider_http_error','provider_redirect'): raise
            raise ValueError('malformed_response') from None
        except Exception as exc:
            raise ValueError('provider_timeout' if 'timeout' in type(exc).__name__.lower() else 'provider_transport') from None
        if not isinstance(data,dict) or data.get('id')!=self.id or data.get('jsonrpc')!='2.0':
            raise ValueError('malformed_response')
        if data.get('error') is not None:
            raw=data['error'];msg=str(raw).lower()
            raise ValueError('rate_limit' if 'rate limit' in msg or 'limit exceeded' in msg or 'too many' in msg
                             else 'credits_exhausted' if 'credit' in msg or 'quota' in msg else 'rpc_rejected')
        if 'result' not in data or data['result'] is None: raise ValueError('missing_result')
        return data['result']

    def close(self):
        if hasattr(self,'session'):self.session.close()


def assets_for(chain):
    if chain=='solana': return list(SOL_ASSETS)
    from sniper.execution.evm_wallet import CHAIN_QUOTE,CHAIN_USDC,CHAIN_STABLE,native_is_quote
    from sniper.settings.memecoin import MEMECOIN_ONCHAIN_M5_USD_STABLES
    quote=CHAIN_QUOTE.get(chain) or {}
    assets={}
    if quote.get('token'):
        assets[quote['token'].lower()]=(quote.get('usd_symbol') or 'USDC',None)
    if CHAIN_USDC.get(chain):assets[CHAIN_USDC[chain].lower()]=('USDC',None)
    if CHAIN_STABLE.get(chain):assets[CHAIN_STABLE[chain].lower()]=('USDT',None)
    for token in MEMECOIN_ONCHAIN_M5_USD_STABLES.get(chain,()):assets.setdefault(token.lower(),('USDG',None))
    return [(t,s,d) for t,(s,d) in assets.items()]


def native_symbol(chain):
    if chain=='solana':return 'SOL'
    if chain=='5042':return 'USD'
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    return (CHAIN_QUOTE.get(chain) or {}).get('usd_symbol') or ('ETH' if chain=='1' else None)


def atomic(value):
    if isinstance(value,int) and not isinstance(value,bool) and value>=0:return value
    if isinstance(value,str) and re.fullmatch(r'0x[0-9a-fA-F]+',value):return int(value,16)
    raise ValueError('malformed_amount')


def component(asset,symbol,amount,decimals,prices,now):
    px=price_for(symbol,prices,now=now)
    return dict(asset=asset,symbol=symbol,atomic=str(amount),decimals=decimals,
                usd=None if px is None else float(Decimal(amount)/Decimal(10)**decimals*Decimal(str(px))),
                price=px,price_source='stable_par_assumption' if symbol in ('USD','USDC','USDT','USDG')
                else (prices.get(symbol) or {}).get('source'),
                price_at=now if symbol in ('USD','USDC','USDT','USDG') else (prices.get(symbol) or {}).get('observed_at'),state='ok')


def observe_capital(chain,address,call,prices,*,now,clock=None):
    chain=canonical_chain_id(chain)
    read_clock=clock or (lambda:now)
    finished_at=None
    row=dict(method=CAPITAL_METHOD,chain=chain,address=normalize_address(chain,address),state='unread',usd=None,
             components=[],observed_at=now,scope='Liquid native, wrapped native and trusted stables; stable par assumed')
    try:
        if chain=='solana':
            from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
            if call('getGenesisHash',[])!=SOLANA_MAINNET_GENESIS:raise ValueError('wrong_chain')
            result=call('getBalance',[address,dict(commitment='finalized')])
            slot=atomic(result['context']['slot']);row['slot']=slot
            row['components'].append(component('native','SOL',atomic(result['value']),9,prices,now))
            slots=[slot]
            for mint,symbol,dec in assets_for(chain):
                try:
                    result=call('getTokenAccountsByOwner',[address,dict(mint=mint),dict(encoding='jsonParsed',commitment='finalized',minContextSlot=slot)])
                    current=atomic(result['context']['slot']);slots.append(current)
                    if current<slot or current-slot>300:raise ValueError('incoherent_slots')
                    if not isinstance(result['value'],list):raise ValueError('malformed_response')
                    total=0;seen=set()
                    for account in result['value']:
                        if account.get('pubkey') in seen:raise ValueError('duplicate_account')
                        seen.add(account.get('pubkey'))
                        info=account['account']['data']['parsed']['info'];amount=info['tokenAmount']
                        if info.get('owner')!=address or info.get('mint')!=mint or amount.get('decimals')!=dec:
                            raise ValueError('wrong_asset_identity')
                        if not isinstance(amount.get('amount'),str) or not re.fullmatch('[0-9]+',amount['amount']):raise ValueError('malformed_amount')
                        total+=int(amount['amount'])
                    row['components'].append(component(mint,symbol,total,dec,prices,now))
                except Exception as exc:
                    if str(exc) in ('rate_limit','credits_exhausted','provider_rejected','funded_book_open','budget','rate_limit_pause'):raise
                    row['components'].append(dict(asset=mint,symbol=symbol,state=str(exc),usd=None,atomic=None))
            row['slot_range']=[min(slots),max(slots)]
            head=atomic(call('getSlot',[dict(commitment='finalized')]))
            if head<max(slots) or head-min(slots)>300:raise ValueError('stale_provider_state')
            row['block_time']=atomic(call('getBlockTime',[min(slots)]))
            if not -5<=aware(read_clock()).timestamp()-row['block_time']<=600:raise ValueError('stale_provider_state')
        else:
            if atomic(call('eth_chainId',[]))!=int(chain):raise ValueError('wrong_chain')
            height=max(0,atomic(call('eth_blockNumber',[]))-(12 if chain=='56' else 2))
            block=call('eth_getBlockByNumber',[hex(height),False])
            if atomic(block['number'])!=height or not re.fullmatch(r'0x[0-9a-fA-F]{64}',block['hash']):raise ValueError('wrong_block')
            row.update(block=height,block_hash=block['hash'],block_time=atomic(block['timestamp']))
            if not -5<=(aware(read_clock()).timestamp()-row['block_time'])<=600:raise ValueError('stale_provider_state')
            row['components'].append(component('native',native_symbol(chain),atomic(call('eth_getBalance',[address,hex(height)])),18,prices,now))
            from sniper.execution.evm_wallet import native_is_quote
            for token,symbol,_dec in ([] if native_is_quote(chain) else assets_for(chain)):
                try:
                    decimals=atomic(call('eth_call',[dict(to=token,data='0x313ce567'),hex(height)]))
                    if decimals>36:raise ValueError('invalid_decimals')
                    amount=atomic(call('eth_call',[dict(to=token,data='0x70a08231'+address[2:].lower().zfill(64)),hex(height)]))
                    row['components'].append(component(token,symbol,amount,decimals,prices,now))
                except Exception as exc:
                    if str(exc) in ('rate_limit','credits_exhausted','provider_rejected','funded_book_open','budget','rate_limit_pause'):raise
                    row['components'].append(dict(asset=token,symbol=symbol,state=str(exc),usd=None,atomic=None))
            end=call('eth_getBlockByNumber',[hex(height),False])
            if end.get('hash')!=row['block_hash']:raise ValueError('block_reorg')
            if atomic(end.get('number'))!=height or atomic(end.get('timestamp'))!=row['block_time']:
                raise ValueError('wrong_block')
        finished_at=read_clock()
        if not -5<=aware(finished_at).timestamp()-row['block_time']<=600:
            raise ValueError('stale_provider_state')
        good=all(c.get('state')=='ok' and finite(c.get('usd')) is not None for c in row['components'])
        row['state']='ok' if good else 'partial'
        row['usd']=sum(c['usd'] for c in row['components']) if good else None
    except Exception as exc:
        row.update(state=str(exc) if isinstance(exc,ValueError) else 'malformed_response',usd=None)
    row['observed_at']=finished_at or read_clock()
    return row


def open_metrics(path,*,source=None):
    path=Path(path).resolve()
    if source and path==Path(source).resolve():raise ValueError('funded_ledger_refused')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as check:
            tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and (tables-{'owner','capital','flow_units','source_clocks'} or 'owner' not in tables):raise ValueError('foreign_ledger_refused')
            if tables and check.execute('SELECT identity FROM owner').fetchall()!=[('wallet-universe-metrics-v1',)]:raise ValueError('foreign_ledger_refused')
    conn=sqlite3.connect(path,timeout=2);conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''CREATE TABLE IF NOT EXISTS owner(identity TEXT PRIMARY KEY);
      INSERT OR IGNORE INTO owner VALUES ('wallet-universe-metrics-v1');
      CREATE TABLE IF NOT EXISTS capital(chain TEXT,address TEXT,uid TEXT,snapshot TEXT,last_attempt TEXT,
        PRIMARY KEY(chain,address));
      CREATE TABLE IF NOT EXISTS flow_units(identity TEXT PRIMARY KEY,unit TEXT NOT NULL,result TEXT,updated_at TEXT);
      CREATE TABLE IF NOT EXISTS source_clocks(identity TEXT PRIMARY KEY,clock TEXT NOT NULL,observed_at TEXT NOT NULL);
    ''')
    page_size=conn.execute('PRAGMA page_size').fetchone()[0]
    conn.execute('PRAGMA max_page_count='+str(1024*1024*1024//page_size))
    return conn


def clock_key(unit):
    fields={k:unit.get(k) for k in ('chain','uid','address','mint','signature')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()


def source_clocks(path):
    """Provisional clocks only; source-buy proof is still required by the full collector."""
    path=Path(path)
    if not path.exists():return {},'not_collected'
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as c:
            if c.execute('SELECT identity FROM owner').fetchall()!=[('wallet-universe-metrics-v1',)]:raise ValueError('foreign_ledger')
            rows=c.execute('SELECT identity,clock FROM source_clocks LIMIT 50000').fetchall()
            return {k:json.loads(v) for k,v in rows},'ok'
    except (sqlite3.Error,ValueError):return {},'unavailable'


def project_metrics(snapshot,owners,*,chain,now):
    """Live identity joins, complete denominators and honest current readiness; no provider calls."""
    chain=canonical_chain_id(chain)
    proven={(canonical_chain_id(c),normalize_address(c,a)):uid for (c,a),(uid,_h) in owners.items()}
    wanted=defaultdict(set)
    for (c,a),uid in proven.items():
        if c==chain:wanted[uid].add(a)
    capital=defaultdict(dict)
    for row in snapshot.get('capital',[]):
        key=(canonical_chain_id(row.get('chain')),normalize_address(row.get('chain'),row.get('address')))
        if key[0]==chain and proven.get(key)==row.get('uid'):capital[row['uid']][key[1]]=row
    impact={r['uid']:r for r in snapshot.get('impact',[]) if canonical_chain_id(r.get('chain'))==chain}
    out={}
    for uid,addresses in wanted.items():
        samples=list(capital[uid].values());state='ok'
        if len(samples)!=len(addresses) or any(r.get('method')!=CAPITAL_METHOD or r.get('state')!='ok' or finite(r.get('usd')) is None for r in samples):state='incomplete'
        try:
            ages=[(aware(now)-aware(r.get('observed_at'))).total_seconds() for r in samples]
            if not ages or max(ages)>MAX_CAPITAL_AGE_S or min(ages)<0:state='stale'
        except (ValueError,TypeError):state='incomplete'
        cap=dict(state=state,usd=sum(r['usd'] for r in samples) if state=='ok' else None,
                 measured=len(samples),addresses=len(addresses),samples=samples,
                 observed_at=min((r.get('observed_at') for r in samples),default=None),scope='Liquid funds; stablecoins valued at par')
        flow=impact.get(uid) or {};n=flow.get('complete',0);eligible=flow.get('eligible',0)
        flow_state='not_collected'
        flow_addresses=flow.get('addresses') or [flow.get('address')]
        if flow and all(proven.get((chain,normalize_address(chain,a)))==uid for a in flow_addresses):
            counts=(n,eligible,flow.get('priced',0))
            value=flow.get('median_usd')
            valid_counts=all(type(v) is int and v>=0 for v in counts) and 0<=counts[2]<=n<=eligible
            valid_value=type(value) in (int,float) and math.isfinite(value) and abs(value)<1e15
            flow_state=('malformed' if not valid_counts else 'insufficient' if n<10 else
                        'incomplete' if not eligible or n/eligible<.75 else 'unpriced' if counts[2]<n else
                        'ok' if valid_value else 'malformed')
            try:
                age=(aware(now)-aware(snapshot.get('generated_at'))).total_seconds()
                if age<0 or age>7200:flow_state='stale'
            except (TypeError,ValueError):flow_state='stale'
            proof=flow.get('proof_addresses')
            if proof is not None and set(proof)!=addresses:flow_state='proof_changed'
            if (snapshot.get('collection',{}).get('source',{}).get('source_cap_reached')):
                flow_state='source_census_incomplete'
            from sniper.services.wallet_universe_flow import METHOD
            if flow.get('method')!=METHOD or flow.get('valuation')!='post_buy_net_quote_flow':flow_state='method_changed'
        flow=dict(flow,state=flow_state,value_usd=flow.get('median_usd') if flow_state=='ok' else None)
        out[uid]=dict(capital=cap,impact=flow)
    return dict(chain=chain,generated_at=snapshot.get('generated_at'),wallets=out,
                collection=snapshot.get('collection',{}),method='wallet-universe-metrics-v1')


def read_metrics(path,owners,*,chain,now):
    try:
        path=Path(path)
        if path.stat().st_size>MAX_ARTIFACT_BYTES:raise ValueError('artifact_over_bound')
        body=json.loads(path.read_text())
        if body.get('version')!=1 or not isinstance(body.get('capital'),list) or not isinstance(body.get('impact'),list):raise ValueError('bad_artifact')
        return project_metrics(body,owners,chain=chain,now=now)
    except (OSError,ValueError,TypeError,KeyError):
        return dict(chain=chain,wallets={},collection=dict(state='unavailable'),method='wallet-universe-metrics-v1')
