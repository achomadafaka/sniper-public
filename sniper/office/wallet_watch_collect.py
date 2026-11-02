"""Bounded off-path intake into the existing Office wallet network.

Finalized address history is paged durably; a head cursor advances only after its
interval and every detail commit. Absence is never a transfer/control proof.
No funded store, roster, settings or callback is opened here.
"""
import sqlite3
import time
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import math

from sniper.office import wallet_watch as W
from sniper.office.chain_data import CREDITS_ESTIMATE, MAX_ROWS, PACE, SIGNATURE, _Refused, _clean, _signature, _int
from sniper.office.wallet_network import CREDITS_PER_DAY, OTHER_DAYS, _day
from sniper.services.wallet_identity import verified_sol_funding
from sniper.office import wallet_token_flow as TF

SCHEMA = '''
CREATE TABLE IF NOT EXISTS watch_collect_state(
 address TEXT PRIMARY KEY,forward_floor REAL NOT NULL DEFAULT 0,cursor TEXT,cursor_slot INTEGER NOT NULL DEFAULT 0,head TEXT,head_slot INTEGER,scan_asof REAL,before_sig TEXT,exhausted INTEGER NOT NULL DEFAULT 0,
 interval_unknown INTEGER NOT NULL DEFAULT 0,last_turn INTEGER NOT NULL DEFAULT 0,last_attempt REAL,last_success REAL,last_event_at REAL,state TEXT NOT NULL DEFAULT 'unknown',reason TEXT);
CREATE TABLE IF NOT EXISTS watch_collect_pending(
 address TEXT NOT NULL,signature TEXT NOT NULL,slot INTEGER,block_time REAL,attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(address,signature));
CREATE TABLE IF NOT EXISTS watch_movements(
 address TEXT NOT NULL,signature TEXT NOT NULL,asset TEXT NOT NULL,delta_atomic TEXT,
 decimals INTEGER,kind TEXT NOT NULL,transfer_proven INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL,reason TEXT,slot INTEGER,event_at REAL,known_at REAL NOT NULL,fetch_id TEXT,
 PRIMARY KEY(address,signature,asset));
CREATE INDEX IF NOT EXISTS watch_movements_known ON watch_movements(known_at);
CREATE TABLE IF NOT EXISTS watch_collect_sources(source TEXT PRIMARY KEY,cursor INTEGER NOT NULL,last_read REAL,state TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS watch_flow_state(source TEXT PRIMARY KEY,identity TEXT NOT NULL,cursor INTEGER NOT NULL,last_read REAL,anchor TEXT);
CREATE TABLE IF NOT EXISTS watch_flow_pending(source TEXT NOT NULL,flow_id INTEGER NOT NULL,last_try REAL,first_seen REAL,reason TEXT,PRIMARY KEY(source,flow_id));
CREATE INDEX IF NOT EXISTS watch_flow_pending_age ON watch_flow_pending(first_seen);
CREATE INDEX IF NOT EXISTS watch_flow_pending_turn ON watch_flow_pending(source,last_try,flow_id);
CREATE TABLE IF NOT EXISTS watch_flow_receipts(source TEXT NOT NULL,flow_id INTEGER NOT NULL,signature TEXT,state TEXT,reason TEXT,known_at REAL,PRIMARY KEY(source,flow_id));
CREATE INDEX IF NOT EXISTS watch_flow_receipts_age ON watch_flow_receipts(known_at);
CREATE TABLE IF NOT EXISTS watch_flow_timing(source TEXT NOT NULL,flow_id INTEGER NOT NULL,signature TEXT NOT NULL,
 received_at REAL,capture_processed_at REAL,proof_stored_at REAL,graph_commit_before REAL,graph_commit_after REAL,
 selector_json TEXT NOT NULL,gaps_json TEXT NOT NULL,flow_state TEXT,flow_reason TEXT,slot INTEGER,event_at REAL,token_state TEXT,token_reason TEXT,PRIMARY KEY(source,flow_id));
CREATE INDEX IF NOT EXISTS watch_flow_timing_signature ON watch_flow_timing(signature);
CREATE INDEX IF NOT EXISTS watch_flow_timing_unwitnessed ON watch_flow_timing(source,flow_id) WHERE graph_commit_after IS NULL AND flow_state='known';
CREATE INDEX IF NOT EXISTS watch_flow_timing_upper ON watch_flow_timing(source,graph_commit_after);
CREATE TABLE IF NOT EXISTS watch_flow_expansions(source TEXT NOT NULL,flow_id INTEGER NOT NULL,signature TEXT NOT NULL,event_at REAL,
 sol_json TEXT NOT NULL,sol_cursor INTEGER NOT NULL DEFAULT 0,token_outer INTEGER NOT NULL DEFAULT -1,token_inner INTEGER NOT NULL DEFAULT -2,
 token_done INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'pending',last_try REAL,reason TEXT,PRIMARY KEY(source,flow_id));
CREATE INDEX IF NOT EXISTS watch_flow_expansions_pending ON watch_flow_expansions(state,last_try,source,flow_id);
'''
STOP_LABELS = frozenset({'infrastructure','exchange','known_infrastructure'})
METHOD = 'wallet-watch-receipt-v1'


def _uint(value):
    return type(value) is int and 0 <= value < 2**64


def movements(tx, address):
    """Native balance delta includes fees/rent. Tokens are balance observations only."""
    meta=tx.get('meta') or {}; msg=(tx.get('transaction') or {}).get('message') or {}
    keys=[k.get('pubkey') if isinstance(k,dict) else k for k in msg.get('accountKeys') or []]
    pre,post=meta.get('preBalances'),meta.get('postBalances')
    native=dict(asset='native:sol',delta_atomic=None,decimals=9,kind='balance_observation',state='unknown',reason='native_balances_unknown')
    if address in keys and isinstance(pre,list) and isinstance(post,list) and len(pre)==len(post)==len(keys):
        i=keys.index(address)
        if _uint(pre[i]) and _uint(post[i]):
            native.update(delta_atomic=str(post[i]-pre[i]),state='known',reason=None)
    out=[native]
    token_pre,token_post=meta.get('preTokenBalances'),meta.get('postTokenBalances')
    if not isinstance(token_pre,list) or not isinstance(token_post,list):
        out.append(dict(asset='tokens:unknown',delta_atomic=None,decimals=None,kind='balance_observation',state='unknown',reason='token_balances_unknown'))
        return out
    totals={}; bad=set()
    for side,rows in ((-1,token_pre),(1,token_post)):
        seen={}
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('owner'),str):
                bad.add('tokens:unknown');continue
            if row.get('owner')!=address:continue
            mint=row.get('mint'); amount=row.get('uiTokenAmount')
            if not isinstance(amount,dict):amount={}
            digits=amount.get('amount'); decimals=amount.get('decimals')
            index=row.get('accountIndex')
            if type(index) is not int or index<0:
                bad.add(mint if isinstance(mint,str) and W.ADDRESS.fullmatch(mint) else 'tokens:unknown')
            else:
                if index in seen:
                    bad.add(seen[index]);bad.add(mint if isinstance(mint,str) and W.ADDRESS.fullmatch(mint) else 'tokens:unknown')
                seen[index]=mint if isinstance(mint,str) and W.ADDRESS.fullmatch(mint) else 'tokens:unknown'
            if not isinstance(mint,str) or not W.ADDRESS.fullmatch(mint):
                bad.add('tokens:unknown');continue
            if not isinstance(digits,str) or not digits.isascii() or not digits.isdigit() or len(digits)>78 or type(decimals) is not int or not 0<=decimals<=255:
                bad.add(mint);continue
            old,dec=totals.get(mint,(0,decimals))
            if dec!=decimals:bad.add(mint)
            totals[mint]=(old+side*int(digits),decimals)
    for mint in sorted(set(totals)|bad):
        value,dec=totals.get(mint,(None,None))
        out.append(dict(asset=mint,delta_atomic=None if mint in bad else str(value),decimals=dec,kind='balance_observation',state='unknown' if mint in bad else 'known',reason='token_balance_malformed' if mint in bad else None))
    return out


def _time(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None


def selector_snapshot(path,now):
    unknown=dict(state='unknown',reason='selector_snapshot_unknown',selector_acknowledged=None)
    if _time(now) is None:return unknown
    try:
        with path.open('rb') as f:raw=f.read(65537)
        if len(raw)>65536:return dict(unknown,reason='selector_snapshot_oversize')
        value=json.loads(raw);stamp=_time(value.get('ts'));flow=value.get('wallet_flow')
        if stamp is None or stamp>now or now-stamp>30 or not isinstance(flow,dict):return unknown
        for name in ('desired_selectors','submitted_selectors'):
            if type(flow.get(name)) is not int or flow[name]<0:return unknown
        submitted=_time(flow.get('submitted_at'))
        if submitted is not None and submitted>stamp:return unknown
        return dict(state='known',heartbeat_at=stamp,snapshot_at=now,desired_selectors=flow['desired_selectors'],
                    submitted_selectors=flow['submitted_selectors'],submitted_at=submitted,selector_acknowledged=None,
                    note='batch observation only; not historical selector membership or provider acknowledgement')
    except (OSError,ValueError,TypeError,AttributeError):return unknown


class Collector:
    def __init__(self, net, desk_dir, *, page_size=MAX_ROWS, expand=True):
        if type(page_size) is not int or not 1<=page_size<=MAX_ROWS:raise ValueError('bad_page_size')
        if net.chain is None:raise ValueError('research_chain_required')
        self.net,self.chain,self.desk_dir,self.page_size,self.expand=net,net.chain,desk_dir,page_size,expand

    def _open(self):
        conn=self.net._conn()
        try:
            conn.executescript(W.SCHEMA+SCHEMA)
            TF.init_storage(conn)
            conn.execute('CREATE INDEX IF NOT EXISTS watch_token_account_latest ON watch_token_accounts(account)')
            with conn:conn.execute("INSERT OR IGNORE INTO watch_collect_sources VALUES('activation',0,?,'partial','initial_history_unknown')",(self.net.clock(),))
            return conn
        except BaseException:
            conn.close()
            raise

    def _cached(self, method, params, now):
        key=hashlib.sha256(json.dumps([method,params],sort_keys=True).encode()).hexdigest()
        conn=self.chain._conn()
        try:row=conn.execute('SELECT doc,at FROM cache WHERE key=? AND expires>?',(key,now)).fetchone()
        finally:conn.close()
        if row:
            try:return json.loads(row[0]),True,row[1]
            except (ValueError,TypeError):return None
        return None

    def _reserve(self, conn, now, cached=None):
        if cached is not None:return self.net._refusal(conn,now,rows=1)
        # Only a preflight: ChainData still atomically owns pacing and quota admission.
        gate_conn=self.chain._conn()
        try:gate=gate_conn.execute('SELECT last_at,stop_until,stop_reason FROM gate WHERE id=1').fetchone()
        finally:gate_conn.close()
        if gate:
            if now<gate[0]:return 'clock_unknown'
            if now<gate[1]:return gate[2] or 'provider_backoff'
            if now-gate[0]<PACE:return 'shared_pace'
        conn.execute('BEGIN IMMEDIATE')
        why=self.net._refusal(conn,now,rows=1)
        used=conn.execute('SELECT credits FROM daily WHERE day=?',(_day(now),)).fetchone()
        if not why and CREDITS_PER_DAY is not None and (used[0] if used else 0)+CREDITS_ESTIMATE>CREDITS_PER_DAY:why='credit_cap'
        if not why:self.net._count(conn,now,credits=CREDITS_ESTIMATE)
        conn.commit()  # crash keeps a conservative reservation; never bypass ChainData's shared gate
        return why

    def _finish(self, conn, address, now):
        s=conn.execute('SELECT * FROM watch_collect_state WHERE address=?',(address,)).fetchone()
        if s['exhausted']==1 and not conn.execute('SELECT 1 FROM watch_collect_pending WHERE address=?',(address,)).fetchone():
            conn.execute("UPDATE watch_collect_state SET cursor=COALESCE(head,cursor),cursor_slot=COALESCE(head_slot,cursor_slot),head=NULL,head_slot=NULL,before_sig=NULL,exhausted=0,state=CASE WHEN interval_unknown THEN 'partial' ELSE 'known' END,reason=CASE WHEN interval_unknown THEN 'balance_coverage_partial' ELSE NULL END,last_success=COALESCE(scan_asof,?) WHERE address=?",(now,address))

    def _page(self, conn, s, now, cached=None):
        if cached is None:
            out=self.chain.wallet_history(s['address'],limit=self.page_size,before=s['before_sig'])
        else:
            raw,_,at=cached
            if not isinstance(raw,list):return 'history_malformed'
            if any(not isinstance(r,dict) for r in raw):return 'history_malformed'
            items,bad=_clean([dict(signature=r.get('signature'),slot=r.get('slot'),block_time_s=r.get('blockTime')) for r in raw],dict(signature=_signature,slot=_int,block_time_s=_int))
            out=dict(state='partial' if len(raw)>=self.page_size else 'known',items=items,malformed_fields=bad,truncated=len(raw)>=self.page_size,cursor=raw[-1].get('signature') if raw else None,fetched_at=at)
        now=self.net.clock()
        if out.get('state') not in ('known','partial'):
            return out.get('reason') or 'history_unknown'
        items=out.get('items'); cursor=out.get('cursor')
        if not isinstance(items,list) or out.get('malformed_fields') or any(not isinstance(r,dict) or not isinstance(r.get('signature'),str) or type(r.get('slot')) is not int or r['slot']<=0 for r in items):
            return 'history_malformed'
        if out.get('truncated') and (not cursor or cursor==s['before_sig']):return 'history_cursor_stalled'
        head=s['head'] or (items[0]['signature'] if items else None)
        head_slot=s['head_slot'] or (items[0]['slot'] if items else None)
        if items and s['cursor_slot'] and items[0]['slot']<s['cursor_slot'] and not s['before_sig']:return 'cursor_regression'
        boundary=next((i for i,r in enumerate(items) if r['signature']==s['cursor']),None) if s['cursor'] else None
        selected=items[:boundary] if boundary is not None else items
        floor_reached=any(type(r.get('block_time_s')) is int and r['block_time_s']<s['forward_floor'] for r in selected)
        selected=[r for r in selected if type(r.get('block_time_s')) is not int or r['block_time_s']>=s['forward_floor']]
        exhausted=boundary is not None or floor_reached or not out.get('truncated')
        gap=exhausted and s['cursor'] is not None and boundary is None
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            why=self.net._refusal(conn,now,rows=len(selected)+1)
            if why:return why
            if not s['head']:
                conn.execute('UPDATE watch_collect_state SET interval_unknown=0,scan_asof=? WHERE address=?',(out.get('fetched_at',now),s['address']))
            for r in selected:
                conn.execute('INSERT OR IGNORE INTO watch_collect_pending(address,signature,slot,block_time) VALUES(?,?,?,?)',(s['address'],r['signature'],r['slot'],r.get('block_time_s')))
            conn.execute("UPDATE watch_collect_state SET head=?,head_slot=?,before_sig=?,exhausted=?,state='partial',reason=? WHERE address=?",(head,head_slot,cursor,2 if gap else int(exhausted),'cursor_not_found' if gap else 'backlog',s['address']))
            self.net._count(conn,now,rows=len(selected)+1)
            self._finish(conn,s['address'],now)
        return 'cursor_not_found' if gap else None

    def _proof(self, clue, now):
        # Small per-edge receipts avoid ChainData's 8192-byte receipt truncation.
        return self.chain._answer('watch_verified_funding',dict(signature=clue['funding_signature']),lambda:dict(state='known',asof=dict(kind='finalized_transaction',block_time_s=clue['funded_at']),**clue))

    def _detail(self, conn, address, p, now, cached=None):
        try:
            tx,was_cached,at=cached if cached is not None else self.chain.call('getTransaction',[p['signature'],dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')],ttl=86400)
        except _Refused as exc:return exc.reason
        except (OSError,ValueError,TypeError,sqlite3.Error):return 'transaction_read_unknown'
        now=self.net.clock()
        if not isinstance(tx,dict):return 'transaction_not_found'
        transaction=tx.get('transaction') or {}; meta=tx.get('meta')
        if not isinstance(meta,dict) or 'err' not in meta:return 'transaction_meta_unknown'
        if not isinstance(transaction,dict):return 'transaction_identity_unknown'
        signatures=transaction.get('signatures')
        if not isinstance(signatures,list) or not signatures or signatures[0]!=p['signature'] or tx.get('slot')!=p['slot']:return 'transaction_identity_mismatch'
        event_at=tx.get('blockTime')
        if type(event_at) is not int or not 0<event_at<=now:return 'transaction_clock_unknown'
        floor=conn.execute('SELECT forward_floor FROM watch_collect_state WHERE address=?',(address,)).fetchone()[0]
        if event_at<floor:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                why=self.net._refusal(conn,now,rows=1)
                if why:return why
                conn.execute('DELETE FROM watch_collect_pending WHERE address=? AND signature=?',(address,p['signature']))
                self.net._count(conn,now,rows=1)
                self._finish(conn,address,now)
            return None
        failed=meta['err'] is not None
        try:
            observed=[dict(asset='transaction:failed',delta_atomic=None,decimals=None,kind='failed_transaction',state='known',reason='transaction_failed')] if failed else movements(tx,address)
            clues=[] if failed else verified_sol_funding(tx,datetime.fromtimestamp(now,timezone.utc).isoformat())
        except (KeyError,TypeError,ValueError,IndexError,AttributeError):
            # Missing proof never erases a useful, explicitly partial balance observation.
            observed=[dict(asset='transaction:unknown',delta_atomic=None,decimals=None,kind='balance_observation',state='unknown',reason='receipt_malformed')]
            clues=[]
        clues=[c for c in clues if c.get('verified') is True and address in (c.get('funder'),c.get('recipient'))]
        receipts=[(c,self._proof(c,now)) for c in clues]
        out=self.chain._answer('watch_balance_observation',dict(address=address,signature=p['signature']),lambda:dict(state='known',address=address,signature=p['signature'],asof=dict(kind='finalized_transaction',block_time_s=event_at)))
        if not out.get('fetch_id'):return 'proof_store_unknown'
        if any(proof.get('state')!='known' or not self.net._fetch_proves(proof.get('fetch_id'),clue['recipient'],clue['funder']) for clue,proof in receipts):return 'proof_store_unknown'
        children=[]
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            why=self.net._refusal(conn,now,rows=len(observed)+len(receipts)+1)
            if why:return why
            if any(r['state']=='unknown' for r in observed):
                conn.execute('UPDATE watch_collect_state SET interval_unknown=1 WHERE address=?',(address,))
            for r in observed:
                conn.execute('INSERT OR IGNORE INTO watch_movements VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)',(address,p['signature'],r['asset'],r['delta_atomic'],r['decimals'],r['kind'],r['state'],r['reason'],p['slot'],event_at,now,out['fetch_id']))
            for clue,proof in receipts:
                ref=proof.get('fetch_id');src,dst=clue['recipient'],clue['funder']
                self.net._insert(conn,src=src,dst=dst,edge_type='funded_by',source='host',actor='wallet_watch_collect',observed_at=event_at,known_at=now,proof_kind='chain_fetch',proof_ref=ref,proof_state='verified',method=METHOD)
                if dst==address:children.append(src)
            conn.execute('DELETE FROM watch_collect_pending WHERE address=? AND signature=?',(address,p['signature']))
            conn.execute('UPDATE watch_collect_state SET last_event_at=MAX(COALESCE(last_event_at,0),?) WHERE address=?',(event_at,address))
            self.net._count(conn,now,rows=len(observed)+len(receipts)+1)
            self._finish(conn,address,now)
        return self._expand(conn,address,children,p['signature'],now,event_at=event_at)

    def _expand(self,conn,address,children,signature,now,*,event_at=None,_batch=False):
        if self.expand and children:
            children=[child for child in children if not conn.execute('SELECT 1 FROM watch_list WHERE address=?',(child,)).fetchone()]
            if not children:return None
            if _time(now) is None or _time(event_at) is None or event_at>now:return 'expansion_campaign_clock_unknown'
            infrastructure=W._known_infrastructure()
            if infrastructure is None:return 'expansion_infrastructure_unknown'
            try:
                from sniper.services.memecoin_desk_txshape import JITO_TIP_ACCOUNTS
                from sniper.services.memecoin_fee_reconciler import tip_accounts
                infrastructure=infrastructure|JITO_TIP_ACCOUNTS|tip_accounts()
            except (ImportError,TypeError,ValueError):return 'expansion_fee_infrastructure_unknown'
            for child in children:
                if conn.execute('SELECT 1 FROM watch_list WHERE address=?',(child,)).fetchone():continue # explicit removals stay removed
                labels={r[0].lower() for r in conn.execute('SELECT label FROM labels WHERE network_id=? AND address IN (?,?)',('solana',address,child))}
                fanout=conn.execute("SELECT count(DISTINCT src) FROM edges WHERE network_id='solana' AND type='funded_by' AND dst=? AND proof_state='verified'",(address,)).fetchone()[0]
                if labels&STOP_LABELS or {address,child}&infrastructure:continue
                campaign=dict(operator_id=f'sol60:{address}:{int(event_at//60)*60}',campaign_limit=-1)
                if fanout>=W.FANOUT_HUB:
                    if type(event_at) not in (int,float) or not math.isfinite(event_at) or not 0<event_at<=now:
                        return 'expansion_campaign_clock_unknown'
                    campaign=dict(operator_id=f'hub60:{address}:{int(event_at//60)*60}',campaign_limit=-1)
                if conn.execute('SELECT 1 FROM watch_list WHERE address=?',(child,)).fetchone():continue # explicit removals stay removed
                add,target=(W._add_on_connection,conn) if _batch else (W.add,self.desk_dir)
                result=add(self.net,target,address=child,evidence='verified funding '+signature,source='host',actor='wallet_watch_collect',now=now,**(campaign or dict(operator_id=None)))
                if result.get('state')!='added':return result.get('reason','watch_add_unknown')
        return None

    def _queue_expansion(self,conn,source,flow_id,signature,event_at,children,now):
        n=conn.execute('INSERT OR IGNORE INTO watch_flow_expansions(source,flow_id,signature,event_at,sol_json) VALUES(?,?,?,?,?)',
                       (source,flow_id,signature,event_at,json.dumps(children))).rowcount
        if n:self.net._count(conn,now,rows=1)

    def _expansion_progress(self,conn,source,flow_id,now,**fields):
        with conn:
            why=self.net._refusal(conn,now,rows=1)
            if why:return why
            columns=list(fields)+['last_try']
            conn.execute('UPDATE watch_flow_expansions SET '+','.join(c+'=?' if c!='last_try' else 'last_try=MAX(COALESCE(last_try,0),?)' for c in columns)+' WHERE source=? AND flow_id=?',
                         (*fields.values(),now,source,flow_id))
            self.net._count(conn,now,rows=1)
        return None

    def _run_expansion(self,conn,source,flow_id,now,deadline):
        task=conn.execute('SELECT * FROM watch_flow_expansions WHERE source=? AND flow_id=? AND state=?',(source,flow_id,'pending')).fetchone()
        if task is None:return None
        if not self.expand:return 'expansion_disabled'
        knowledge=conn.execute('SELECT r.known_at,t.graph_commit_before,t.graph_commit_after FROM watch_flow_receipts r JOIN watch_flow_timing t USING(source,flow_id) WHERE r.source=? AND r.flow_id=?',(source,flow_id)).fetchone()
        if _time(now) is None or knowledge is None or _time(knowledge[0]) is None or any(_time(t) is not None and t>now for t in knowledge):
            return 'expansion_clock_unknown'
        children=json.loads(task['sol_json']);cursor=task['sol_cursor']
        why=None
        if cursor<len(children):
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                for _ in range(20):
                    if cursor>=len(children) or time.monotonic()>deadline:break
                    sender,child=children[cursor]
                    why=self._expand(conn,sender,[child],task['signature'],now,event_at=task['event_at'],_batch=True)
                    if why:break
                    cursor+=1
            W._sync(conn,self.desk_dir,now)
        progress=self._expansion_progress(conn,source,flow_id,now,sol_cursor=cursor,reason=why)
        if progress or why:return progress or why
        if cursor<len(children):return 'expansion_work_deferred'
        why=self._expand_token(conn,source,flow_id,task['signature'],now,deadline=deadline)
        return why

    def _resume_expansions(self,conn,deadline):
        # Bootstrap old token rows in bounded rowid pages; upgrade never drops already observed trails.
        old=conn.execute("SELECT cursor FROM watch_collect_sources WHERE source='token_expansion_seed'").fetchone()
        cursor=old[0] if old else 0
        page=conn.execute('SELECT rowid AS rid,source,flow_id,signature FROM watch_token_transfers WHERE rowid>? ORDER BY rowid LIMIT 100',(cursor,)).fetchall()
        if page:
            why=self.net._refusal(conn,self.net.clock(),rows=len(page)+1)
            if why:return why
            with conn:
                for t in page:
                    if t['source'] is None or t['flow_id'] is None:continue
                    timing=conn.execute('SELECT event_at FROM watch_flow_timing WHERE source=? AND flow_id=? AND signature=? AND flow_state=?',(t['source'],t['flow_id'],t['signature'],'known')).fetchone()
                    if timing:self._queue_expansion(conn,t['source'],t['flow_id'],t['signature'],timing[0],[],self.net.clock())
                conn.execute("INSERT INTO watch_collect_sources VALUES('token_expansion_seed',?,?,'partial','available_token_rows_only') ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read",(page[-1]['rid'],self.net.clock()))
                self.net._count(conn,self.net.clock(),rows=1)
        tasks=conn.execute("SELECT source,flow_id FROM watch_flow_expansions WHERE state='pending' ORDER BY last_try,source,flow_id LIMIT 20").fetchall()
        for task in tasks:
            if time.monotonic()>deadline:break
            self._run_expansion(conn,task['source'],task['flow_id'],self.net.clock(),deadline)

    def _expand_token(self,conn,source,flow_id,signature,now,*,deadline=None):
        """Fresh supported token transfers can watch recipients; this creates no funding/control edge."""
        if not self.expand:return None
        if conn.in_transaction:return 'token_expansion_commit_unknown'
        if _time(now) is None:return 'token_expansion_clock_unknown'
        try:
            receipt=conn.execute('SELECT signature,state FROM watch_flow_receipts WHERE source=? AND flow_id=?',(source,flow_id)).fetchone()
            if receipt is None or tuple(receipt)!=(signature,'known'):return 'token_expansion_receipt_unknown'
            task=conn.execute('SELECT token_outer,token_inner,token_done FROM watch_flow_expansions WHERE source=? AND flow_id=?',(source,flow_id)).fetchone()
            if task is not None and task['token_done']:return None
            position=(task['token_outer'],task['token_inner']) if task else (-1,-2)
            candidates=conn.execute('SELECT * FROM watch_token_transfers WHERE signature=? AND source=? AND flow_id=? AND (outer_ix,inner_ix)>(?,?) ORDER BY outer_ix,inner_ix LIMIT 101',
                                    (signature,source,flow_id,*position)).fetchall()
            if not candidates:
                if task:self._expansion_progress(conn,source,flow_id,now,token_done=1,state='done')
                return None
            timing=conn.execute('SELECT event_at FROM watch_flow_timing WHERE source=? AND flow_id=? AND signature=? AND flow_state=?',(source,flow_id,signature,'known')).fetchone()
            event_at=timing[0] if timing else None
            if _time(event_at) is None or event_at>now:return 'token_expansion_clock_unknown'
            infrastructure=W._known_infrastructure()
            if infrastructure is None:return 'token_expansion_infrastructure_unknown'
            try:
                from sniper.services.memecoin_desk_txshape import JITO_TIP_ACCOUNTS
                from sniper.services.memecoin_fee_reconciler import tip_accounts
                from sniper.office.wallet_family import PROGRAMS
                infrastructure=infrastructure|JITO_TIP_ACCOUNTS|tip_accounts()|PROGRAMS|{
                    TF.TOKEN,TF.TOKEN22,'11111111111111111111111111111111',
                    'ComputeBudget111111111111111111111111111111',
                    'Vote111111111111111111111111111111111111111',
                    'AddressLookupTab1e1111111111111111111111111'}
            except (ImportError,TypeError,ValueError):return 'token_expansion_fee_infrastructure_unknown'
            from solders.pubkey import Pubkey
            added=0;seen=set();proofs={};reason=None;processed=position;deferred=False
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                for t in candidates[:100]:
                    if added>=20 or deadline is not None and time.monotonic()>deadline:
                        deferred=True;break
                    previous=processed;processed=(t['outer_ix'],t['inner_ix'])
                    intent=t['program']==TF.TOKEN22 and t['state']=='unknown' and t['reason']=='token2022_fee_unknown'
                    if not (t['state']=='known' or intent) or t['program'] not in (TF.TOKEN,TF.TOKEN22):
                        reason='token_expansion_transfer_unknown';continue
                    sender,child=t['source_owner'],t['destination_owner']
                    try:
                        if not sender or not child or not Pubkey.from_string(child).is_on_curve():
                            reason='token_expansion_owner_unknown';continue
                        amount=TF._amount(t['amount_atomic'])
                        if intent:
                            if int(amount)<=0 or t['credit_atomic'] is not None or t['fee_atomic'] is not None:
                                reason='token_expansion_credit_unknown';continue
                            fee=None
                        else:credit,fee=TF._amount(t['credit_atomic']),TF._amount(t['fee_atomic'])
                        if not intent and (int(fee)>int(amount) or int(credit)<=0 or int(credit)!=int(amount)-int(fee) or t['program']==TF.TOKEN and int(fee)!=0):
                            reason='token_expansion_credit_unknown';continue
                        ix=json.loads(t['instruction_json']);parsed=ix['parsed'];info=parsed['info']
                        kinds=('transfer','transferChecked') if t['program']==TF.TOKEN or intent else ('transferCheckedWithFee',)
                        if (ix['programId']!=t['program'] or parsed['type'] not in kinds
                                or info['source']!=t['source_account'] or info['destination']!=t['destination_account']
                                or (info['amount'] if parsed['type']=='transfer' else info['tokenAmount']['amount'])!=amount):
                            reason='token_expansion_instruction_unknown';continue
                        if parsed['type']!='transfer' and (info['mint']!=t['mint'] or info['tokenAmount']['decimals']!=t['decimals']):
                            reason='token_expansion_instruction_unknown';continue
                        if t['program']==TF.TOKEN22 and not intent and (info['feeAmount']['amount']!=fee or info['feeAmount']['decimals']!=t['decimals']):
                            reason='token_expansion_instruction_unknown';continue
                    except (ValueError,TypeError,KeyError):
                        reason='token_expansion_transfer_unknown';continue
                    if sender==child or child in seen:continue
                    if conn.execute('SELECT 1 FROM watch_list WHERE address=?',(child,)).fetchone():continue # removals stay removed
                    owner=conn.execute('SELECT added_at FROM watch_list WHERE address=? AND removed_at IS NULL',(sender,)).fetchone()
                    if owner is None:reason='token_expansion_source_not_watched';continue
                    if _time(t['known_at']) is None or t['known_at']>now or owner[0]>t['known_at']:
                        reason='token_expansion_clock_unknown';continue
                    labels={r[0].lower() for r in conn.execute('SELECT label FROM labels WHERE network_id=? AND address IN (?,?)',('solana',sender,child))}
                    if labels&STOP_LABELS or {sender,child}&infrastructure:continue
                    asked=dict(source=source,flow_id=flow_id,signature=signature,address=sender)
                    ref='stream-'+hashlib.sha256(json.dumps(['watch_stream_balance',asked],sort_keys=True,allow_nan=False).encode()).hexdigest()
                    key=(sender,ref)
                    if t['fetch_id']!=ref:reason='token_expansion_source_proof_unknown';continue
                    if key not in proofs:proofs[key]=self.net._fetch_proves(ref,sender,signature)
                    if not proofs[key]:reason='token_expansion_source_proof_unknown';continue
                    result=W._add_on_connection(self.net,conn,address=child,operator_id=f'token60:{sender}:{int(event_at//60)*60}',
                        campaign_limit=-1,evidence=('observed_token_transfer_intent gross/net_unknown ' if intent else 'observed_token_transfer ')+signature,source='host',actor='wallet_watch_collect',now=now)
                    if result.get('state')!='added':
                        # Keep the unprocessed candidate for a later turn, including transient configured caps.
                        processed=previous;reason=result.get('reason','token_expansion_watch_unknown');deferred=True;break
                    added+=1;seen.add(child)
            W._sync(conn,self.desk_dir,now)
            if task:
                done=not deferred and len(candidates)<=100
                self._expansion_progress(conn,source,flow_id,now,token_outer=processed[0],token_inner=processed[1],token_done=int(done),state='done' if done else 'pending',reason=reason)
            return reason or ('token_expansion_work_deferred' if deferred or len(candidates)>100 else None)
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
            return 'token_expansion_unknown'

    def _stream_row(self,conn,row,source,now):
        clock_floor=now if type(now) in (int,float) and math.isfinite(now) and now>0 else 0
        if not clock_floor:return 'pending','stream_clock_unknown',[],clock_floor,None,None
        from google.protobuf.message import DecodeError
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        from sniper.office.wallet_stream_proof import confirmed_receipt,decode_payload
        try:
            if (row['commitment']!='confirmed' or not isinstance(row['signature'],str) or not SIGNATURE.fullmatch(row['signature'])
                    or type(row['slot']) is not int or row['slot']<=0
                    or any(type(row[k]) not in (int,float) or not math.isfinite(row[k]) for k in ('received_at','processed_at'))
                    or not 0<row['received_at']<=row['processed_at']):
                return 'unknown','stream_envelope_unproven',[],clock_floor,None,None
            if row['processed_at']>now:return 'pending','stream_clock_future',[],clock_floor,None,None
            if not isinstance(row['payload_json'],str) or len(row['payload_json'].encode())>256*1024:
                return 'unknown','stream_payload_oversize',[],clock_floor,None,None
            raw=decode_payload(row['payload_json'],archive_dir=Path(source).parent/'wallet-flow-archive')
            update=pb.SubscribeUpdate();update.ParseFromString(raw)
            tx=confirmed_receipt(update,commitment='confirmed',block_time=row['block_time'])
            if (tx['slot']!=row['slot'] or tx['transaction']['signatures'][0]!=row['signature']
                    or tx['transactionIndex']!=row['tx_index']):
                return 'unknown','stream_identity_mismatch',[],clock_floor,None,None
            event_at=tx['blockTime']
            if event_at is None:return 'pending','block_time_pending',[],clock_floor,None,None
            if event_at>now:return 'pending','stream_clock_future',[],clock_floor,None,None
            clues=verified_sol_funding(tx,datetime.fromtimestamp(now,timezone.utc).isoformat())
        except (ValueError,TypeError,KeyError,IndexError,AttributeError,DecodeError):
            return 'unknown','stream_receipt_unproven',[],clock_floor,None,None
        observed_clock=self.net.clock()
        if type(observed_clock) not in (int,float) or not math.isfinite(observed_clock) or observed_clock<clock_floor:
            return 'pending','stream_clock_regressed',[],clock_floor,None,None
        now=clock_floor=observed_clock
        keys={k['pubkey'] for k in tx['transaction']['message']['accountKeys']}
        token_rows=tx['meta']['preTokenBalanceObservations']+tx['meta']['postTokenBalanceObservations']
        owner_candidates={r['owner'] for r in token_rows}
        candidates=sorted(keys|owner_candidates)
        marks=','.join('?' for _ in candidates)
        watched={r[0] for r in conn.execute('SELECT address FROM watch_list WHERE removed_at IS NULL AND address IN ('+marks+')',candidates)}
        # Account aliases observe ownership, not signing authority. Include latest ownership changes too.
        token_keys=[tx['transaction']['message']['accountKeys'][r['accountIndex']]['pubkey'] for r in token_rows]
        context=set()
        for account in set(token_keys):
            prior=conn.execute('SELECT 1 FROM watch_token_accounts a JOIN watch_list w ON w.address=a.owner AND w.removed_at IS NULL WHERE a.account=? LIMIT 1',(account,)).fetchone()
            if prior:context.update(r['owner'] for r in token_rows if tx['transaction']['message']['accountKeys'][r['accountIndex']]['pubkey']==account)
        actors=sorted(watched|context)
        clues=[c for c in clues if watched.intersection((c['recipient'],c['funder']))]
        observed=[(a,m) for a in actors for m in movements(tx,a)]
        from sniper.office.wallet_launch_proof import launch_facts
        launch_result=launch_facts(tx,commitment='confirmed')
        funded_children={c['recipient'] for c in clues if c['funder'] in watched}
        launches=[f for f in launch_result['items'] if f['developer'] in watched|funded_children]
        token_evidence=TF.token_flow(tx)
        token_accounts,token_transfers=TF.scoped_rows(token_evidence,actors)
        post_by_account={tx['transaction']['message']['accountKeys'][r['accountIndex']]['pubkey']:r for r in tx['meta']['postTokenBalanceObservations']}
        token_accounts=[dict(a) for a in token_accounts]
        for a in token_accounts:
            if a['reason']=='token_identity_changed' and a['account'] in post_by_account:
                post=post_by_account[a['account']]
                a.update(owner=post['owner'],mint=post['mint'],program=post['programId'],decimals=post['uiTokenAmount']['decimals'],
                         pre_atomic=None,post_atomic=post['uiTokenAmount']['amount'],delta_atomic=None,state='unknown',reason='token_balance_side_missing')
        # Reserve both timing INSERT and its bounded post-commit witness UPDATE.
        charged=len(observed)+len(clues)+len(launches)+5+len(token_accounts)+len(token_transfers)
        why=self.net._refusal(conn,now,rows=charged)
        if why:return 'cap',why,[],clock_floor,None,None
        asked=dict(source=source,flow_id=row['flow_id'],signature=row['signature'])
        receipts=[];proof_times=[]
        for clue in clues:
            proof=self.chain.record_stream_fact('watch_stream_funding',dict(asked,funder=clue['funder'],recipient=clue['recipient']),
                dict(state='known',asof=dict(kind='confirmed_transaction',block_time_s=event_at),**clue),commitment='confirmed')
            if proof.get('state')!='known' or not self.net._fetch_proves(proof.get('fetch_id'),clue['recipient'],clue['funder']):
                return 'pending',proof.get('reason','stream_proof_store_unknown'),[],clock_floor,None,None
            receipts.append((clue,proof));proof_times.append(proof.get('proof_stored_at'))
        launch_receipts=[]
        for fact in launches:
            proof=self.chain.record_stream_fact('watch_stream_launch',dict(asked,mint=fact['mint'],developer=fact['developer'],outer_ix=fact['outer_ix']),
                dict(state='known',asof=dict(kind='confirmed_transaction',block_time_s=event_at),**fact),commitment='confirmed')
            if proof.get('state')!='known' or not self.net._fetch_proves(proof.get('fetch_id'),fact['developer'],fact['mint']):
                return 'pending',proof.get('reason','stream_proof_store_unknown'),[],clock_floor,None,None
            launch_receipts.append((fact,proof));proof_times.append(proof.get('proof_stored_at'))
        observation_refs={}
        for address in actors:
            proof=self.chain.record_stream_fact('watch_stream_balance',dict(asked,address=address),
                dict(state='known',address=address,signature=row['signature'],asof=dict(kind='confirmed_transaction',block_time_s=event_at)),commitment='confirmed')
            if not proof.get('fetch_id'):return 'pending',proof.get('reason','stream_proof_store_unknown'),[],clock_floor,None,None
            observation_refs[address]=proof['fetch_id'];proof_times.append(proof.get('proof_stored_at'))
        if any(type(t) not in (int,float) or not math.isfinite(t) or t<=0 for t in proof_times):
            return 'pending','stream_proof_clock_unknown',[],clock_floor,None,None
        clock_floor=max([clock_floor]+proof_times)
        observed_clock=self.net.clock()
        if type(observed_clock) not in (int,float) or not math.isfinite(observed_clock) or observed_clock<clock_floor:
            return 'pending','stream_clock_regressed',[],clock_floor,None,None
        now=clock_floor=observed_clock
        why=self.net._refusal(conn,now,rows=charged)
        if why:return 'cap',why,[],clock_floor,None,None
        TF.persist(conn,signature=row['signature'],accounts=token_accounts,transfers=token_transfers,proof_refs=observation_refs,source=source,flow_id=row['flow_id'],known_at=now)
        expansions=[]
        for address,m in observed:
            conn.execute('INSERT OR IGNORE INTO watch_movements VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)',
                (address,row['signature'],m['asset'],m['delta_atomic'],m['decimals'],m['kind'],m['state'],m['reason'],row['slot'],event_at,now,observation_refs[address]))
        for clue,proof in receipts:
            self.net._insert(conn,src=clue['recipient'],dst=clue['funder'],edge_type='funded_by',source='host',actor='wallet_watch_collect',
                observed_at=event_at,known_at=now,proof_kind='research_stream',proof_ref=proof['fetch_id'],proof_state='verified',method='wallet-watch-stream-v1')
            if clue['funder'] in watched:expansions.append((clue['funder'],clue['recipient']))
        for fact,proof in launch_receipts:
            self.net._insert(conn,src=fact['developer'],dst=fact['mint'],edge_type='created_mint',source='host',actor='wallet_watch_collect',
                observed_at=event_at,known_at=now,proof_kind='research_stream',proof_ref=proof['fetch_id'],proof_state='verified',method='wallet-watch-launch-v1')
        self.net._count(conn,now,rows=charged-1)
        return 'known',launch_result.get('reason'),expansions,clock_floor,max(proof_times,default=None),(token_evidence['state'],token_evidence['reason'])



    def _recover_timings(self,conn,source,now):
        """Re-observe durable known rows; NOW is a new upper bound, never the old commit instant."""
        if _time(now) is None:return False
        floor=conn.execute('SELECT last_read FROM watch_flow_state WHERE source=?',(source,)).fetchone()
        if floor is None:return True
        upper=conn.execute('SELECT max(graph_commit_after) FROM watch_flow_timing WHERE source=?',(source,)).fetchone()[0]
        if _time(floor[0]) is None or floor[0]>now or upper is not None and (_time(upper) is None or upper>now):return False
        pending=conn.execute("""SELECT t.flow_id,t.graph_commit_before,t.proof_stored_at,r.known_at
            FROM watch_flow_timing t JOIN watch_flow_receipts r ON r.source=t.source AND r.flow_id=t.flow_id AND r.signature=t.signature
            WHERE t.source=? AND t.graph_commit_after IS NULL AND t.flow_state='known' AND r.state='known'
            AND t.graph_commit_before>0 AND t.graph_commit_before<=? AND t.proof_stored_at>0 AND t.proof_stored_at<=? AND r.known_at>0 AND r.known_at<=?
            ORDER BY t.flow_id LIMIT 20""",(source,now,now,now)).fetchall()
        witnesses=[(source,r[0],r[1],now) for r in pending if all(_time(t) is not None for t in r[1:]) and r[2]<=r[1]]
        return self._finish_timings(conn,witnesses)  # Its UPDATE was reserved in the original four base charges.

    def _finish_timings(self, conn, witnesses):
        # A second batch commit records observed completion bounds, never a fabricated commit instant.
        try:
            with conn:
                for source,flow_id,before,after in witnesses:
                    if before is not None and after is not None and after>=before:
                        conn.execute('UPDATE watch_flow_timing SET graph_commit_after=? WHERE source=? AND flow_id=? AND graph_commit_before=?',
                                     (after,source,flow_id,before))
                if self.net._size()>=self.net.max_bytes or conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>=self.net.max_bytes:
                    raise ValueError('stream_timing_store_cap')
            return True
        except (sqlite3.Error,ValueError):
            conn.rollback()
            return False

    def consume_flows(self, *, limit=20):
        """Read the existing confirmed tape only; pending block facts never hold its head cursor."""
        if type(limit) is not int or not 1<=limit<=499:raise ValueError('bad_flow_limit')
        stage=time.monotonic();phases=dict(decode_store=0.,expansion=0.)
        try:conn=self._open()
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='flow_store_unknown',rows=0,unknown=0)
        phases['open']=time.monotonic()-stage;stage=time.monotonic()
        tape=None;now=self.net.clock();deadline=time.monotonic()+1
        path=(Path(self.desk_dir)/'pumpswap/tape.db').resolve();source=str(path)
        counts=dict(rows=0,unknown=0);cursor=0
        try:
            why=self.net._refusal(conn,now)
            if why:return dict(state='unknown',reason=why,**counts)
            self._recover_timings(conn,source,now)
            old=conn.execute('SELECT * FROM watch_flow_state WHERE source=?',(source,)).fetchone()
            try:
                stat=path.stat();identity=str(stat.st_dev)+':'+str(stat.st_ino)
                if old and old['identity']!=identity:return dict(state='unknown',reason='flow_source_replaced',**counts)
                tape=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0);tape.row_factory=sqlite3.Row
                budget=[5000]
                def progress():
                    budget[0]-=1
                    return budget[0]<0 or time.monotonic()>deadline
                tape.set_progress_handler(progress,1000)
                cursor=old['cursor'] if old else 0
                if old and old['anchor'] is not None:
                    anchor=tape.execute('SELECT signature,slot,received_at FROM wallet_flow_transactions WHERE flow_id=?',(cursor,)).fetchone()
                    if anchor is not None and json.dumps(list(anchor))!=old['anchor']:
                        return dict(state='unknown',reason='flow_source_replaced',**counts)
                maximum=tape.execute('SELECT flow_id FROM wallet_flow_transactions ORDER BY flow_id DESC LIMIT 1').fetchone()
                if old and maximum and maximum[0]<cursor:return dict(state='unknown',reason='flow_source_replaced',**counts)
                try:
                    gaps=tape.execute('SELECT * FROM wallet_flow_gaps WHERE known_at<=? ORDER BY day DESC LIMIT 21',(now,)).fetchall()
                    stream_gaps=dict(state='known',items=[dict(g) for g in gaps[:20]],truncated=len(gaps)>20)
                except sqlite3.Error:stream_gaps=dict(state='unknown',reason='flow_gap_coverage_unknown',items=[])
                new=tape.execute('SELECT * FROM wallet_flow_transactions WHERE flow_id>? ORDER BY flow_id LIMIT ?',(cursor,limit)).fetchall()
                pending=conn.execute('SELECT flow_id FROM watch_flow_pending WHERE source=? ORDER BY last_try,flow_id LIMIT ?',(source,limit)).fetchall()
                retry=[]
                for p in pending:
                    r=tape.execute('SELECT * FROM wallet_flow_transactions WHERE flow_id=?',(p[0],)).fetchone()
                    if r is not None:retry.append(r)
                    else:
                        with conn:
                            conn.execute('INSERT OR IGNORE INTO watch_flow_receipts VALUES(?,?,NULL,?,?,?)',(source,p[0],'unknown','stream_row_pruned',now))
                            conn.execute('DELETE FROM watch_flow_pending WHERE source=? AND flow_id=?',(source,p[0]))
                        counts['unknown']+=1
            except (sqlite3.Error,OSError):return dict(state='unknown',reason='flow_tape_unknown',**counts)
            reason='available_stream_only';witnesses=[]
            phases['read']=time.monotonic()-stage;stage=time.monotonic()
            self._resume_expansions(conn,deadline)
            phases['resume_expansions']=time.monotonic()-stage
            selectors=selector_snapshot(Path(self.desk_dir)/'universe.heartbeat',now)
            turn_source='flow_retry_turn:'+source
            turn=conn.execute('SELECT cursor FROM watch_collect_sources WHERE source=?',(turn_source,)).fetchone()
            retry_first=turn is None or turn[0]==0
            lanes=(retry,list(new)) if retry_first else (list(new),retry)
            work=[];seen=set();retry_ids={r['flow_id'] for r in retry}
            for index in range(max(len(new),len(retry))):
                for lane in lanes:
                    if index<len(lane) and lane[index]['flow_id'] not in seen:
                        work.append(lane[index]);seen.add(lane[index]['flow_id'])
            fair_turn=bool(new and retry)
            if self.expand and conn.execute("SELECT 1 FROM watch_flow_expansions WHERE state='pending' LIMIT 1").fetchone():
                reason='expansion_work_deferred';work=[]
            for row in work:
                if time.monotonic()>deadline:
                    reason='stream_read_budget';break
                row_started=time.monotonic()
                now=self.net.clock()
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if conn.execute('SELECT 1 FROM watch_flow_receipts WHERE source=? AND flow_id=?',(source,row['flow_id'])).fetchone():continue
                    why=self.net._refusal(conn,now,rows=2+int(fair_turn))
                    if why:
                        reason=why;break
                    attempt_floor=now
                    previous=conn.execute('SELECT last_try FROM watch_flow_pending WHERE source=? AND flow_id=?',(source,row['flow_id'])).fetchone()
                    global_floor=conn.execute('SELECT last_read FROM watch_flow_state WHERE source=?',(source,)).fetchone()
                    for prior in (previous,global_floor):
                        if prior is not None and type(prior[0]) in (int,float) and math.isfinite(prior[0]):attempt_floor=max(attempt_floor,prior[0])
                    proof_stored_at=None;token_status=None
                    if now<attempt_floor:state,why,children='pending','stream_clock_regressed',[]
                    else:
                        state,why,children,row_floor,proof_stored_at,token_status=self._stream_row(conn,row,source,now)
                        attempt_floor=max(attempt_floor,row_floor)
                    now=self.net.clock()
                    if why=='stream_proof_store_cap':reason=why
                    stop_after_row=state=='cap'
                    if stop_after_row:
                        reason=why;state='pending'
                    if state=='pending':
                        conn.execute('INSERT INTO watch_flow_pending VALUES(?,?,?,?,?) ON CONFLICT(source,flow_id) DO UPDATE SET last_try=MAX(COALESCE(watch_flow_pending.last_try,0),excluded.last_try),reason=excluded.reason',(source,row['flow_id'],max(now,attempt_floor),attempt_floor,why))
                    else:
                        conn.execute('INSERT INTO watch_flow_receipts VALUES(?,?,?,?,?,?)',(source,row['flow_id'],row['signature'],state,why,max(now,attempt_floor)))
                        conn.execute('DELETE FROM watch_flow_pending WHERE source=? AND flow_id=?',(source,row['flow_id']))
                    if state=='known' and (children or conn.execute('SELECT 1 FROM watch_token_transfers WHERE signature=? AND source=? AND flow_id=? LIMIT 1',(row['signature'],source,row['flow_id'])).fetchone()):
                        self._queue_expansion(conn,source,row['flow_id'],row['signature'],row['block_time'],children,now)
                    timing_allowed=state=='known' or self.net._refusal(conn,now,rows=4) is None
                    if state!='known':self.net._count(conn,now,rows=4 if timing_allowed else 2)
                    if fair_turn:
                        conn.execute("INSERT INTO watch_collect_sources VALUES(?,?,?,'partial','fair_stream_retry') ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read",
                                     (turn_source,int(row['flow_id'] in retry_ids),max(now,attempt_floor)))
                        self.net._count(conn,now,rows=1)
                    cursor=max(cursor,row['flow_id'])
                    anchor=tape.execute('SELECT signature,slot,received_at FROM wallet_flow_transactions WHERE flow_id=?',(cursor,)).fetchone()
                    conn.execute('INSERT INTO watch_flow_state VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=MAX(COALESCE(watch_flow_state.last_read,0),excluded.last_read),anchor=excluded.anchor',(source,identity,cursor,max(now,attempt_floor),json.dumps(list(anchor)) if anchor else None))
                    before=_time(self.net.clock())
                    if before is not None and before<attempt_floor:before=None
                    received,processed=_time(row['received_at']),_time(row['processed_at'])
                    if received is not None and (before is None or received>before):received=None
                    if processed is not None and (before is None or processed>before or received is not None and processed<received):processed=None
                    if timing_allowed:
                        conn.execute('INSERT INTO watch_flow_timing VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,flow_id) DO UPDATE SET proof_stored_at=excluded.proof_stored_at,graph_commit_before=excluded.graph_commit_before,graph_commit_after=NULL,selector_json=excluded.selector_json,gaps_json=excluded.gaps_json,flow_state=excluded.flow_state,flow_reason=excluded.flow_reason,token_state=excluded.token_state,token_reason=excluded.token_reason',
                                     (source,row['flow_id'],row['signature'],received,processed,proof_stored_at,before,None,json.dumps(selectors),json.dumps(stream_gaps),state,why,row['slot'],row['block_time'],*(token_status or ('unknown','flow_not_decoded'))))
                    if self.net._size()>=self.net.max_bytes or conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]>=self.net.max_bytes:
                        raise ValueError('stream_disk_cap')
                phases['decode_store']+=time.monotonic()-row_started
                if timing_allowed:witnesses.append((source,row['flow_id'],before,_time(self.net.clock())))
                counts['rows']+=1;counts['unknown']+=state=='unknown'
                if state=='known':
                    stage=time.monotonic()
                    expansion_why=self._run_expansion(conn,source,row['flow_id'],self.net.clock(),deadline)
                    phases['expansion']+=time.monotonic()-stage
                    if expansion_why:reason=expansion_why
                    if self.expand and conn.execute("SELECT 1 FROM watch_flow_expansions WHERE source=? AND flow_id=? AND state='pending'",(source,row['flow_id'])).fetchone():
                        reason=expansion_why or 'expansion_work_deferred';break
                if stop_after_row:break
            stage=time.monotonic()
            export=dict(state='unknown',reason='stream_timing_store_unknown')
            if not self._finish_timings(conn,witnesses):reason='stream_timing_store_unknown'
            else:
                try:
                    exported_at=self.net.clock()
                    phases['timing_witness']=time.monotonic()-stage;stage=time.monotonic()
                    W._sync(conn,self.desk_dir,exported_at)
                    phases['export']=time.monotonic()-stage
                    export=dict(state='known',asof=exported_at)
                except (sqlite3.Error,OSError,ValueError):reason='watch_export_unknown';export=dict(state='unknown',reason=reason)
            n=conn.execute('SELECT count(*) FROM watch_flow_pending WHERE source=?',(source,)).fetchone()[0]
            pending_expansions=[dict(r) for r in conn.execute("SELECT source,flow_id,last_try,reason FROM watch_flow_expansions WHERE state='pending' ORDER BY last_try,source,flow_id LIMIT 21")]
            expansion_status=dict(items=pending_expansions[:20],truncated=len(pending_expansions)>20,count_lower_bound=len(pending_expansions))
            return dict(state='partial',reason=reason,cursor=cursor,raw_frontier=maximum[0] if maximum else 0,raw_caught_up=cursor>=(maximum[0] if maximum else 0),pending=n,expansion_pending=expansion_status,stream_gaps=stream_gaps,watch_export=export,phase_s=phases,coverage='watched confirmed frames only; missing rows are not absence',**counts)
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:
            conn.rollback()
            return dict(state='unknown',reason='disk_cap' if str(exc)=='stream_disk_cap' else 'stream_consume_unknown',cursor=cursor,**counts)
        finally:
            if tape is not None:tape.close()
            conn.close()

    def consume_setups(self, *, limit=50):
        """Only existing tape facts; sponsor != funder, mint creator or common control."""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('bad_setup_limit')
        try:conn=self._open()
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='setup_store_unknown',added=0)
        tape=None; added=0; now=self.net.clock()
        try:
            why=self.net._refusal(conn,now)
            if why:return dict(state='unknown',reason=why,added=0)
            old=conn.execute("SELECT * FROM watch_collect_sources WHERE source='setup'").fetchone()
            cursor=old['cursor'] if old else 0
            try:
                tape=sqlite3.connect((Path(self.desk_dir)/'pumpswap/tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
                tape.row_factory=sqlite3.Row
                rows=tape.execute('SELECT * FROM wallet_setup_events WHERE setup_id>? ORDER BY setup_id LIMIT ?',(cursor,limit)).fetchall()
                maximum=tape.execute('SELECT setup_id FROM wallet_setup_events ORDER BY setup_id DESC LIMIT 1').fetchone()
            except (sqlite3.Error,OSError):return dict(state='unknown',reason='setup_tape_unknown',added=0)
            if maximum and maximum[0]<cursor:return dict(state='unknown',reason='setup_source_replaced',added=0)
            retention_gap=bool(old and old['last_read'] is not None and now-old['last_read']>30*86400)
            for r in rows:
                payer,wallet=r['payer'],r['wallet']
                received,processed=r['received_at'],r['processed_at']
                valid=(isinstance(r['signature'],str) and SIGNATURE.fullmatch(r['signature']) and type(r['slot']) is int and r['slot']>0 and type(r['outer_ix']) is int and r['outer_ix']>=0 and isinstance(payer,str) and W.ADDRESS.fullmatch(payer) and isinstance(wallet,str) and W.ADDRESS.fullmatch(wallet) and wallet!=payer
                    and type(received) in (int,float) and type(processed) in (int,float) and math.isfinite(received) and math.isfinite(processed) and 0<received<=processed<=now)
                if not valid:return dict(state='unknown',reason='setup_fact_malformed',added=added)
                watched=conn.execute('SELECT 1 FROM watch_list WHERE address=? AND removed_at IS NULL',(payer,)).fetchone()
                exists=conn.execute('SELECT 1 FROM watch_list WHERE address=?',(wallet,)).fetchone()
                labels={r[0].lower() for r in conn.execute('SELECT label FROM labels WHERE network_id=? AND address IN (?,?)',('solana',payer,wallet))}
                if watched and not exists and not labels&STOP_LABELS:
                    result=W.add(self.net,self.desk_dir,address=wallet,operator_id=None,evidence='setup_sponsor:'+payer+' '+r['signature'],source='host',actor='wallet_watch_collect',now=now)
                    if result.get('state')!='added':return dict(state='unknown',reason=result.get('reason','watch_add_unknown'),added=added)
                    added+=1
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    why=self.net._refusal(conn,now,rows=1)
                    if why:return dict(state='unknown',reason=why,added=added)
                    conn.execute("INSERT INTO watch_collect_sources VALUES('setup',?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read,state=excluded.state,reason=excluded.reason",(r['setup_id'],now,'partial','retention_gap_possible' if retention_gap else 'available_tape_only'))
                    self.net._count(conn,now,rows=1)
            # Preserve last_read on empty polls: it is the last source event, not a coverage claim.
            return dict(state='partial',reason='retention_gap_possible' if retention_gap else 'available_tape_only',added=added,rows=len(rows),cursor=rows[-1]['setup_id'] if rows else cursor)
        except (OSError,sqlite3.Error):
            conn.rollback()
            return dict(state='unknown',reason='setup_store_unknown',added=added)
        finally:
            if tape is not None:tape.close()
            conn.close()

    def tick(self, *, max_calls=10):
        if type(max_calls) is not int or not 1<=max_calls<=100:raise ValueError('bad_call_budget')
        try:conn=self._open()
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='collect_store_unknown',calls_attempted=0)
        now=self.net.clock(); attempted=0; cached_count=0
        try:
            why=self.net._refusal(conn,now)
            if why:return dict(state='unknown',reason=why,calls_attempted=0)
            with conn:
                scan=conn.execute("SELECT cursor FROM watch_collect_sources WHERE source='activation_scan'").fetchone()
                page=conn.execute('SELECT rowid,address,added_at,removed_at FROM watch_list WHERE rowid>? ORDER BY rowid LIMIT 100',(scan[0] if scan else 0,)).fetchall()
                floor=conn.execute("SELECT last_read FROM watch_collect_sources WHERE source='activation'").fetchone()[0]
                for _,address,added,removed in page:
                    if removed is None:conn.execute('INSERT OR IGNORE INTO watch_collect_state(address,forward_floor) VALUES(?,?)',(address,int(max(added,floor))))
                conn.execute("INSERT INTO watch_collect_sources VALUES('activation_scan',?,?,'partial','resumable_watch_activation') ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read",(page[-1][0] if page else 0,now))
                conn.execute('DELETE FROM watch_movements WHERE known_at<?',(now-OTHER_DAYS*86400,))
            blocked=None
            states=conn.execute('SELECT s.* FROM watch_collect_state s JOIN watch_list w USING(address) WHERE w.removed_at IS NULL ORDER BY s.last_turn,s.address LIMIT ?', (100,)).fetchall()
            for s in states:
                if attempted>=max_calls:break
                now=self.net.clock()
                p=conn.execute('SELECT * FROM watch_collect_pending WHERE address=? ORDER BY attempts,slot DESC,signature LIMIT 1',(s['address'],)).fetchone()
                method,params=('getTransaction',[p['signature'],dict(encoding='jsonParsed',maxSupportedTransactionVersion=1,commitment='finalized')]) if p else ('getSignaturesForAddress',[s['address'],dict(limit=self.page_size,commitment='finalized',**({'before':s['before_sig']} if s['before_sig'] else {}))])
                cached=self._cached(method,params,now)
                why=self._reserve(conn,now,cached)
                if why:
                    blocked=why
                    if why in ('disk_cap','daily_row_cap'):break
                    continue
                with conn:
                    conn.execute('UPDATE watch_collect_state SET last_turn=(SELECT COALESCE(MAX(last_turn),0)+1 FROM watch_collect_state),last_attempt=? WHERE address=?',(now,s['address']))
                p=conn.execute('SELECT * FROM watch_collect_pending WHERE address=? ORDER BY attempts,slot DESC,signature LIMIT 1',(s['address'],)).fetchone()
                attempted+=1
                if cached is not None:cached_count+=1
                why=self._detail(conn,s['address'],p,now,cached) if p else self._page(conn,s,now,cached)
                if why:
                    if p:
                        with conn:conn.execute('UPDATE watch_collect_pending SET attempts=attempts+1 WHERE address=? AND signature=?',(s['address'],p['signature']))
                    with conn:conn.execute("UPDATE watch_collect_state SET state='unknown',reason=? WHERE address=?",(why,s['address']))
            return self._status(conn,attempted,blocked or 'polling_coverage_only',cached_count)
        except (OSError,sqlite3.Error):
            conn.rollback()
            return dict(state='unknown',reason='collect_store_unknown',calls_attempted=attempted,cached_operations=cached_count)
        finally:conn.close()

    def _status(self, conn, attempted, reason='polling_coverage_only', cached_count=0):
        now=self.net.clock()
        pending=conn.execute('SELECT count(*) FROM watch_collect_pending').fetchone()[0]
        daily=conn.execute('SELECT credits FROM daily WHERE day=?',(_day(now),)).fetchone()
        credits=daily[0] if daily else 0
        status=[]
        for r in conn.execute('SELECT s.* FROM watch_collect_state s JOIN watch_list w USING(address) WHERE w.removed_at IS NULL ORDER BY s.address LIMIT ?', (101,)):
            status.append(dict(address=r['address'],state=r['state'],reason=r['reason'],cursor=r['cursor'],cursor_slot=r['cursor_slot'],frontier=r['before_sig'],forward_floor=r['forward_floor'],history_before_floor='unknown',scan_age_s=None if r['last_success'] is None else max(0,now-r['last_success']),newest_event_age_s=None if r['last_event_at'] is None else max(0,now-r['last_event_at'])))
        truncated=len(status)>100
        status=status[:100]
        return dict(status_scope='bounded_status_page_not_watch_universe',wallets_truncated=truncated,state='partial' if reason in ('polling_coverage_only','shared_pace') else 'unknown',reason=reason,calls_attempted=attempted,cached_operations=cached_count,history_cache_ttl_s=60,pending=pending,lag_guarantee_s=None,watchset=[r['address'] for r in status],wallets=status,credits_reserved_estimate_today=credits,credit_cap_estimate_per_day=CREDITS_PER_DAY,credits_remaining_estimate_today=None if CREDITS_PER_DAY is None else max(0,CREDITS_PER_DAY-credits))
