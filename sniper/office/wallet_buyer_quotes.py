"""Frozen prospective buyer quote study; observed-only research, never fills or funded authority."""
import fcntl
import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

from sniper.office.wallet_campaign_quotes import USDC, CLIP, atomic, finite

VERSION='wallet-buyer-quotes-v1'
WSOL='So11111111111111111111111111111111111111112'
BUYERS=('97DAFA8iRV3hHMoHchwNw7zTXA8HHMf24zM3PKmBwWwk','mATtYiy3Cn1Bihg3aMaWDzWsWN33txxtnhDyJcFFiWZ')
DISCOVERY=frozenset(('EF6sTJvSo8vyaLMz5deax8ZUSUJ4rwykT9qzzHYupump','2RxFmbxr93ywy6JhGmyjKGuoCcARtzfgbEoH4icxpump','HgGArDobuZVt2qWAgKCBVuUzCV37G11hsWGCSP2Qpump','kiFRYL9EKLY2TFL5p75oD9QxmbK6t5QW5NM697fpump','5EH4bo9k5omHfD1mRSKfpUsSgPRyeRjaaFdhHGq1pump','64vVGiNa4MefL4rq3mHzLiq8APfyc8iWLbumsFNfpump','D7Q2ApyCUfAm9Vay3wC2g1tDBtrKdomYCLdiSPaNpump'))
DELAYS=(1,5,10)
HORIZONS=(60,300)
DDL='''
CREATE TABLE IF NOT EXISTS buyer_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS buyer_events(signature TEXT,ordinal INTEGER,buyer TEXT,mint TEXT,received_at REAL,detected_at REAL,study_id TEXT,PRIMARY KEY(signature,ordinal));
CREATE TABLE IF NOT EXISTS buyer_studies(study_id TEXT PRIMARY KEY,parent_id TEXT,subject TEXT,control_rank INTEGER,buyer TEXT,mint TEXT,signature TEXT,ordinal INTEGER,source_received_at REAL,anchor_at REAL,detected_at REAL,state TEXT,reason TEXT,snapshot_json TEXT,version TEXT,launch_state TEXT);
CREATE TABLE IF NOT EXISTS buyer_quotes(study_id TEXT,delay_s INTEGER,horizon_s INTEGER,basis TEXT,due_host REAL,state TEXT,reason TEXT,input_mint TEXT,output_mint TEXT,input_atomic TEXT,expected_output_atomic TEXT,minimum_output_atomic TEXT,requested_host REAL,response_host REAL,requested_utc REAL,response_utc REAL,clock_offset_s REAL,clock_sampled_at REAL,route_id TEXT,context_slot INTEGER,accounted INTEGER NOT NULL DEFAULT 0,net_usdc_atomic TEXT,execution_state TEXT NOT NULL DEFAULT 'quote_only',PRIMARY KEY(study_id,delay_s,horizon_s,basis));
CREATE INDEX IF NOT EXISTS buyer_pending ON buyer_quotes(state,due_host);
'''

def _key(*parts):return hashlib.sha256(json.dumps(parts,separators=(',',':')).encode()).hexdigest()

def _band(value):
    if not atomic(value,True):return None
    return sum(int(value)>=n*10**9 for n in (10,50,200,1000))

def _valid_address(value):
    from solders.pubkey import Pubkey
    try:return isinstance(value,str) and str(Pubkey.from_string(value))==value
    except ValueError:return False

def _valid_signature(value):
    import base58
    try:return isinstance(value,str) and 64<=len(value)<=88 and len(base58.b58decode(value))==64
    except ValueError:return False

class BuyerProbe:
    def __init__(self,owner,source,activation):
        if (not isinstance(activation,dict) or not finite(activation.get('activation_host'))
            or not isinstance(activation.get('submitted_digest'),str) or not re.fullmatch('[0-9a-f]{64}',activation['submitted_digest'])
            or not isinstance(activation.get('watched'),list) or not all(isinstance(k,str) for k in activation['watched'])
            or not set(BUYERS).issubset(activation['watched']) or activation.get('capture_healthy') is not True
            or not finite(activation.get('capture_sampled_at')) or not 0<=activation['activation_host']-activation['capture_sampled_at']<=30):
            raise ValueError('buyer activation coverage evidence required')
        self.owner,self.source,self.activation=owner,Path(source),activation
        self.lock=(owner.out_dir/'buyer-study.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();raise ValueError('buyer_study_already_running') from None
        self.conn=sqlite3.connect(owner.out_dir/'buyer-study.db',timeout=.1)
        self.conn.row_factory=sqlite3.Row;self.conn.executescript(DDL)
        frozen=json.dumps(activation,sort_keys=True,allow_nan=False)
        self.activation=json.loads(frozen)
        prior=self.conn.execute("SELECT value FROM buyer_meta WHERE key='activation'").fetchone()
        if prior and prior[0]!=frozen:self.close();raise ValueError('buyer activation immutable')
        with self.conn:self.conn.execute("INSERT OR IGNORE INTO buyer_meta VALUES('activation',?)",(frozen,))

    def close(self):
        self.conn.close();self.lock.close()

    def quotes_today(self,utc):
        return self.conn.execute('SELECT coalesce(sum(accounted),0) FROM buyer_quotes WHERE requested_utc>=?',(utc-utc%86400,)).fetchone()[0]

    def _clock(self,now,clock):
        if (not finite(now) or not isinstance(clock,(tuple,list)) or len(clock)!=2
            or type(clock[0]) not in (float,int) or not math.isfinite(clock[0])
            or not finite(clock[1]) or not 0<=now-clock[1]<=60 or not finite(now-clock[0])):return 'clock_unknown'
        prior=self.conn.execute("SELECT value FROM buyer_meta WHERE key='clock_floor'").fetchone()
        if prior and now<float(prior[0]):return 'clock_regressed'
        with self.conn:self.conn.execute("INSERT OR REPLACE INTO buyer_meta VALUES('clock_floor',?)",(repr(now),))

    def _creation(self,row,now,clock):
        out=dict(state='unknown',reason='creation_source_unavailable',read_known_at=now)
        desk=getattr(self.owner,'desk_dir',None)
        if desk is None or not finite(row['received_at']) or row['received_at']>now:return out
        found=[]
        for stamp in (row['received_at'],row['received_at']-86400):
            path=Path(desk)/'pump'/(datetime.fromtimestamp(stamp,timezone.utc).strftime('%Y-%m-%d')+'.db')
            if not path.is_file():continue
            c=None
            try:
                c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0);c.row_factory=sqlite3.Row
                deadline=time.monotonic()+.05;c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
                fact=c.execute('SELECT * FROM launches WHERE mint=?',(row['mint'],)).fetchone()
                if fact:found.append((path,dict(fact)))
            except (OSError,sqlite3.Error):return out
            finally:
                if c is not None:c.close()
        if len(found)!=1:
            out['reason']='creation_missing' if not found else 'creation_conflict';return out
        path,fact=found[0];out.update(source_path=str(path.resolve()),fact=fact)
        if (not _valid_address(fact.get('curve')) or not _valid_signature(fact.get('signature'))
            or type(fact.get('slot')) is not int or fact['slot']<=0 or type(fact.get('block_time')) is not int
            or not finite(fact.get('received_at')) or fact['received_at']>row['received_at']):
            out['reason']='creation_identity_or_clock_unknown';return out
        activation=self.activation['activation_host'];creation=fact['received_at'];block=fact['block_time']
        activation_utc=activation-clock[0]
        if creation<activation:
            out.update(state='old_launch',reason='creation_pre_activation');return out
        if block<activation_utc:
            out['reason']='creation_boundary_second_unknown' if block==math.floor(activation_utc) else 'creation_clock_conflict';return out
        if block>creation-clock[0]:out['reason']='creation_clock_conflict';return out
        if row['received_at']-creation>86400:
            out.update(state='old_launch',reason='creation_over_24h');return out
        out.update(state='new_launch',reason=None,age_s=row['received_at']-creation,authority='local_native_creation_decoder_not_independent_chain_verification')
        return out

    def _study(self,row,now,*,parent=None,rank=None,anchor=None,clock=(0.,0.)):
        buyer=row['actor'];mint=row['mint'];sid=_key(VERSION,parent if parent else buyer,mint)
        if self.conn.execute('SELECT 1 FROM buyer_studies WHERE study_id=?',(sid,)).fetchone():return sid
        reason=None
        if mint in DISCOVERY:reason='discovery_mint'
        elif row['quote_mint']!=WSOL:reason='unsupported_quote_mint'
        elif not _valid_address(mint) or not _valid_address(row['pool']):reason='identity_unknown'
        elif not atomic(row['base_atomic']) or not atomic(row['quote_atomic']) or _band(row['pre_quote_reserve_atomic']) is None:reason='amount_or_reserve_unknown'
        elif not _valid_signature(row['signature']) or type(row['slot']) is not int or row['slot']<=0 or type(row['ordinal']) is not int or row['ordinal']<0:reason='source_identity_unknown'
        original=row['received_at'];anchor=original if anchor is None else anchor
        if not finite(original) or original>now or not finite(anchor) or anchor>now:reason='source_clock_unknown'
        creation=self._creation(row,now,clock)
        snapshot=dict(row);snapshot['creation']=creation
        self.conn.execute('INSERT INTO buyer_studies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sid,parent,'control' if parent else 'target',rank,buyer,mint,row['signature'],row['ordinal'],original,anchor,now,'censored' if reason else 'pending',reason,json.dumps(snapshot,sort_keys=True),VERSION,creation['state']))
        if not reason:
            for delay in DELAYS:self.conn.execute("INSERT INTO buyer_quotes(study_id,delay_s,horizon_s,basis,due_host,state) VALUES(?,?,0,'entry',?,'pending')",(sid,delay,anchor+delay))
        return sid

    def _controls(self,src,trigger,parent,now,clock):
        # Selected watch activity only; never represented as a market-wide control universe.
        rows=src.execute('SELECT rowid AS source_rowid,* FROM ps_watch_events INDEXED BY ps_watch_events_at WHERE received_at>=? AND received_at<? ORDER BY received_at,rowid LIMIT 1001',(trigger['received_at']-60,trigger['received_at'])).fetchall()
        if len(rows)>1000:return 'control_source_truncated'
        choices={}
        for row in rows:
            if (row['is_buy']!=1 or row['actor'] in BUYERS or row['mint'] in DISCOVERY or row['mint']==trigger['mint']
                or row['quote_mint']!=WSOL or _band(row['pre_quote_reserve_atomic'])!=_band(trigger['pre_quote_reserve_atomic'])):continue
            choices.setdefault(row['mint'],row)
        ranked=sorted(choices,key=lambda m:(_key(trigger['signature'],trigger['ordinal'],m),m))[:3]
        for rank,mint in enumerate(ranked):self._study(choices[mint],now,parent=parent,rank=rank,anchor=trigger['received_at'],clock=clock)
        return None if len(ranked)==3 else 'control_unmatched'

    def ingest(self,now,clock):
        src=None
        try:
            stat=self.source.stat();identity=json.dumps([str(self.source.resolve()),stat.st_dev,stat.st_ino])
            prior=self.conn.execute("SELECT value FROM buyer_meta WHERE key='source_identity'").fetchone()
            if prior and prior[0]!=identity:return 'source_replaced'
            src=sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro',uri=True,timeout=0);src.row_factory=sqlite3.Row
            deadline=time.monotonic()+.1;src.set_progress_handler(lambda:time.monotonic()>deadline,1000)
            cursor=self.conn.execute("SELECT value FROM buyer_meta WHERE key='cursor'").fetchone()
            if cursor:
                cursor=int(cursor[0]);anchor=self.conn.execute("SELECT value FROM buyer_meta WHERE key='anchor'").fetchone()
                if cursor and anchor:
                    row=src.execute('SELECT rowid,signature,ordinal FROM ps_watch_events WHERE rowid=?',(cursor,)).fetchone()
                    if row is None or json.dumps(list(row))!=anchor[0]:return 'source_anchor_changed'
            else:
                # Indexed time seed avoids replaying all retained history, including after a delayed start.
                row=src.execute('SELECT min(rowid) FROM ps_watch_events INDEXED BY ps_watch_events_at WHERE received_at>=?',(self.activation['activation_host'],)).fetchone()
                cursor=row[0]-1 if row[0] is not None else src.execute('SELECT coalesce(max(rowid),0) FROM ps_watch_events').fetchone()[0]
            rows=src.execute('SELECT rowid AS source_rowid,* FROM ps_watch_events WHERE rowid>? ORDER BY rowid LIMIT 200',(cursor,)).fetchall()
            with self.conn:
                self.conn.execute("INSERT OR IGNORE INTO buyer_meta VALUES('source_identity',?)",(identity,))
                for row in rows:
                    if row['actor'] in BUYERS and row['is_buy']==1 and (not finite(row['received_at']) or row['received_at']>=self.activation['activation_host']):
                        sid=self._study(row,now,clock=clock)
                        self.conn.execute('INSERT OR IGNORE INTO buyer_events VALUES(?,?,?,?,?,?,?)',(row['signature'],row['ordinal'],row['actor'],row['mint'],row['received_at'],now,sid))
                        target=self.conn.execute('SELECT signature,ordinal,reason FROM buyer_studies WHERE study_id=?',(sid,)).fetchone()
                        if target['signature']==row['signature'] and target['ordinal']==row['ordinal'] and not target['reason']:
                            why=self._controls(src,row,sid,now,clock)
                            self.conn.execute("INSERT OR REPLACE INTO buyer_meta VALUES(?,?)",('controls:'+sid,why or 'matched'))
                    cursor=row['source_rowid']
                self.conn.execute("INSERT OR REPLACE INTO buyer_meta VALUES('cursor',?)",(str(cursor),))
                if rows:self.conn.execute("INSERT OR REPLACE INTO buyer_meta VALUES('anchor',?)",(json.dumps([cursor,rows[-1]['signature'],rows[-1]['ordinal']]),))
            return None
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
            self.conn.rollback();return 'source_unavailable'
        finally:
            if src is not None:src.close()

    @staticmethod
    def _priority(row):
        primary=row['delay_s']==5;target=row['subject']=='target';stress=row['basis']=='minimum'
        rank=0 if primary and target else 1 if primary else 2 if target else 3
        return (row['cohort']!='new_launch',4 if stress else rank,row['delay_s'],row['control_rank'] or 0,row['due_host'],row['study_id'],row['horizon_s'],row['basis'])

    def _finish(self,row,state,reason,**fields):
        values={'state':state,'reason':reason,**fields}
        self.conn.execute('UPDATE buyer_quotes SET '+','.join(k+'=?' for k in values)+' WHERE study_id=? AND delay_s=? AND horizon_s=? AND basis=?',(*values.values(),row['study_id'],row['delay_s'],row['horizon_s'],row['basis']))

    def due(self,clock,cap):
        now=self.owner.now_fn();rows=self.conn.execute("SELECT q.*,s.mint,s.subject,s.control_rank,CASE WHEN s.subject='control' THEN p.launch_state ELSE s.launch_state END cohort FROM buyer_quotes q JOIN buyer_studies s USING(study_id) LEFT JOIN buyer_studies p ON s.parent_id=p.study_id WHERE q.state='pending' AND q.due_host<=? ORDER BY q.due_host LIMIT 200",(now,)).fetchall();calls=0
        with self.conn:
            for row in rows:
                if now>row['due_host']+1:self._finish(row,'censored','queue_or_quota_window_passed')
        for row in sorted((r for r in rows if now<=r['due_host']+1),key=self._priority):
            now=self.owner.now_fn()
            if self._clock(now,clock):break
            if now>row['due_host']+1:
                with self.conn:self._finish(row,'censored','queue_or_quota_window_passed')
                continue
            if self.owner._today>=cap or not self.owner.glob.take(now-clock[0]):continue
            entry=row['horizon_s']==0
            amount=CLIP if entry else int(row['input_atomic']);inp,out=(USDC,row['mint']) if entry else (row['mint'],USDC)
            fields=dict(input_mint=inp,output_mint=out,input_atomic=str(amount),requested_host=now,requested_utc=now-clock[0],clock_offset_s=clock[0],clock_sampled_at=clock[1],accounted=1)
            with self.conn:self._finish(row,'censored','quote_interrupted',**fields)
            self.owner._today+=1;calls+=1
            try:q,reason,extra=self.owner._jupiter(inp,out,amount,now-clock[0])
            except Exception:q,reason,extra=None,'provider_error',{}
            response=self.owner.now_fn();fields.update(response_host=response,response_utc=response-clock[0],route_id=extra.get('route_id') if isinstance(extra,dict) else None)
            if not finite(response) or response<now:reason='clock_regressed'
            elif response>row['due_host']+3 or response-now>2:reason='response_late'
            elif q is None:reason=reason or 'provider_error'
            elif not isinstance(q,dict):reason='quote_payload_unknown'
            elif not reason:
                if q.get('inputMint')!=inp or q.get('outputMint')!=out or str(q.get('inAmount'))!=str(amount):reason='quote_identity_mismatch'
                elif not atomic(q.get('outAmount')) or not atomic(q.get('otherAmountThreshold')) or int(q['otherAmountThreshold'])>int(q['outAmount']):reason='quote_amount_unknown'
                elif not q.get('routePlan'):reason='route_unknown'
                else:fields.update(expected_output_atomic=q['outAmount'],minimum_output_atomic=q['otherAmountThreshold'],context_slot=q.get('contextSlot') if type(q.get('contextSlot')) is int else None)
            with self.conn:
                self._finish(row,'censored' if reason else 'known',reason,**fields)
                if not reason and entry:
                    for horizon in HORIZONS:
                        for basis,quantity in [('expected',q['outAmount']),('minimum',q['otherAmountThreshold'])]:
                            self.conn.execute("INSERT OR IGNORE INTO buyer_quotes(study_id,delay_s,horizon_s,basis,due_host,state,input_atomic) VALUES(?,?,?,?,?,'pending',?)",(row['study_id'],row['delay_s'],horizon,basis,response+horizon,quantity))
            # Preserve a regressed response as failure, then stop this turn rather than sending on a bad clock.
            if reason=='clock_regressed':break
        with self.conn:
            self.conn.execute("UPDATE buyer_studies SET state='complete',reason='quote_only_not_fills' WHERE state='pending' AND NOT EXISTS(SELECT 1 FROM buyer_quotes q WHERE q.study_id=buyer_studies.study_id AND q.state='pending')")
        return calls

    def tick(self,clock,cap):
        try:
            reason=self._clock(self.owner.now_fn(),clock)
            if reason:return dict(reason=reason,quotes=0,execution_state='quote_only',coverage='observed_only_unknown_completeness')
            reason=self.ingest(self.owner.now_fn(),clock)
            # Existing studies may finish if the source fails; failure does not fabricate new events.
            return dict(reason=reason,quotes=self.due(clock,cap),execution_state='quote_only',coverage='observed_only_unknown_completeness')
        except (OSError,sqlite3.Error):
            self.conn.rollback()
            return dict(reason='buyer_store_unavailable',quotes=None,execution_state='quote_only',coverage='observed_only_unknown_completeness')
