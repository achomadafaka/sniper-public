"""Operator-only, bounded chart reads. No providers, funded handles or seat exports."""
from collections import Counter,defaultdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from decimal import Decimal,InvalidOperation
from datetime import datetime,timezone
from .report import _ro

EPOCH='office-v2-explore-1'
GPT_EPOCH='office-v2-explore-gpt-1'
BOOKS=((EPOCH,'v2/explore-ledger/ledger.db'),(GPT_EPOCH,'v2/explore-gpt-ledger/ledger.db'))
WSOL='So11111111111111111111111111111111111111112'
GAP_S=30
ANCHOR_S=10
POINT_CAP=600
SEGMENT_CAP=16
SWAP_CAP=2000
CLOSED_S=600


def number(v):
    try:return type(v) in (int,float) and math.isfinite(v) and 0<=v<=1e11
    except OverflowError:return False


def atom(v):
    if type(v) is int:return v if v>=0 else None
    if isinstance(v,str) and len(v)<=100 and v.isascii() and v.isdigit():return int(v)
    return None


def ratio(a,b):
    try:
        value=float(Decimal(a)/Decimal(b))
        return value if math.isfinite(value) else None
    except (InvalidOperation,ZeroDivisionError,OverflowError,TypeError):return None


def segments(points):
    out=[];current=[];previous=None
    for point in points[-POINT_CAP:]:
        if point.get('value') is None:
            if current:out.append(current)
            current=[];previous=None;continue
        if previous is not None and point['time']-previous>GAP_S:
            if current:out.append(current)
            current=[]
        current.append(dict(time=point['time'],value=point['value']));previous=point['time']
    if current:out.append(current)
    return out[-SEGMENT_CAP:],len(points)>POINT_CAP or len(out)>SEGMENT_CAP


def _last(rows):
    """A second's last chain-ordered observation; ambiguous transaction order is a gap."""
    if any(type(r.get('slot')) is not int or r['slot']<0 for r in rows):return None
    latest=max(r['slot'] for r in rows);rows=[r for r in rows if r['slot']==latest]
    if len(rows)==1:return rows[0]
    if any(type(r.get('tx_index')) is not int or r['tx_index']<0 or type(r.get('ordinal')) is not int for r in rows):return None
    keys=[(r['tx_index'],r['ordinal']) for r in rows]
    if len(set(keys))!=len(keys):return None
    return max(rows,key=lambda r:(r['tx_index'],r['ordinal']))


def project_order(row,marks,swaps,*,now,limited=False):
    filled=row.get('filled_at');spend=atom(row.get('spend_lamports'));qty=atom(row.get('tokens_atomic'));dec=row.get('decimals')
    out=dict(order_id=row.get('order_id'),state='unknown',status=row.get('state'),ticker=(str(row['ticker'])[:128] if row.get('ticker') else None),network_id=row.get('network_id'),mint=row.get('mint'),pool=row.get('pool'),filled_at=filled,closed_at=row.get('closed_at'),age_s=None,
             anchor=None,model_fill_sol_per_token=None,model_fill_to_tape_factor=None,tape_segments=[],mark_segments=[],levels={},exit=None,stats={},limited=limited,reason='order_evidence_unknown',tape_ended_at=None,
             source='data/desk/pumpswap/tape.db',gap_s=GAP_S,anchor_max_age_s=ANCHOR_S)
    if row.get('epoch_id') not in (EPOCH,GPT_EPOCH) or row.get('network_id')!='solana' or not number(now) or not number(filled) or filled>now or not spend or not qty:return out
    out['age_s']=now-filled
    scale=10**dec if type(dec) is int and 0<=dec<=18 else None
    out['model_fill_sol_per_token']=ratio(spend*scale,qty*10**9) if scale is not None else None
    policy=None
    try:
        raw=row['policy_json']
        if not isinstance(raw,str) or len(raw.encode())>65536 or hashlib.sha256(raw.encode()).hexdigest()!=row['policy_hash']:raise ValueError('policy_identity')
        policy=json.loads(raw)
        if not isinstance(policy,dict) or any(not number(policy.get(k)) for k in ('sl_pct','tp_pct','trail_pct','catastrophe_pct')):raise ValueError('policy_fields')
    except (ValueError,TypeError,KeyError):policy=None
    out['policy_state']='known' if policy is not None else 'unknown';out['policy_sha256']=row.get('policy_hash')
    levels=dict(entry=0.)
    peak=atom(row.get('peak_lamports'))
    if peak is not None:levels['host_peak']=(ratio(peak,spend)-1)*100
    if policy is not None:
        for key,field,sign in [('stop','sl_pct',-1),('take_profit','tp_pct',1),('catastrophe','catastrophe_pct',-1)]:
            if policy[field]>0:levels[key]=policy[field]*sign
        if peak is not None and peak>spend and policy['trail_pct']>0:levels['trail']=(ratio(peak,spend)*(1-policy['trail_pct']/100)-1)*100
    out['levels']=levels
    closed=row.get('closed_at');end=closed if number(closed) and filled<=closed<=now else now
    if row.get('state')=='closed' and number(closed) and filled<=closed<=now:out['age_s']=closed-filled
    stats=Counter();groups=defaultdict(list)
    for swap in swaps:
        direct,joined=swap.get('block_time'),swap.get('joined_block_time')
        bt=direct if type(direct) is int and direct>0 else joined if type(joined) is int and joined>0 else None
        if bt is None or number(direct) and number(joined) and direct!=joined:
            stats['swaps_without_block_time']+=1;continue
        if not filled-ANCHOR_S<=bt<=end:continue
        rec=dict(swap,time=bt,value=None)
        if swap.get('pool')!=row['pool'] or swap.get('mint')!=row['mint'] or swap.get('quote_mint')!=WSOL:stats['wrong_identity']+=1
        else:
            base,quote=atom(swap.get('pre_base_reserve_atomic')),atom(swap.get('pre_quote_reserve_atomic'))
            if scale is not None and base and quote is not None:
                rec['value']=ratio(quote*scale,base*10**9)
                if rec['value'] is not None and rec['value']<0:rec['value']=None
            if rec['value'] is None:stats['invalid_reserves']+=1
        groups[bt].append(rec)
    points=[]
    for bt,values in sorted(groups.items()):
        last=_last(values)
        if last is None:stats['ambiguous_seconds']+=1
        points.append(dict(time=bt,value=last.get('value') if last else None,row=last))
    anchors=[p for p in points if p['value'] is not None and filled-ANCHOR_S<=p['time']<=filled]
    candidate_anchor=anchors[-1] if anchors else None
    anchor=candidate_anchor if candidate_anchor and candidate_anchor['value']>0 else None
    if anchor:
        rec=anchor['row'];out['anchor']=dict(time=anchor['time'],slot=rec['slot'],signature=rec.get('signature'),ordinal=rec.get('ordinal'),price_sol_per_token=anchor['value'])
        out['model_fill_to_tape_factor']=ratio(out['model_fill_sol_per_token'],anchor['value']) if out['model_fill_sol_per_token'] is not None else None
        out['tape_segments'],tape_limited=segments([dict(time=p['time'],value=(p['value']/anchor['value']-1)*100 if p['value'] is not None else None) for p in points])
    if not anchor:
        tape_limited=False
        if candidate_anchor:
            rec=candidate_anchor['row'];out['anchor']=dict(time=candidate_anchor['time'],slot=rec['slot'],signature=rec.get('signature'),ordinal=rec.get('ordinal'),price_sol_per_token=0.)
    latest=points[-1]['time'] if points else None
    if latest is not None and end-latest>GAP_S:out['tape_ended_at']=latest
    by_time={}
    if marks is None:stats['marks_read_unknown']=1
    bindings=Counter()
    for mark in marks or []:
        at=mark.get('at')
        if not number(at) or not filled<=at<=end:continue
        kind=mark.get('binding_kind')
        bindings[kind if isinstance(kind,str) and kind and len(kind)<=80 else 'not_recorded']+=1
        value=atom(mark.get('lamports'))
        if value is None:stats['null_marks']+=1
        by_time[at]=dict(time=at,value=(ratio(value,spend)-1)*100 if value is not None else None)
    out['mark_binding_kind_counts']=dict(bindings)
    out['mark_segments'],mark_limited=segments([p for _,p in sorted(by_time.items())])
    if number(closed) and filled<=closed<=now:
        proceeds=atom(row.get('proceeds_lamports'))
        out['exit']=dict(time=closed,value=(ratio(proceeds,spend)-1)*100 if proceeds is not None else None,reason=str(row.get('exit_reason') or 'Unknown')[:160],model_price_sol_per_token=ratio(proceeds*scale,qty*10**9) if proceeds is not None and scale is not None else None)
    out['stats']={k:stats[k] for k in ('swaps_without_block_time','wrong_identity','invalid_reserves','ambiguous_seconds','null_marks','marks_read_unknown')}
    out['limited']=limited or tape_limited or mark_limited or len(marks or [])>POINT_CAP
    out['reason']=('entry_tape_price_zero' if candidate_anchor else 'entry_not_on_tape') if anchor is None else None
    out['state']='known' if anchor and out['mark_segments'] and not any(stats.values()) and policy is not None and not out['limited'] else 'partial'
    return out


def _bucket(rows,size):
    out={}
    for r in rows:
        t=r['time']//size*size;c=out.get(t)
        if c is None:c=out[t]=[t,r['price'],r['price'],r['price'],r['price'],0.,0.,0,0]
        c[2]=max(c[2],r['price']);c[3]=min(c[3],r['price']);c[4]=r['price']
        side=(5,7) if r['is_buy'] else (6,8)
        c[side[0]]=round(c[side[0]]+r['sol'],4);c[side[1]]+=1
    return [out[t] for t in sorted(out)]


def candles(swaps,*,decimals,sol_usd,sol_usd_at,supply_tokens,seed_virtual=None,now=None):
    """OHLC from our own tape on the fills' basis: (post real SOL + virtual) / post base, USD per token. A bucket with no trade is absent, so a gap stays a gap. The virtual quote carries from the newest proven buy; a swap before any proof is counted, never priced."""
    from sniper.services.memecoin_desk_ps_tape import _proof
    base=dict(fields=['t','o','h','l','c','buy_sol','sell_sol','buy_n','sell_n'],basis='(post real SOL + virtual) / post base from our tape; same basis as fills',sol_usd=sol_usd,sol_usd_at=sol_usd_at,supply_tokens=supply_tokens)
    if type(decimals) is not int or not 0<=decimals<=18 or not number(sol_usd):return dict(base,state='unknown',reason='decimals_or_fx_unknown',**{'5s':[],'1m':[]})
    rows=[];virtual=seed_virtual;missing=0;no_volume=0
    for r in sorted(swaps,key=lambda x:(x.get('slot') or 0,x.get('tx_index') or 0,x.get('ordinal') or 0)):
        proof=_proof(r.get('payload_json'))
        if proof is not None:virtual=proof
        bt=r.get('block_time') if type(r.get('block_time')) is int and r.get('block_time')>0 else r.get('joined_block_time')
        q,b,sol=atom(r.get('post_quote_reserve_atomic')),atom(r.get('post_base_reserve_atomic')),atom(r.get('quote_atomic'))
        if type(bt) is not int or bt<=0 or q is None or not b or r.get('is_buy') not in (0,1):continue
        if now is not None and bt>now:continue   # past the read's cut: not part of this view
        if virtual is None:missing+=1;continue
        if sol is None:no_volume+=1   # priced, but buy_sol and sell_sol are lower bounds then
        rows.append(dict(time=bt,price=float('%.6g'%(float(ratio((q+virtual)*10**decimals,b*10**9))*sol_usd)),is_buy=r['is_buy'],sol=(sol or 0)/1e9))
    return dict(base,state='partial' if missing or no_volume or not rows else 'known',reason=None if rows else 'no_priced_swaps',no_virtual_rows=missing,unknown_volume_rows=no_volume,**{'5s':_bucket(rows,5)[-300:],'1m':_bucket(rows,60)[-240:]})


def _slim(out):
    """Over the cap, keep every order: newest 60 of each candle size, and the line layers down to their newest points. Says so."""
    for item in out.get('items',[]):
        c=item.get('candles') or {}
        for size in ('5s','1m'):
            if isinstance(c.get(size),list):c[size]=c[size][-60:]
        for key in ('tape_segments','mark_segments'):
            item[key]=[part[-120:] for part in (item.get(key) or [])[-1:]]
        item.update(limited=True,candles_limited=True)
    out['state']='partial';return out


def price_levels(item,sol_usd):
    """USD per token. Rails fire on sell value after impact, so these are the fill price times each rail: approximate."""
    fill=item.get('model_fill_sol_per_token')
    if not number(fill) or not number(sol_usd):return {}
    entry=fill*sol_usd;out=dict(entry=entry)
    for key,pct in (item.get('levels') or {}).items():
        if key in ('stop','take_profit','catastrophe','trail') and type(pct) in (int,float) and math.isfinite(pct):out[key]=entry*(1+pct/100)
    exit_=(item.get('exit') or {}).get('model_price_sol_per_token')
    if number(exit_):out['exit']=exit_*sol_usd
    return out


def _proof_slot(raw,pool):
    try:
        value=json.loads(raw or '{}');slot=value.get('proof',{}).get('slot')
        return slot if value.get('pool')==pool and type(slot) is int and slot>0 else None
    except (ValueError,TypeError,AttributeError):return None


class OfficeCharts:
    """One immutable snapshot per web process; nonwaiting singleflight, never a runner connection."""
    def __init__(self,root,*,monotonic=time.monotonic):
        self.root=Path(root);self.monotonic=monotonic;self._lock=threading.Lock();self._snapshot=None

    def _copy(self,snapshot,now,*,stale=False,cached=True):
        if now<snapshot[1]:return dict(state='unknown',epoch_id=EPOCH,reason='chart_clock_rolled_back')
        out=json.loads(snapshot[2]);stale=stale or now-snapshot[1]>5
        out.update(cached=cached,cache_age_s=now-snapshot[1],stale=stale)
        if stale and out.get('state')=='known':out['state']='partial'
        return out

    def read(self,*,now):
        if not number(now):return dict(state='unknown',epoch_id=EPOCH,reason='chart_clock_unknown')
        snapshot=self._snapshot
        if snapshot is not None and self.monotonic()-snapshot[0]<5:return self._copy(snapshot,now)
        if not self._lock.acquire(blocking=False):
            return self._copy(snapshot,now,stale=True) if snapshot else dict(state='unknown',epoch_id=EPOCH,reason='chart_read_in_progress')
        try:
            snapshot=self._snapshot
            if snapshot is not None and self.monotonic()-snapshot[0]<5:return self._copy(snapshot,now)
            out=self._load(now=now)
            from .operator_view import _display_symbols
            if out.get('items'):_display_symbols(self.root,out['items'],now)
            encoded=json.dumps(out,allow_nan=False,separators=(',',':'))
            if len(encoded.encode())>524288:encoded=json.dumps(_slim(out),allow_nan=False,separators=(',',':'))
            if len(encoded.encode())>524288:encoded=json.dumps(dict(state='unknown',epoch_id=EPOCH,reason='chart_output_limit'))
            # Atomic publication prevents readers mixing another snapshot's age, time or payload.
            snapshot=(self.monotonic(),now,encoded);self._snapshot=snapshot
            return self._copy(snapshot,now,cached=False)
        finally:self._lock.release()

    def _load(self,*,now):
        deadline=self.monotonic()+.3;items=[];open_total=0;open_limited=False;missing=[]
        columns='order_id,epoch_id,book_kind,network_id,mint,pool,state,ticker,decimals,filled_at,closed_at,spend_lamports,tokens_atomic,peak_lamports,proceeds_lamports,exit_reason,policy_json,policy_hash,fill_proof,exit_proof,entry_unit_price_usd'
        for epoch,path in BOOKS:
            try:
                with _ro(self.root/path) as ledger:
                    if ledger.execute('SELECT 1 FROM ledger_epochs WHERE epoch_id=? LIMIT 1',(epoch,)).fetchone() is None:raise ValueError('epoch_unknown')
                    opened=[dict(r) for r in ledger.execute('SELECT '+columns+" FROM orders WHERE epoch_id=? AND book_kind='evaluated' AND state='open' ORDER BY filled_at DESC LIMIT 4",(epoch,))]
                    closed=[dict(r) for r in ledger.execute('SELECT '+columns+" FROM orders WHERE epoch_id=? AND book_kind='evaluated' AND state='closed' AND closed_at>=? AND closed_at<=? ORDER BY closed_at DESC LIMIT 2",(epoch,now-CLOSED_S,now))]
                    open_total+=len(opened[:3]);open_limited=open_limited or len(opened)>3
                    for row in opened[:3]+closed:
                        marks=[dict(r) for r in ledger.execute('SELECT id,at,lamports,reason,binding_kind FROM marks WHERE order_id=? AND at>=? AND at<=? ORDER BY at DESC,id DESC LIMIT 601',(row['order_id'],row['filled_at'],min(row['closed_at'] or now,now)))]
                        marks.reverse();items.append((row,marks))
            except (OSError,sqlite3.Error,ValueError,TypeError):
                if epoch==EPOCH:return dict(state='unknown',epoch_id=EPOCH,as_of=now,reason='chart_ledger_unavailable')
                missing.append(epoch)   # the main book still charts; the other book reads unknown, never empty
        from .operator_view import _display_fx
        try:fx=_display_fx(self.root)
        except (OSError,ValueError,TypeError):fx={}
        self._seeds={}
        out=[]
        for row,marks in items:
            swaps=[];limited=len(marks)>POINT_CAP;source_error=None
            if self.monotonic()>deadline:source_error='chart_read_budget'
            else:
                try:swaps,tail_limited=self._swaps(row,now);limited=limited or tail_limited
                except (OSError,sqlite3.Error,ValueError,TypeError):source_error='chart_tape_unavailable'
            item=project_order(row,marks,swaps,now=now,limited=limited)
            pool_state=self._frozen_pool(row['order_id'])
            supply=atom(pool_state.get('supply_atomic'));dec=row.get('decimals')
            item['candles']=candles(swaps,now=now,decimals=dec,sol_usd=fx.get('sol_usd'),sol_usd_at=fx.get('sol_at'),seed_virtual=self._seeds.get(row['pool']),
                                    supply_tokens=supply/10**dec if supply and type(dec) is int and 0<=dec<=18 else None)
            item['price_levels']=price_levels(item,fx.get('sol_usd'))
            if source_error:item.update(state='partial',reason=source_error)
            out.append(dict(item,book_id=row['epoch_id']))
        from .operator_view import _display_symbols
        names=[dict(mint=r['mint'],network_id=r['network_id'],ticker=r['ticker']) for r,_ in items]
        _display_symbols(self.root,names,now)
        for item,name in zip(out,names):
            if name.get('display_ticker'):item['ticker']=str(name['display_ticker'])[:128]
            item['ticker_source']=name.get('display_ticker_source') or 'unknown'
        return dict(state='known' if not open_limited and not missing and all(r['state']=='known' for r in out) else 'partial',epoch_id=EPOCH,as_of=now,
                    open_count=open_total if not open_limited and not missing else None,open_limited=open_limited,books_unavailable=missing,items=out,gap_s=GAP_S,
                    coverage='At most 3 open and 2 closed in the last 10 minutes; 2000 recent swap rows plus 100 anchor rows; 600 points and 16 segments per layer. Logical ledger/tape cut, not atomic.')

    def _frozen_pool(self,order_id):
        """The sheet's pool state frozen at the order; {} when unknown."""
        try:
            with _ro(self.root/'office.db') as store:
                bundle=store.execute('SELECT payload_json FROM decision_bundles WHERE decision_id=? ORDER BY seq DESC LIMIT 1',(order_id,)).fetchone()
                hit=store.execute('SELECT payload_json FROM receipts WHERE row_hash=?',(json.loads(bundle[0])['receipt_row_hash'],)).fetchone() if bundle else None
            return json.loads(hit[0])['sheet'].get('pool_state') or {} if hit else {}
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,AttributeError):return {}

    def _swaps(self,row,now):
        path=self.root.parent/'desk/pumpswap/tape.db'
        with _ro(path) as tape:
            # All predicates use the producer's pool-slot index. Receipt/batch timestamps never become chart x coordinates.
            columns='s.pool,s.mint,s.quote_mint,s.slot,s.signature,s.tx_index,s.ordinal,s.block_time,s.received_at,s.pre_base_reserve_atomic,s.pre_quote_reserve_atomic,s.post_base_reserve_atomic,s.post_quote_reserve_atomic,s.is_buy,s.quote_atomic,s.payload_json'
            base='SELECT '+columns+' FROM swaps_ps AS s INDEXED BY swaps_ps_pool_slot WHERE s.pool=?'
            end_slot=_proof_slot(row.get('exit_proof'),row['pool']) if row.get('state')=='closed' else None
            where=' AND s.slot<=?' if end_slot is not None else ''
            params=(row['pool'],end_slot) if end_slot is not None else (row['pool'],)
            recent=[dict(r) for r in tape.execute(base+where+' ORDER BY s.slot DESC LIMIT 2001',params)]
            limited=len(recent)>SWAP_CAP
            if limited:
                boundary=min(r['slot'] for r in recent if type(r['slot']) is int)
                recent=[r for r in recent if type(r['slot']) is int and r['slot']>boundary][:SWAP_CAP]
            anchor_slot=_proof_slot(row.get('fill_proof'),row['pool'])
            anchor=[dict(r) for r in tape.execute(base+' AND s.slot<=? ORDER BY s.slot DESC LIMIT 100',(row['pool'],anchor_slot))] if anchor_slot is not None else []
            # Unmapped legacy swaps may use this exact pool's on-chain creation identity; never a mint-wide substitute.
            # Bounded by index and row count, never a text scan: the virtual quote just before the oldest row read. No proof in reach stays unknown.
            oldest=min((r['slot'] for r in recent+anchor if type(r.get('slot')) is int),default=None)
            if oldest is not None:
                from sniper.services.memecoin_desk_ps_tape import _proof
                for prior, in tape.execute('SELECT payload_json FROM swaps_ps INDEXED BY swaps_ps_pool_slot WHERE pool=? AND slot<? AND is_buy=1 ORDER BY slot DESC LIMIT 20',(row['pool'],oldest)):
                    if (seed:=_proof(prior)) is not None:self._seeds[row['pool']]=seed;break
            maps=[tuple(r) for r in tape.execute('SELECT mint,quote_mint FROM pools INDEXED BY pools_pool WHERE pool=? LIMIT 33',(row['pool'],))]
            identity=set(maps)
            if len(maps)<=32 and identity=={(row['mint'],WSOL)}:
                for r in recent+anchor:
                    if r['mint'] is None:r['mint']=row['mint']
                    if r['quote_mint'] is None:r['quote_mint']=WSOL
        unique={tuple(r.get(k) for k in ('slot','signature','ordinal')):r for r in recent+anchor}
        rows=list(unique.values())
        dates={datetime.fromtimestamp(t,timezone.utc).date().isoformat() for t in (now,row.get('filled_at'),*(r.get('received_at') for r in rows)) if number(t)}
        clocks={};slots=sorted({r['slot'] for r in rows if type(r.get('slot')) is int and r['slot']>0})
        # The producer writes block metadata to day files. Receipt time chooses files only, never the plotted time.
        for day in sorted(dates,reverse=True)[:3]:
            block_path=self.root.parent/'desk/blocks'/(day+'.db')
            if not block_path.is_file():continue
            try:
                with _ro(block_path) as blocks:
                    for start in range(0,len(slots),400):
                        batch=slots[start:start+400]
                        for slot,stamp in blocks.execute('SELECT slot,block_time FROM blocks WHERE slot IN ('+','.join('?' for _ in batch)+')',tuple(batch)):
                            if type(stamp) is int and stamp>0:
                                if slot in clocks and clocks[slot]!=stamp:clocks[slot]=None
                                else:clocks[slot]=stamp
            except (OSError,sqlite3.Error,ValueError):continue
        for r in rows:r['joined_block_time']=clocks.get(r.get('slot'))
        return rows,limited
