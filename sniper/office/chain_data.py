"""Host-side read-only Solana data for seats. A fixed method list on the research key only; one shared pace and stop across seats. No free text reaches a seat: token names, memos and logs are hostile, so results carry numbers, addresses and signatures."""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

from .market_data import _OPENER

PACE=2
TTL=60
FINAL_TTL=86400
STOP_429=900
TIMEOUT=10
STREAM_PROOF_MAX_BYTES=math.inf  # Operator removed local proof-store ceiling; provider policy is unchanged.
READ_BOUND=5_000_000
CREDITS_ESTIMATE=100   # pessimistic per call until per-method costs are verified; labelled estimate wherever it is shown
PLAN_CREDITS=10_000_000
PAGE_AT=(50,80)
MAX_ROWS=100
TRACE_HOPS=3
TRACE_HOLDER_WAIT_S=10
TRACE_RULE='first_sol_payer_oldest5_v2'   # v1 paged getSignaturesForAddress and counted failed transactions in the five; v1 claims stay graded under v1
TRACE_TXS=5
TRACE_DEADLINE_S=120
QUOTA_WORDS=('max usage','credit','limit exhausted','quota')
QUOTA_CODES=frozenset({-32429})
SUSPECT_429_S=3600
ADDRESS=re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}\Z')
SIGNATURE=re.compile(r'[1-9A-HJ-NP-Za-km-z]{64,90}\Z')
METHODS=frozenset({'getTransaction','getSignaturesForAddress','getTokenLargestAccounts','getTokenSupply','getTransactionsForAddress','getMultipleAccounts'})


def _quota_text(text):
    return any(word in str(text).lower() for word in QUOTA_WORDS)


def _rpc(url,method,params):
    """The URL carries the key, so nothing from a failure may quote it: only a status and a typed body flag leave here."""
    body=json.dumps(dict(jsonrpc='2.0',id=1,method=method,params=params)).encode()
    req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json','User-Agent':'sniper-office/1'})
    try:
        with _OPENER.open(req,timeout=TIMEOUT) as r:return r.status,json.loads(r.read(READ_BOUND))
    except urllib.error.HTTPError as exc:
        try:text=exc.read(2000).decode('utf-8','replace')
        except OSError:text=''
        return exc.code,dict(quota=True) if exc.code in (402,403,429) and _quota_text(text) else None
    except (OSError,ValueError):return None,None


def _dict(value):
    return value if isinstance(value,dict) else {}


def _next_utc_day(now):
    return (int(now//86400)+1)*86400.


def _address(value):
    return value if isinstance(value,str) and ADDRESS.match(value) else None


def _signature(value):
    return value if isinstance(value,str) and SIGNATURE.match(value) else None


def _int(value):
    return value if type(value) is int else None


def _digits(value):
    return value if isinstance(value,str) and value.isdigit() and len(value)<40 else None


def _clean(rows,fields):
    """A provider string in an address slot is hostile until its shape says otherwise: malformed becomes null and is counted."""
    bad=0;out=[]
    for row in rows:
        clean={k:check(row.get(k)) for k,check in fields.items()}
        bad+=sum(clean[k] is None and row.get(k) is not None for k in fields)
        out.append(clean)
    return out,bad


def _tx_summary(result):
    """Numbers and addresses only."""
    meta=_dict(result.get('meta'));message=_dict(_dict(result.get('transaction')).get('message'))
    keys=[k.get('pubkey') if isinstance(k,dict) else k for k in (message.get('accountKeys') or [])]
    raw=len([k for k in keys if k is not None]);keys=[_address(k) for k in keys];bad=raw-sum(k is not None for k in keys)
    pre,post=meta.get('preBalances') or [],meta.get('postBalances') or []
    sol=[dict(account=k,lamports_delta=b-a) for k,a,b in zip(keys,pre,post) if type(a) is int and type(b) is int and a!=b]
    tokens={}
    for side,rows in ((-1,meta.get('preTokenBalances') or []),(1,meta.get('postTokenBalances') or [])):
        for row in rows:
            row=_dict(row);amount=_dict(row.get('uiTokenAmount')).get('amount')
            if _address(row.get('owner')) and _address(row.get('mint')) and _digits(amount):
                key=(row['owner'],row['mint']);tokens[key]=tokens.get(key,0)+side*int(amount)
            elif row:bad+=1
    return dict(malformed_fields=bad,slot=_int(result.get('slot')),block_time_s=_int(result.get('blockTime')),failed=meta.get('err') is not None,fee_lamports=_int(meta.get('fee')),signer=keys[0] if keys else None,
                sol_deltas=sol[:MAX_ROWS],token_deltas_atomic=[dict(owner=o,mint=m,delta=d) for (o,m),d in tokens.items() if d][:MAX_ROWS],accounts_shown=min(len(sol),MAX_ROWS),accounts_changed=len(sol))


def research_url_of(env_file):
    """Read at each call, one named variable; the host holds it, a seat never does. Refuses a key the funded lane also uses, and any non-https value."""
    def url_of():
        from dotenv import dotenv_values
        from sniper.services.memecoin_research_rpc import funded_overlap
        try:env=dotenv_values(env_file)
        except OSError:raise ValueError('research_key_unknown') from None
        url=env.get('MEMECOIN_RESEARCH_HELIUS_RPC_URL') or ''
        if not url.startswith('https://'):raise ValueError('research_key_unknown')
        try:overlap=funded_overlap([url],{k:v or '' for k,v in env.items()})
        except (ValueError,TypeError):raise ValueError('research_key_unknown') from None
        if overlap:raise ValueError('research_key_is_funded')
        return url
    return url_of


def operator_page(env_file,*,send=None,prefix='Office chain tools: '):
    """A direct send with the host's own read of the bot settings: this process has no Telegram environment, and the dispatcher would audit into the funded database. Raises when not delivered, so the threshold stays unpaged."""
    def page(text):
        from dotenv import dotenv_values
        from sniper.services.memecoin_telegram import send_telegram
        # The watchdog's lookup: the bot settings live in the sibling .fomo.env.
        env={**dotenv_values(Path(env_file).with_name('.fomo.env')),**dotenv_values(env_file)}
        token=env.get('MEMECOIN_TG_BOT_TOKEN') or env.get('TELEGRAM_BOT_TOKEN');chat=(env.get('MEMECOIN_TG_CHAT_ID') or env.get('TELEGRAM_CHAT_ID') or '').split(',')[0].strip()
        if not token or not chat:raise ValueError('page_settings_unknown')
        result=(send or send_telegram)(prefix+text,token=token,chat_id=chat)
        if not (result[0] if isinstance(result,tuple) else result):raise ValueError('page_not_delivered')
    return page


class ChainData:
    def __init__(self,path,url_of,*,clock=time.time,rpc=_rpc,page=None,sleep=time.sleep,monotonic=time.monotonic):
        self.path,self.url_of,self.clock,self.rpc,self.page,self.sleep,self.monotonic=Path(path),url_of,clock,rpc,page,sleep,monotonic

    def _conn(self):
        conn=sqlite3.connect(self.path,timeout=2,isolation_level=None)
        try:
            if conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]!='wal':
                raise sqlite3.OperationalError('research_wal_unavailable')
            conn.execute('PRAGMA synchronous=FULL')
            conn.executescript('CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,at REAL,expires REAL,doc TEXT);'
                               'CREATE TABLE IF NOT EXISTS calls(at REAL,method TEXT,outcome TEXT,credits_charged INTEGER,credits_actual INTEGER,stop_until REAL);CREATE INDEX IF NOT EXISTS calls_at ON calls(at);'
                               'CREATE TABLE IF NOT EXISTS fetches(fetch_id TEXT PRIMARY KEY,shown_at REAL,tool TEXT,asked TEXT,state TEXT,reason TEXT,asof TEXT,result TEXT);'
                               'CREATE TABLE IF NOT EXISTS gate(id INTEGER PRIMARY KEY CHECK(id=1),last_at REAL,stop_until REAL,stop_reason TEXT);'
                               'CREATE TABLE IF NOT EXISTS pages(threshold INTEGER,cycle INTEGER,at REAL,PRIMARY KEY(threshold,cycle));')
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def stream_store_lifetime(self):
        # Keep the research WAL open between independent FULL proof commits, without a read transaction.
        conn=self._conn()
        try:
            assert not conn.in_transaction
            yield
            assert not conn.in_transaction
        finally:conn.close()

    def _admit(self,conn,method):
        conn.execute('BEGIN IMMEDIATE')
        try:
            now=self.clock()
            last,stop,why=conn.execute('SELECT last_at,stop_until,stop_reason FROM gate WHERE id=1').fetchone() or (0.,0.,None)
            if now<last:return 'clock_unknown'
            if now<stop:return why or 'provider_backoff'
            if now-last<PACE:return 'paced'
            conn.execute('INSERT OR REPLACE INTO gate VALUES(1,?,?,?)',(now,stop,why))
            return conn.execute('INSERT INTO calls(at,method,credits_charged) VALUES(?,?,?)',(now,method,CREDITS_ESTIMATE)).lastrowid
        finally:conn.execute('COMMIT')

    def _stop(self,conn,until,why,reservation,outcome):
        conn.execute('UPDATE gate SET stop_until=?,stop_reason=? WHERE id=1',(until,why))
        conn.execute('UPDATE calls SET outcome=?,stop_until=? WHERE rowid=?',(outcome,until,reservation))

    def call(self,method,params,*,ttl=TTL):
        """Returns (result, cached, at), or raises _Refused with a typed reason."""
        if method not in METHODS:raise _Refused('method_refused')
        key=hashlib.sha256(json.dumps([method,params],sort_keys=True).encode()).hexdigest()
        conn=self._conn()
        try:
            hit=conn.execute('SELECT at,expires,doc FROM cache WHERE key=?',(key,)).fetchone()
            if hit and 0<=self.clock()<hit[1]:return json.loads(hit[2]),True,hit[0]
            try:url=self.url_of()
            except ValueError as exc:raise _Refused(str(exc)) from None
            reservation=self._admit(conn,method)
            if isinstance(reservation,str):raise _Refused(reservation)
            status,doc=self.rpc(url,method,params)
            now=self.clock()
            if _dict(doc).get('quota'):
                self._stop(conn,_next_utc_day(now),'provider_quota_exhausted',reservation,'provider_quota_exhausted')
                raise _Refused('provider_quota_exhausted',dialed=True)
            if status==429:
                # An unexplained second 429 is treated as the month quota: over-stopping costs tool hours, under-stopping costs the funded proofs on this key.
                if conn.execute("SELECT 1 FROM calls WHERE outcome='provider_rate_limited' AND at>? LIMIT 1",(now-SUSPECT_429_S,)).fetchone():
                    self._stop(conn,_next_utc_day(now),'quota_suspected',reservation,'quota_suspected')
                    raise _Refused('quota_suspected',dialed=True)
                self._stop(conn,now+STOP_429,'provider_backoff',reservation,'provider_rate_limited')
                raise _Refused('provider_rate_limited',dialed=True)
            error=_dict(doc).get('error')
            if _dict(error).get('code') in QUOTA_CODES or _quota_text(_dict(error).get('message')):   # the month quota also arrives as a JSON-RPC error
                self._stop(conn,_next_utc_day(now),'provider_quota_exhausted',reservation,'provider_quota_exhausted')
                raise _Refused('provider_quota_exhausted',dialed=True)
            if status!=200 or not isinstance(doc,dict) or 'result' not in doc and error is None:
                conn.execute("UPDATE calls SET outcome='provider_unavailable' WHERE rowid=?",(reservation,))
                raise _Refused('provider_unavailable',dialed=True)
            if error is not None:
                # Our own vocabulary only: provider text is hostile. A big mint's refusal must not read as an outage.
                code=_dict(error).get('code')
                reason='too_many_accounts' if 'too many accounts' in str(_dict(error).get('message')).lower() else 'provider_error:%d'%code if type(code) is int else 'provider_error:other'
                conn.execute('UPDATE calls SET outcome=? WHERE rowid=?',(reason,reservation))
                raise _Refused(reason,dialed=True)
            conn.execute("UPDATE calls SET outcome='fetched' WHERE rowid=?",(reservation,))
            conn.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',(key,now,now+ttl,json.dumps(doc['result'])))
            self._maybe_page(conn,now)
            return doc['result'],False,now
        finally:conn.close()

    def _maybe_page(self,conn,now):
        """The tool's own estimate against the plan, since the key also serves funded deposit proofs. A page lost to a send failure is not retried until the next call."""
        if self.page is None:return
        cycle=int(now//(30*86400))
        used=conn.execute('SELECT coalesce(sum(credits_charged),0) FROM calls WHERE at>=?',(cycle*30*86400.,)).fetchone()[0]
        for threshold in PAGE_AT:
            if used>=PLAN_CREDITS*threshold/100 and not conn.execute('SELECT 1 FROM pages WHERE threshold=? AND cycle=?',(threshold,cycle)).fetchone():
                try:self.page('Office chain tools estimate %d credits in Unix 30-day bucket %d, %d%% of the %d plan. Tool estimate only, not the billing month, and it cannot see funded deposit proofs on the same key.'%(used,cycle,threshold,PLAN_CREDITS))
                except Exception:return
                conn.execute('INSERT INTO pages VALUES(?,?,?)',(threshold,cycle,now))

    def _answer(self,tool,asked,fn):
        try:
            out=fn()
        except _Refused as exc:
            out=dict(state='unknown',reason=exc.reason,asof=dict(kind='unknown'))
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,AttributeError):
            out=dict(state='unknown',reason='chain_store_unknown',asof=dict(kind='unknown'))
        out=dict(out,tool=tool,asked=asked,network_id='solana',provenance='research_rpc')
        if out.get('reason')=='bad_arguments':return out
        try:
            conn=self._conn()
            try:
                out['fetch_id']=uuid.uuid4().hex
                conn.execute('INSERT INTO fetches VALUES(?,?,?,?,?,?,?,?)',(out['fetch_id'],self.clock(),tool,json.dumps(asked),out['state'],out.get('reason'),json.dumps(out.get('asof')),json.dumps(out)[:8192]))
            finally:conn.close()
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='chain_store_unknown',tool=tool)
        return out

    def record_stream_fact(self,tool,asked,fact,*,commitment):
        """Local proof receipt only: never populate the finalized RPC cache or spend credits."""
        if commitment!='confirmed':raise ValueError('stream_commitment_unproven')
        out=dict(fact,tool=tool,asked=asked,network_id='solana',provenance='research_stream',commitment=commitment)
        identity=json.dumps([tool,asked],sort_keys=True,allow_nan=False)
        out['fetch_id']='stream-'+hashlib.sha256(identity.encode()).hexdigest()
        encoded=json.dumps(out,sort_keys=True,allow_nan=False)
        if len(encoded.encode())>8192:return dict(state='unknown',reason='stream_proof_oversize')
        conn=None
        try:
            conn=self._conn();conn.execute('PRAGMA busy_timeout=0');conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT result,shown_at FROM fetches WHERE fetch_id=?',(out['fetch_id'],)).fetchone()
            if old is not None:
                return dict(out,proof_stored_at=old[1]) if old[0]==encoded else dict(state='unknown',reason='stream_proof_identity_changed')
            wal=Path(str(self.path)+'-wal')
            wal_bytes=wal.stat().st_size if wal.exists() else 0
            physical=self.path.stat().st_size+wal_bytes
            # Reserve pages for this bounded <=8 KiB record and its index split before the commit.
            if physical+65536>STREAM_PROOF_MAX_BYTES:
                return dict(state='unknown',reason='stream_proof_store_cap')
            stored_at=self.clock()
            event_at=(out.get('asof') or {}).get('block_time_s')
            if type(stored_at) not in (int,float) or not math.isfinite(stored_at) or stored_at<=0 or (type(event_at) in (int,float) and event_at>stored_at):
                return dict(state='unknown',reason='stream_proof_clock_unknown')
            conn.execute('INSERT INTO fetches VALUES(?,?,?,?,?,?,?,?)',(out['fetch_id'],stored_at,tool,json.dumps(asked),out['state'],out.get('reason'),json.dumps(out.get('asof')),encoded))
            if conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]+wal_bytes+65536>STREAM_PROOF_MAX_BYTES:
                conn.rollback()
                return dict(state='unknown',reason='stream_proof_store_cap')
            conn.commit()
            return dict(out,proof_stored_at=stored_at)
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='stream_proof_store_unknown')
        finally:
            if conn is not None:conn.close()

    def transaction(self,signature):
        asked=dict(signature=signature)
        if not isinstance(signature,str) or not SIGNATURE.match(signature):return dict(state='unknown',reason='bad_arguments',tool='chain_transaction')
        def fn():
            result,cached,at=self.call('getTransaction',[signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')],ttl=FINAL_TTL)
            if result is None:return dict(state='unknown',reason='not_found_at_finalized',asof=dict(kind='now',at=at),cached=cached)
            if not isinstance(result,dict):raise _Refused('provider_malformed')
            return dict(state='known',asof=dict(kind='finalized_transaction',block_time_s=_int(result.get('blockTime'))),cached=cached,fetched_at=at,**_tx_summary(result))
        return self._answer('chain_transaction',asked,fn)

    def wallet_history(self,address,*,limit=50,before=None,as_of=None,page_token=None):
        """Newest first. as_of drops anything later, and says so; a page that fills is truncated with a cursor, so an empty or short answer is only ever a complete read."""
        asked=dict(address=address,limit=limit,before=before,as_of=as_of)
        if page_token is not None:asked['page_token']=page_token
        if (not isinstance(address,str) or not ADDRESS.match(address) or type(limit) is not int or not 1<=limit<=MAX_ROWS
            or before is not None and (not isinstance(before,str) or not SIGNATURE.match(before)) or as_of is not None and (type(as_of) not in (int,float) or not math.isfinite(as_of) or as_of<=0)
            or page_token is not None and (not isinstance(page_token,str) or not 0<len(page_token)<=256 or as_of is None or before is not None)):
            return dict(state='unknown',reason='bad_arguments',tool='chain_wallet_history')
        def bounded():
            # Reads at the as_of on the provider's side. Filtering the newest page here returned empty whenever the wallet had a full page after as_of (seat request 30454).
            config=dict(transactionDetails='signatures',sortOrder='desc',limit=limit,commitment='finalized',maxSupportedTransactionVersion=1,filters=dict(blockTime=dict(lte=int(as_of))))
            if page_token:config['paginationToken']=page_token
            result,cached,at=self.call('getTransactionsForAddress',[address,config])
            rows=_dict(result).get('data')
            if not isinstance(rows,list):raise _Refused('provider_malformed')
            rows=[_dict(r) for r in rows]
            items,bad=_clean([dict(signature=r.get('signature'),slot=r.get('slot'),block_time_s=r.get('blockTime')) for r in rows],dict(signature=_signature,slot=_int,block_time_s=_int))
            if any(type(i['block_time_s']) is not int or i['block_time_s']>as_of for i in items):raise _Refused('provider_cutoff_malformed')   # the provider's filter is checked, not trusted
            for item,r in zip(items,rows):item['failed']=r.get('err') is not None
            token=_dict(result).get('paginationToken')
            token=token if isinstance(token,str) and token else None
            return dict(state='partial' if token else 'known',asof=dict(kind='time',at=as_of),cached=cached,fetched_at=at,items=items,truncated=bool(token),cursor=None,page_token=token,malformed_fields=bad,dropped_unknown_time=0)
        def fn():
            if as_of is not None and before is None:return bounded()
            config=dict(limit=limit,commitment='finalized')
            if before:config['before']=before
            result,cached,at=self.call('getSignaturesForAddress',[address,config])
            if not isinstance(result,list):raise _Refused('provider_malformed')
            rows=[_dict(r) for r in result]
            items,bad=_clean([dict(signature=r.get('signature'),slot=r.get('slot'),block_time_s=r.get('blockTime')) for r in rows],dict(signature=_signature,slot=_int,block_time_s=_int))
            for item,r in zip(items,rows):item['failed']=r.get('err') is not None
            unknown_time=sum(not isinstance(i['block_time_s'],int) for i in items)
            if as_of is not None:items=[i for i in items if isinstance(i['block_time_s'],int) and i['block_time_s']<=as_of]
            truncated=len(rows)>=limit
            return dict(state='partial' if truncated else 'known',asof=dict(kind='time',at=as_of) if as_of is not None else dict(kind='now',at=at),cached=cached,fetched_at=at,items=items,
                        truncated=truncated,cursor=_signature(rows[-1].get('signature')) if truncated and rows else None,malformed_fields=bad,dropped_unknown_time=unknown_time if as_of is not None else 0)
        return self._answer('chain_wallet_history',asked,fn)

    def token_holders(self,mint,*,as_of=None):
        asked=dict(mint=mint,as_of=as_of)
        if not isinstance(mint,str) or not ADDRESS.match(mint):return dict(state='unknown',reason='bad_arguments',tool='chain_token_holders')
        if as_of is not None:return dict(state='unknown',reason='as_of_unsupported',tool='chain_token_holders',asof=dict(kind='unknown'))   # the chain gives current holders only
        def fn():
            result,cached,at=self.call('getTokenLargestAccounts',[mint,dict(commitment='finalized')])
            value=_dict(result).get('value')
            if not isinstance(value,list):raise _Refused('provider_malformed')
            items,bad=_clean([_dict(v) for v in value[:MAX_ROWS]],dict(address=_address,amount=_digits,decimals=_int))
            owners_state='unknown'
            try:
                addresses=[i['address'] for i in items if i['address']]
                got,_c,owner_at=self._paced_call('getMultipleAccounts',[addresses,dict(encoding='jsonParsed',commitment='finalized')],self.monotonic()+TRACE_HOLDER_WAIT_S,[])
                at=max(at,owner_at)   # either read can be the cached one
                accounts=_dict(got).get('value')
                if isinstance(accounts,list) and len(accounts)==len(addresses):
                    infos={a:_dict(_dict(_dict(_dict(v).get('data')).get('parsed')).get('info')) for a,v in zip(addresses,accounts)}
                    owner_of={a:_address(i.get('owner')) if i.get('mint')==mint else None for a,i in infos.items()}   # only an account of the asked mint
                    for item in items:item['owner']=owner_of.get(item['address'])
                    owners_state='known' if all(i.get('owner') for i in items) else 'partial'
            except _Refused as exc:
                if exc.reason in ('provider_quota_exhausted','quota_suspected'):raise
            return dict(state='partial',reason='top_20_only',asof=dict(kind='now',at=at),cached=cached,fetched_at=at,
                        items=items,item_count=len(items),malformed_fields=bad,owners=owners_state,owners_note='two reads, not one snapshot: accounts then owners, asof is the later read; top accounts only; pool, exchange and program owners appear as plain owners; a failed owner read leaves token accounts only')
        return self._answer('chain_token_holders',asked,fn)

    def _paced_call(self,method,params,deadline,dials,*,ttl=TTL):
        """A trace needs several calls, so it waits its turn; any other refusal ends it."""
        while True:
            if self.monotonic()>=deadline:raise _Refused('trace_deadline')
            try:
                out=self.call(method,params,ttl=ttl)
                if not out[1]:dials.append(1)
                return out
            except _Refused as exc:
                if exc.dialed:dials.append(1)
                if exc.reason!='paced' or self.monotonic()+PACE>=deadline:raise
                self.sleep(PACE)

    def _funder(self,wallet,deadline,dials):
        """Who first sent this wallet SOL, from its oldest transactions. Returns a dict with funder or unknown; 'none' only on a history read to its true start."""
        # Oldest first directly, so a wallet with a long history costs one call, not a page per 1000.
        page,_c,_a=self._paced_call('getTransactionsForAddress',[wallet,dict(transactionDetails='signatures',sortOrder='asc',limit=TRACE_TXS,maxSupportedTransactionVersion=1,filters=dict(status='succeeded'))],deadline,dials)
        rows=_dict(page).get('data')
        if not isinstance(rows,list):raise _Refused('provider_malformed')
        oldest=[_signature(_dict(r).get('signature')) for r in rows[:TRACE_TXS]]   # a provider ignoring the limit cannot add calls
        if not oldest:return dict(wallet=wallet,state='unknown',reason='no_history')
        candidates=[]
        for signature in [x for x in oldest if x]:
            result,_c,_a=self._paced_call('getTransaction',[signature,dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')],deadline,dials,ttl=FINAL_TTL)
            if not isinstance(result,dict):continue
            tx=_tx_summary(result);gain=next((d['lamports_delta'] for d in tx['sol_deltas'] if d['account']==wallet),0)
            if gain>0 and tx['signer'] and tx['signer']!=wallet and not tx['failed']:
                candidates.append(dict(funder=tx['signer'],signature=signature,lamports=gain,block_time_s=tx['block_time_s']))
        if not candidates:return dict(wallet=wallet,state='unknown',reason='no_funding_in_oldest_%d'%TRACE_TXS)
        return dict(wallet=wallet,state='known',**candidates[0],candidates_not_followed=len(candidates)-1)

    def funding_trace(self,wallet,*,hops=1):
        """Evidence, not a verdict: the signer that first paid a wallet is a fact about one transaction, and an exchange or bot often sits there. Stops at the hop limit or the first unknown and says which."""
        asked=dict(wallet=wallet,hops=hops)
        if not isinstance(wallet,str) or not ADDRESS.match(wallet) or type(hops) is not int or not 1<=hops<=TRACE_HOPS:return dict(state='unknown',reason='bad_arguments',tool='chain_funding_trace')
        def fn():
            deadline=self.monotonic()+TRACE_DEADLINE_S;dials=[];steps=[];current=wallet;seen={wallet}
            try:
                for _ in range(hops):
                    step=self._funder(current,deadline,dials);steps.append(step)
                    if step['state']!='known':break
                    current=step['funder']
                    if current in seen:
                        steps[-1]['loop']=True;break
                    seen.add(current)
            except _Refused as exc:
                steps.append(dict(wallet=current,state='unknown',reason=exc.reason))
            # Reaching the hop limit is not the start of the money: what lies beyond is unknown.
            return dict(state='partial',reason='loop' if steps and steps[-1].get('loop') else 'hop_limit' if len(steps)==hops and all(x['state']=='known' for x in steps) else 'trace_stopped',rule=TRACE_RULE,hops_requested=hops,hops_followed=sum(x['state']=='known' for x in steps),candidates_not_followed=sum(x.get('candidates_not_followed',0) for x in steps),dials=len(dials),credits_estimate=len(dials)*CREDITS_ESTIMATE,steps=steps,asof=dict(kind='now',at=self.clock()),note='candidate funding evidence: one payment by this rule, not proof of common control; exchanges and programs can mislead')
        return self._answer('chain_funding_trace',asked,fn)

    def credits_by_day(self,start,end):
        """Read-only report: the tool's own use. Estimate while credits_actual is NULL; the key's account total includes funded deposit proofs and is unknown here."""
        try:conn=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)
        except sqlite3.Error:return dict(state='unknown',reason='chain_db_missing')
        try:
            rows=conn.execute('SELECT cast(at/86400 as int),count(*),sum(credits_charged),sum(credits_actual) FROM calls WHERE at>=? AND at<? GROUP BY 1 ORDER BY 1 LIMIT 400',(start,end)).fetchall()
            methods=conn.execute('SELECT cast(at/86400 as int),method,count(*),sum(credits_charged) FROM calls WHERE at>=? AND at<? GROUP BY 1,2 ORDER BY 1,2 LIMIT 4000',(start,end)).fetchall()
            return dict(state='known',label='estimate, tool only; the account total includes funded deposit proofs and is unknown here',plan_credits=PLAN_CREDITS,days=[dict(utc_day=r[0],dials=r[1],credits_estimate=r[2],credits_actual=r[3]) for r in rows],by_method=[dict(utc_day=r[0],method=r[1],dials=r[2],credits_estimate=r[3]) for r in methods])
        except sqlite3.Error:return dict(state='unknown',reason='chain_db_unreadable')
        finally:conn.close()


class _Refused(Exception):
    def __init__(self,reason,dialed=False):
        super().__init__(reason);self.reason,self.dialed=reason,dialed
