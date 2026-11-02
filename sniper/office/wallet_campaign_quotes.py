"""Research launch quotes through the existing owner; no signing, fills or net-PnL authority."""
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION='wallet-campaign-launch-quotes-v1'
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
CLIP=25_000_000
HORIZONS=(5.,30.,300.)
SCHEMA='''
CREATE TABLE IF NOT EXISTS campaign_studies(study_id TEXT PRIMARY KEY,launch_id TEXT,campaign_id TEXT,mint TEXT,source_signature TEXT,
 source_receipt REAL,known_at REAL,emitted_at REAL,decision_at REAL,recorded_before REAL,recorded_at REAL,state TEXT,reason TEXT,version TEXT,snapshot_json TEXT);
CREATE TABLE IF NOT EXISTS campaign_pending(study_id TEXT,arm TEXT,offset_s REAL,due_host REAL,PRIMARY KEY(study_id,arm,offset_s));
CREATE INDEX IF NOT EXISTS campaign_due ON campaign_pending(due_host);
CREATE TABLE IF NOT EXISTS campaign_quotes(study_id TEXT,arm TEXT,offset_s REAL,state TEXT,reason TEXT,due_host REAL,
 requested_host REAL,response_host REAL,observed_host REAL,requested_utc REAL,response_utc REAL,clock_offset_s REAL,clock_sampled_at REAL,input_mint TEXT,output_mint TEXT,input_atomic TEXT,
 expected_output_atomic TEXT,minimum_output_atomic TEXT,acquired_qty_atomic TEXT,context_slot INTEGER,route_id TEXT,
 accounted INTEGER NOT NULL DEFAULT 0,net_usdc_atomic TEXT,execution_state TEXT NOT NULL DEFAULT 'quote_only',
 PRIMARY KEY(study_id,arm,offset_s));
'''

def finite(v):return type(v) in (int,float) and math.isfinite(v) and v>0

def atomic(v,zero=False):return isinstance(v,str) and v.isascii() and v.isdigit() and len(v)<=20 and int(v)<2**64 and (int(v)>=0 if zero else int(v)>0)

def day(at):return datetime.fromtimestamp(at,timezone.utc).strftime('%Y-%m-%d')

def evaluate_round_trip(entry,exit,*,cost_usdc_atomic=None,cost_ref=None):
    """Conditional quote arithmetic only; quote output is not a landed inventory witness."""
    out=dict(state='censored',reason='quote_or_quantity_unknown',quoted_gross_usdc_atomic=None,quoted_after_cost_usdc_atomic=None,
             net_usdc_atomic=None,cost_state='unknown',execution_state='quote_only',landing_state='unknown')
    if (entry.get('state')!='known' or exit.get('state')!='known' or entry.get('input_mint')!=USDC or exit.get('output_mint')!=USDC
        or entry.get('output_mint')!=exit.get('input_mint') or not atomic(entry.get('input_atomic'))
        or not atomic(entry.get('acquired_qty_atomic')) or exit.get('input_atomic')!=entry['acquired_qty_atomic']
        or not atomic(exit.get('expected_output_atomic')) or not finite(entry.get('response_host')) or not finite(exit.get('response_host'))
        or exit['response_host']<entry['response_host']):return out
    gross=int(exit['expected_output_atomic'])-int(entry['input_atomic'])
    out.update(state='known',reason=None,quoted_gross_usdc_atomic=str(gross))
    if atomic(cost_usdc_atomic,True) and isinstance(cost_ref,str) and cost_ref:
        out.update(cost_state='supplied_model',cost_ref=cost_ref,quoted_after_cost_usdc_atomic=str(gross-int(cost_usdc_atomic)))
    return out

class CampaignProbe:
    def __init__(self,owner,source):
        self.owner,self.source=owner,Path(source)
        owner.seen.executescript('CREATE TABLE IF NOT EXISTS campaign_seen(study_id TEXT PRIMARY KEY,day TEXT); CREATE TABLE IF NOT EXISTS campaign_waiting(source_rowid INTEGER PRIMARY KEY,checked_at REAL);')

    @staticmethod
    def init_day(conn):conn.executescript(SCHEMA)

    @staticmethod
    def has_pending(conn):return conn.execute('SELECT 1 FROM campaign_pending LIMIT 1').fetchone() is not None

    def clock_floor(self,sample):
        if not finite(sample):return 'campaign_clock_unknown'
        try:
            row=self.owner.seen.execute("SELECT value FROM meta WHERE key='campaign_clock_floor'").fetchone()
            previous=float(row[0]) if row else None
            if previous is not None and (not finite(previous) or sample<previous):return 'campaign_clock_regressed'
            if previous is None or sample>previous:
                self.owner.seen.execute("INSERT OR REPLACE INTO meta VALUES('campaign_clock_floor',?)",(repr(sample),));self.owner.seen.commit()
            return None
        except (OSError,sqlite3.Error,ValueError,TypeError):
            self.owner.seen.rollback();return 'campaign_clock_store_unknown'

    def ingest(self,host,clock):
        src=None
        reason=self.clock_floor(host)
        if reason:return reason
        try:
            stat=self.source.stat();identity=json.dumps([str(self.source.resolve()),stat.st_dev,stat.st_ino])
            prior=self.owner.seen.execute("SELECT value FROM meta WHERE key='campaign_source_identity'").fetchone()
            if prior and prior[0]!=identity:return 'campaign_source_replaced'
            self.owner.seen.execute("INSERT OR IGNORE INTO meta VALUES('campaign_source_identity',?)",(identity,))
            src=sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro',uri=True,timeout=0);src.row_factory=sqlite3.Row
            deadline=time.monotonic()+1
            src.set_progress_handler(lambda:time.monotonic()>deadline,1000)
            cursor=int((self.owner.seen.execute("SELECT value FROM meta WHERE key='campaign_cursor'").fetchone() or ['0'])[0])
            anchor=self.owner.seen.execute("SELECT value FROM meta WHERE key='campaign_anchor'").fetchone()
            if cursor and anchor:
                row=src.execute('SELECT rowid,launch_id,mint,source_signature FROM wallet_campaign_launches WHERE rowid=?',(cursor,)).fetchone()
                if row is None or json.dumps(list(row))!=anchor[0]:return 'campaign_source_row_changed'
            fresh=src.execute('SELECT rowid AS source_rowid,* FROM wallet_campaign_launches WHERE rowid>? ORDER BY rowid LIMIT 100',(cursor,)).fetchall()
            waiting=self.owner.seen.execute('SELECT source_rowid FROM campaign_waiting ORDER BY checked_at,source_rowid LIMIT 100').fetchall()
            rows={r['source_rowid']:r for r in fresh}
            for r in waiting:
                old=src.execute('SELECT rowid AS source_rowid,* FROM wallet_campaign_launches WHERE rowid=?',(r[0],)).fetchone()
                if old is not None:rows[r[0]]=old
                else:return 'campaign_source_row_missing'
            for rid,row in rows.items():
                if row['emitted_at'] is None:
                    if self.owner.seen.execute('SELECT count(*) FROM campaign_waiting').fetchone()[0]>=200 and not self.owner.seen.execute('SELECT 1 FROM campaign_waiting WHERE source_rowid=?',(rid,)).fetchone():
                        self._study(dict(row),host,clock,forced='emission_waiting_cap')
                    else:self.owner.seen.execute('INSERT INTO campaign_waiting VALUES(?,?) ON CONFLICT(source_rowid) DO UPDATE SET checked_at=excluded.checked_at',(rid,host))
                else:
                    self._study(dict(row),host,clock)
                    self.owner.seen.execute('DELETE FROM campaign_waiting WHERE source_rowid=?',(rid,))
            if fresh:
                last=fresh[-1]
                self.owner.seen.execute("INSERT OR REPLACE INTO meta VALUES('campaign_cursor',?)",(str(last['source_rowid']),))
                self.owner.seen.execute("INSERT OR REPLACE INTO meta VALUES('campaign_anchor',?)",(json.dumps([last['source_rowid'],last['launch_id'],last['mint'],last['source_signature']]),))
            self.owner.seen.commit();return None
        except (OSError,sqlite3.Error,ValueError,TypeError):return 'campaign_source_unknown'
        finally:
            if src is not None:src.close()

    def _study(self,row,host,clock,forced=None):
        pid=hashlib.sha256(json.dumps([VERSION,row['mint'],row['source_signature']]).encode()).hexdigest()
        reason=forced;receipt,known,emitted=(row.get(k) for k in ('source_receipt','known_at','emitted_at'))
        try:coverage=json.loads(row['coverage_json'])
        except (ValueError,TypeError,KeyError):coverage={}
        if not isinstance(coverage,dict):coverage={}
        if (row.get('version')!='wallet-campaign-launch-v1' or coverage.get('creation_verified') is not True
            or not isinstance(coverage.get('launch_proof_ref'),str) or not coverage['launch_proof_ref']
            or coverage.get('launch_method')!='wallet-watch-launch-v1'
            or coverage.get('phase') not in ('observed_preparation','post_atomic_seed','unknown','already_trading')):
            reason=reason or 'creation_proof_unknown'
        if not all(finite(v) for v in (receipt,known,emitted)) or not receipt<=known<=emitted<=host:reason=reason or 'launch_clock_unknown'
        elif host-receipt>120:reason=reason or 'launch_stale'
        if not isinstance(row['mint'],str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',row['mint']) or row['mint']==USDC:reason=reason or 'mint_unknown'
        if not isinstance(row['source_signature'],str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,88}',row['source_signature']):reason=reason or 'signature_unknown'
        d=day((receipt if finite(receipt) else host)-clock[0])
        self.owner.seen.execute('INSERT OR IGNORE INTO campaign_seen VALUES(?,?)',(pid,d));self.owner.seen.commit()
        d=self.owner.seen.execute('SELECT day FROM campaign_seen WHERE study_id=?',(pid,)).fetchone()[0]
        c=self.owner.day(d)
        old=c.execute('SELECT recorded_at FROM campaign_studies WHERE study_id=?',(pid,)).fetchone()
        if old is not None:
            if old[0] is None:
                after=self.owner.now_fn()
                if finite(after) and after>=host and self.clock_floor(after) is None:
                    with c:c.execute('UPDATE campaign_studies SET recorded_at=? WHERE study_id=?',(after,pid))
            return
        with c:
            c.execute('INSERT INTO campaign_studies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pid,row['launch_id'],row['campaign_id'],row['mint'],row['source_signature'],receipt,known,emitted,emitted,host,None,'censored' if reason else 'pending',reason,VERSION,json.dumps(row,sort_keys=True)))
            if not reason:
                for arm,delay in [('ready',0),('delayed',10)]:c.execute('INSERT INTO campaign_pending VALUES(?,?,0,?)',(pid,arm,emitted+delay))
        after=self.owner.now_fn()
        if finite(after) and after>=host and self.clock_floor(after) is None:
            with c:c.execute('UPDATE campaign_studies SET recorded_at=? WHERE study_id=?',(after,pid))

    def record(self,c,pid,arm,h,state,reason,due,**fields):
        observed=self.owner.now_fn()
        clock_reason=self.clock_floor(observed)
        if state=='known' and clock_reason:
            state,reason='censored',clock_reason;fields.pop('acquired_qty_atomic',None)
        data=dict(study_id=pid,arm=arm,offset_s=h,state=state,reason=reason,due_host=due,observed_host=observed if clock_reason is None else None,**fields)
        c.execute('INSERT OR REPLACE INTO campaign_quotes(%s) VALUES(%s)'%(','.join(data),','.join('?' for _ in data)),tuple(data.values()))
        c.execute('DELETE FROM campaign_pending WHERE study_id=? AND arm=? AND offset_s=?',(pid,arm,h))
        return reason

    def no_entry(self,c,pid,arm,due):
        for h in HORIZONS:self.record(c,pid,arm,h,'censored','no_entry',due+h)

    def due(self,clock,daily_cap):
        done=0;seen=0
        for c in list(self.owner.days.values()):
            now=self.owner.now_fn()
            if self.clock_floor(now):return done
            if finite(now):
                with c:c.execute('UPDATE campaign_studies SET recorded_at=? WHERE study_id IN (SELECT study_id FROM campaign_studies WHERE recorded_at IS NULL AND recorded_before<=? LIMIT 100)',(now,now))
            jobs=c.execute('SELECT p.*,s.mint,s.recorded_at FROM campaign_pending p JOIN campaign_studies s USING(study_id) WHERE p.due_host<=? AND s.recorded_at IS NOT NULL ORDER BY due_host LIMIT ?',(self.owner.now_fn(),max(0,100-seen))).fetchall()
            for row in jobs:
                seen+=1;done+=self.one(c,row,clock,daily_cap)
            self.finalize(c)
            if seen>=100:break
        return done

    @staticmethod
    def finalize(c):
        rows=c.execute("SELECT study_id FROM campaign_studies s WHERE state='pending' AND NOT EXISTS(SELECT 1 FROM campaign_pending p WHERE p.study_id=s.study_id) LIMIT 100").fetchall()
        with c:
            for row in rows:
                known,total=c.execute("SELECT sum(state='known'),count(*) FROM campaign_quotes WHERE study_id=?",(row[0],)).fetchone()
                state='quoted' if total==8 and known==8 else 'partial' if known else 'censored'
                c.execute('UPDATE campaign_studies SET state=?,reason=? WHERE study_id=?',(state,'quote_only_not_fills',row[0]))

    def one(self,c,row,clock,daily_cap):
        p,a,h,due=(row[k] for k in ('study_id','arm','offset_s','due_host'));requested=self.owner.now_fn()
        if (not finite(requested) or not isinstance(clock,(tuple,list)) or len(clock)!=2
            or type(clock[0]) not in (int,float) or not math.isfinite(clock[0]) or not finite(clock[1]) or clock[1]>requested):return 0
        utc=requested-clock[0]
        if not finite(utc) or self.clock_floor(requested):return 0
        if requested<due or requested<row['recorded_at']:return 0
        deadline=due+max(5,h*.1)
        if requested>deadline or self.owner._today>=daily_cap:
            with c:
                self.record(c,p,a,h,'censored','window_passed' if requested>deadline else 'cap_reached',due,requested_host=requested)
                if h==0:self.no_entry(c,p,a,due)
            return 0
        if not self.owner.glob.take(utc):return 0
        if h==0:inp,out,amount=USDC,row['mint'],CLIP
        else:
            buy=c.execute("SELECT acquired_qty_atomic FROM campaign_quotes WHERE study_id=? AND arm=? AND offset_s=0 AND state='known'",(p,a)).fetchone()
            if not buy:
                with c:self.record(c,p,a,h,'censored','no_entry',due)
                return 0
            inp,out,amount=row['mint'],USDC,int(buy[0])
        fields=dict(requested_host=requested,requested_utc=utc,clock_offset_s=clock[0],clock_sampled_at=clock[1],input_mint=inp,output_mint=out,input_atomic=str(amount),accounted=1)
        self.owner._today+=1
        with c:self.record(c,p,a,h,'censored','quote_interrupted',due,**fields)
        try:q,reason,extra=self.owner._jupiter(inp,out,amount,utc)
        except Exception:q,reason,extra=None,'provider_error',{}
        response=self.owner.now_fn();fields.update(response_host=response,response_utc=response-clock[0],route_id=extra.get('route_id'))
        if not finite(response) or response<requested:reason='clock_regressed'
        elif response>deadline:reason='response_late'
        response_clock_reason=self.clock_floor(response)
        if response_clock_reason:reason=reason or response_clock_reason
        if q is None:reason=reason or 'provider_error'
        elif reason is None:
            if q.get('inputMint')!=inp or q.get('outputMint')!=out or str(q.get('inAmount'))!=str(amount):reason='quote_identity_mismatch'
            elif not atomic(q.get('outAmount')) or not atomic(q.get('otherAmountThreshold')) or int(q['otherAmountThreshold'])>int(q['outAmount']):reason='quote_amount_unknown'
            elif not q.get('routePlan'):reason='route_unknown'
            else:
                fields.update(expected_output_atomic=q['outAmount'],minimum_output_atomic=q['otherAmountThreshold'],context_slot=q.get('contextSlot') if type(q.get('contextSlot')) is int else None)
                if h==0:fields['acquired_qty_atomic']=q['outAmount']
        with c:
            reason=self.record(c,p,a,h,'censored' if reason else 'known',reason,due,**fields)
            if h==0:
                if reason:self.no_entry(c,p,a,due)
                else:
                    for horizon in HORIZONS:c.execute('INSERT OR IGNORE INTO campaign_pending VALUES(?,?,?,?)',(p,a,horizon,response+horizon))
        return 1

def reconcile_cash_flow(transactions,*,owner,commitment,as_of,fx=None):
    """Cash deltas from caller-attested confirmed receipts; not inventory PnL or independent finality proof.

    Native balance deltas already include fees, tips and rent movements. Never subtract meta.fee twice.
    as_of, receipt blockTime and FX at are UTC. A dated FX observation converts cash at that stated rate,
    not a fabricated historical execution price; callers must not substitute an uncorrected host clock.
    """
    from decimal import Decimal,InvalidOperation
    import base58
    from solders.pubkey import Pubkey
    out=dict(state='unknown',reason='receipt_cash_unknown',clock_basis='utc',usdc_delta_atomic=None,native_delta_lamports=None,
             network_fee_lamports=None,net_cash_usdc=None,round_trip_profit_state='unknown_inventory_and_attribution')
    try:
        if commitment not in ('confirmed','finalized') or not finite(as_of) or not 1<=len(transactions)<=100:raise ValueError()
        if str(Pubkey.from_string(owner))!=owner:raise ValueError()
        total_usdc=total_native=fees=owner_fees=0;seen=set();times=[]
        for tx in transactions:
            if not isinstance(tx,dict):raise ValueError()
            if type(tx.get('slot')) is not int or tx['slot']<=0 or not finite(tx.get('blockTime')) or tx['blockTime']>as_of:raise ValueError()
            msg=tx['transaction']['message'];keys=msg['accountKeys'];meta=tx['meta'];sigs=tx['transaction']['signatures']
            if 'err' not in meta:raise ValueError()
            if not sigs or any(len(base58.b58decode(s))!=64 for s in sigs) or sigs[0] in seen:raise ValueError()
            seen.add(sigs[0]);times.append(tx['blockTime'])
            if not all(isinstance(k,dict) for k in keys):raise ValueError()
            addresses=[k['pubkey'] for k in keys]
            if len(set(addresses))!=len(addresses) or owner not in addresses:raise ValueError()
            for address in addresses:Pubkey.from_string(address)
            index=addresses.index(owner)
            if keys[index].get('signer') is not True:raise ValueError()
            pre,post=meta['preBalances'],meta['postBalances']
            valid_int=lambda x:type(x) is int and 0<=x<2**64
            if len(pre)!=len(keys) or len(post)!=len(keys) or not all(valid_int(x) for x in pre+post) or not valid_int(meta['fee']):raise ValueError()
            total_native+=post[index]-pre[index];fees+=meta['fee']
            if index==0:owner_fees+=meta['fee']
            sides=[];identities=[]
            for balances in (meta['preTokenBalances'],meta['postTokenBalances']):
                if not isinstance(balances,list):raise ValueError()
                items={};all_accounts={}
                for b in balances:
                    i=b['accountIndex'];u=b['uiTokenAmount']
                    if type(i) is not int or not 0<=i<len(keys) or i in all_accounts or type(u['decimals']) is not int or not 0<=u['decimals']<=255 or not atomic(u['amount'],True):raise ValueError()
                    for address in (b['owner'],b['mint'],b['programId']):Pubkey.from_string(address)
                    all_accounts[i]=(b['owner'],b['mint'],b['programId'],u['decimals'])
                    if b['owner']!=owner or b['mint']!=USDC:continue
                    if u['decimals']!=6:raise ValueError()
                    if b.get('programId')!='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA':raise ValueError()
                    items[i]=int(u['amount'])
                sides.append(items);identities.append(all_accounts)
            if any(identities[0][i]!=identities[1][i] for i in identities[0].keys()&identities[1].keys()):raise ValueError()
            if not sides[0] or sides[0].keys()!=sides[1].keys():raise ValueError()
            delta=sum(sides[1].values())-sum(sides[0].values())
            if meta.get('err') is not None and delta!=0:raise ValueError()
            total_usdc+=delta
        converted=None;ref=None
        if fx is not None:
            if not isinstance(fx.get('usdc_per_sol'),str) or len(fx['usdc_per_sol'])>64:raise ValueError()
            rate=Decimal(fx['usdc_per_sol'])
            if not rate.is_finite() or rate<=0 or abs(rate.adjusted())>18 or abs(rate.as_tuple().exponent)>18 or not finite(fx['at']) or fx['at']>as_of or not isinstance(fx.get('ref'),str) or not fx['ref']:raise ValueError()
            converted=format(Decimal(total_usdc)/Decimal(10**6)+Decimal(total_native)/Decimal(10**9)*rate,'f');ref=fx['ref']
        out.update(state='known',reason=None,usdc_delta_atomic=str(total_usdc),native_delta_lamports=str(total_native),network_fee_lamports=str(fees),network_fee_scope='transaction_total_not_subtracted_again',owner_payer_fee_lamports=str(owner_fees),net_cash_usdc=converted,fx_ref=ref,
                   cost_state='native_cash_delta_includes_fee_rent_tips',confirmation=commitment,confirmation_authority='caller_attested',cash_window=[min(times),max(times)])
    except (KeyError,TypeError,ValueError,IndexError,OverflowError,InvalidOperation):pass
    return out
