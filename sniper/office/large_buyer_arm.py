"""Host-side paper arm for Gamma's frozen rule, docs/audits/2026-10-31-large-buyer-operator-scorecard-preregistration.md, reading A. No LLM, no seat, no funded path, no rails, no tuning.
A wallet's cumulative buys in a pool first reach 25 SOL with no earlier sell by it there: enter 10 s after that print, exit 300 s after entry. Exact tape quotes for one fixed clip. One row per trigger.
Wallet history is never deleted, so an old sell is never forgotten; it starts when this process first ran: a sell or buy before that is unseen, so run it before START_AT. Own file, never pooled."""
import math
import sqlite3
import time
from types import SimpleNamespace

from . import ledger as L

BOOK='office-large-buyer-a-1'
START_AT=1793491200.    # 2026-11-01 00:00 UTC, the first test day
TRIGGER_LAMPORTS=25*10**9
ENTRY_DELAY_S=10.
HOLD_S=300.
GIVE_UP_S=30.           # the prereg's censoring window on either time
CLIP_LAMPORTS=25*10**7  # 0.25 SOL; sizes every row alike and is not part of the rule
COST_PCT=3.
WSOL='So11111111111111111111111111111111111111112'
OVERLAP_S=60.           # tape writers commit after received_at: re-read, dedupe on signature
BATCH=50000
# The twin: same entries, the Office's normal rails (config/office/book_policy_pilot.json, frozen here) instead of the fixed exit.
RAILS=SimpleNamespace(sl_pct=30.,tp_pct=100.,trail_pct=20.,catastrophe_pct=60.,max_hold_s=3600.,lag_s=None)
# Exit variants on the same entries; the fixed 300 s exit lives on the trigger row. A variant added here sees only later entries, and is frozen by a dated prereg amendment first.
# Separate booked paper, never pooled with Gamma's print-path reads: state at the time from the tape, polled every 3 s. Amendment 2: exit booked at trigger + 10 s, 30 s printed beside it as stress, censored 60 s after either. Marks every MARK_EVERY_S, so a print between marks is not seen here; Gamma's read is on prints.
_TP=dict(tp_pct=40.,trail_pct=0.,catastrophe_pct=0.,max_hold_s=300.,lag_s=10.)
VARIANTS=dict(rails=RAILS,tp40=SimpleNamespace(sl_pct=0.,**_TP),tp40_sl30=SimpleNamespace(sl_pct=30.,**_TP))
STRESS_LAG_S=30.
CROWD_WINDOW_S=60.
LAG_CENSOR_S=60.
MAX_DETECT_LAG_S=5.     # ROOT frozen CROWD policy; host receipt to producer detection, never source block time
MARK_EVERY_S=3.
EXIT_STRICT_S=10.        # as the ledger: strict from the request this long, then the labelled estimate at landing
EXIT_LANDING_S=3.
EXIT_REQUEST_MAX_S=120.
DDL="""
CREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,exit_elapsed_s REAL,stress_at REAL,stress_elapsed_s REAL,PRIMARY KEY(pool,wallet,variant));
CREATE INDEX IF NOT EXISTS twins_state ON twins(state);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);
CREATE TABLE IF NOT EXISTS ingest_progress(id INTEGER PRIMARY KEY CHECK(id=1),lo REAL NOT NULL,hi REAL NOT NULL,last_at REAL,last_slot,last_index,last_ordinal,last_signature TEXT);
CREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS seen_at ON seen(at);
CREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,entry_elapsed_s REAL,exit_elapsed_s REAL,buyer_first_sell_at REAL,others_60s INTEGER,PRIMARY KEY(pool,wallet));
CREATE TABLE IF NOT EXISTS reaches(pool TEXT,at REAL,wallet TEXT,PRIMARY KEY(pool,at,wallet)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);
"""


def open_db(path):
    conn=sqlite3.connect(path,timeout=5);conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL');conn.executescript(DDL)
    # Older arm files keep their rows; missing observations are NULL, never reconstructed.
    upgrades={
        'triggers':{'history_state':'TEXT','history_reason':'TEXT','crowd_others_60s':'INTEGER','crowd_state':'TEXT','crowd_reason':'TEXT','source_slot':'INTEGER','source_tx_index':'INTEGER','source_ordinal':'INTEGER','source_signature':'TEXT','detect_lag_s':'REAL','detection_state':'TEXT','pool_quote_before':'TEXT','post_quote':'TEXT','post_base':'TEXT','entry_elapsed_s':'REAL','exit_elapsed_s':'REAL','buyer_first_sell_at':'REAL','others_60s':'INTEGER'},
        'wallets':{'crowd_bought':'TEXT','crowd_sold':'INTEGER','crowd_slot':'INTEGER','crowd_tx_index':'INTEGER','crowd_ordinal':'INTEGER','crowd_signature':'TEXT'},
        'reaches':{'source_slot':'INTEGER','source_tx_index':'INTEGER','source_ordinal':'INTEGER','source_signature':'TEXT','crowd_state':'TEXT'},
        'twins':{'exit_elapsed_s':'REAL','stress_at':'REAL','stress_elapsed_s':'REAL'},
    }
    try:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            for table,columns in upgrades.items():
                existing={r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
                for column,kind in columns.items():
                    if column not in existing:conn.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+kind)
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('arm_history_start_host',?)",(time.time(),))
    except Exception:
        conn.close();raise
    return conn


def _source_key(slot,index,ordinal,signature):
    # Stored PS rows have no explicit proto-presence witness for index zero.
    return (slot,index,ordinal,signature) if type(slot) is int and slot>0 and type(index) is int and index>0 and type(ordinal) is int and ordinal>=0 and isinstance(signature,str) and signature else None


def _before(left,right):
    if left is None or right is None:return None
    if left[3]==right[3]:return left[0]==right[0] and left[1]==right[1] and left[2]<right[2]
    return left[:2]<right[:2]


def _order_known(tape,pool,at,key,*,pool_slot_index=False):
    if key is None:return False
    index=' INDEXED BY swaps_ps_pool_slot' if pool_slot_index else ''
    rows=tape.execute('SELECT signature,tx_index,ordinal FROM swaps_ps'+index+' WHERE pool=? AND slot=? AND received_at<=? LIMIT ?',(pool,key[0],at,BATCH+1)).fetchall()
    if len(rows)>BATCH:return False
    indexes={};signatures={};events=set()
    for signature,index,ordinal in rows:
        if _source_key(key[0],index,ordinal,signature) is None or (signature,ordinal) in events:return False
        if index in indexes and indexes[index]!=signature:return False
        if signature in signatures and signatures[signature]!=index:return False
        indexes[index]=signature;signatures[signature]=index;events.add((signature,ordinal))
    return True


def _crowd_count(conn,pool,actor,at,key):
    count=0
    for r in conn.execute('SELECT * FROM reaches WHERE pool=? AND at>=? AND at<=? AND wallet!=?',(pool,at-CROWD_WINDOW_S,at,actor)):
        earlier=_before(_source_key(r['source_slot'],r['source_tx_index'],r['source_ordinal'],r['source_signature']),key)
        if earlier is None:return None,'prior_order_unknown'
        if earlier:
            if r['crowd_state']!='known':return None,'prior_qualification_unknown'
            count+=1
    return count,None


def ingest(conn,tape,now,*,detected_now=None,should_stop=None,should_yield=None):
    """Receipt-order history, with separate causal CROWD proof; no quotes or funded writes."""
    if type(now) not in (int,float) or not math.isfinite(now):raise ValueError('host_clock_unknown')
    row=conn.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
    cursor=row[0] if row else None
    progress=conn.execute('SELECT * FROM ingest_progress WHERE id=1').fetchone()
    lo=progress['lo'] if progress else (cursor if cursor is not None else now)-OVERLAP_S
    hi=progress['hi'] if progress else now
    if hi>now:raise ValueError('ingest_clock_regressed')
    fetch_limit=min(BATCH,2000) if should_yield is not None else BATCH
    last=tuple(progress[name] for name in ('last_at','last_slot','last_index','last_ordinal','last_signature')) if progress and progress['last_at'] is not None else None
    order="received_at,COALESCE(slot,-1),COALESCE(tx_index,-1),COALESCE(ordinal,-1),COALESCE(signature,'')"
    select='SELECT signature,ordinal,pool,mint,actor,is_buy,quote_atomic,quote_mint,received_at,pre_quote_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic,slot,tx_index FROM swaps_ps '
    if last is None:
        rows=tape.execute(select+'WHERE received_at>? AND received_at<=? ORDER BY '+order+' LIMIT ?',(lo,hi,fetch_limit)).fetchall()
    else:
        rows=tape.execute(select+'WHERE received_at>=? AND received_at<=? AND ('+order+')>(?,?,?,?,?) ORDER BY '+order+' LIMIT ?',(last[0],hi,*last,fetch_limit)).fetchall()
    pool_slot_index=tuple(r[2] for r in tape.execute('PRAGMA index_info(swaps_ps_pool_slot)'))==('pool','slot')
    fired=0;newest=cursor;delays=[];fresh=0;stale=0;processed=0;scanned=0;yielded=False
    with conn:
        for r in rows:
            if should_stop is not None and should_stop():raise ValueError('ingest_budget_exhausted')
            if processed and should_yield is not None and should_yield():yielded=True;break
            sig,ordinal,pool,mint,actor,is_buy,quote,quote_mint,at,pre_q,post_q,post_b,slot,index=r
            if type(at) not in (int,float) or not math.isfinite(at):raise ValueError('receipt_time_unknown')
            newest=max(newest,at) if newest is not None else at
            last=(at,slot if slot is not None else -1,index if index is not None else -1,ordinal if ordinal is not None else -1,sig or '')
            scanned+=1
            if conn.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?)',(sig,ordinal,at)).rowcount==0:continue
            processed+=1
            if quote_mint!=WSOL or not actor or not str(quote).isdigit():continue
            previous=conn.execute('SELECT * FROM wallets WHERE pool=? AND wallet=?',(pool,actor)).fetchone()
            key=_source_key(slot,index,ordinal,sig)
            proof=type(is_buy) is int and is_buy in (0,1) and _order_known(tape,pool,at,key,pool_slot_index=pool_slot_index)
            if previous is not None:
                prior=_source_key(previous['crowd_slot'],previous['crowd_tx_index'],previous['crowd_ordinal'],previous['crowd_signature'])
                proof=proof and previous['crowd_bought'] is not None and _before(prior,key) is True
            bought=(int(previous['crowd_bought']) if previous else 0)+(int(quote) if is_buy else 0) if proof else None
            sold=(previous['crowd_sold'] if previous else 0) if proof else None
            if not is_buy:
                conn.execute('INSERT INTO wallets(pool,wallet,sold,last_at) VALUES(?,?,1,?) ON CONFLICT DO UPDATE SET sold=1,last_at=excluded.last_at',(pool,actor,at))
                conn.execute('UPDATE triggers SET buyer_first_sell_at=? WHERE pool=? AND wallet=? AND buyer_first_sell_at IS NULL',(at,pool,actor))
                if proof:sold=1
            else:
                conn.execute('INSERT INTO wallets(pool,wallet,bought,last_at) VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET bought=bought+excluded.bought,last_at=excluded.last_at',(pool,actor,int(quote),at))
            conn.execute('UPDATE wallets SET crowd_bought=?,crowd_sold=?,crowd_slot=?,crowd_tx_index=?,crowd_ordinal=?,crowd_signature=? WHERE pool=? AND wallet=?',(str(bought) if bought is not None else None,sold,slot,index,ordinal,sig,pool,actor))
            if not is_buy:continue
            w=conn.execute('SELECT bought,sold,fired FROM wallets WHERE pool=? AND wallet=?',(pool,actor)).fetchone()
            if w['bought']>=TRIGGER_LAMPORTS and not w['fired']:
                conn.execute('UPDATE wallets SET fired=1 WHERE pool=? AND wallet=?',(pool,actor))
                if not w['sold']:
                    # Keep the measured historical split unchanged; funded CROWD uses only crowd_* proof.
                    others=conn.execute('SELECT count(*) FROM reaches WHERE pool=? AND at>=? AND at<? AND wallet!=?',(pool,at-CROWD_WINDOW_S,at,actor)).fetchone()[0]
                    qualified=proof and bought>=TRIGGER_LAMPORTS and sold==0
                    crowd,why=_crowd_count(conn,pool,actor,at,key) if qualified else (None,'own_qualification_unknown')
                    conn.execute('INSERT OR IGNORE INTO reaches(pool,at,wallet,source_slot,source_tx_index,source_ordinal,source_signature,crowd_state) VALUES(?,?,?,?,?,?,?,?)',(pool,at,actor,slot,index,ordinal,sig,'known' if qualified else 'unknown'))
                    if at>=START_AT:
                        detected=detected_now() if detected_now is not None else now
                        if type(detected) not in (int,float) or not math.isfinite(detected):raise ValueError('detection_clock_unknown')
                        lag=detected-at;detection='known' if 0<=lag<=MAX_DETECT_LAG_S else 'stale' if lag>MAX_DETECT_LAG_S else 'unknown'
                        conn.execute("INSERT OR IGNORE INTO triggers(pool,wallet,mint,signature,trigger_at,detected_at,state,entry_due,pool_quote_before,post_quote,post_base,others_60s,crowd_others_60s,crowd_state,crowd_reason,source_slot,source_tx_index,source_ordinal,source_signature,detect_lag_s,detection_state,history_state,history_reason) VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unknown','complete_pool_history_unproven')",(pool,actor,mint,sig,at,detected,at+ENTRY_DELAY_S,pre_q,post_q,post_b,others,crowd,'known' if crowd is not None else 'unknown',why,slot,index,ordinal,sig,lag,detection))
                        fired+=1;delays.append(lag);fresh+=detection=='known';stale+=detection=='stale'
        if should_stop is not None and should_stop():raise ValueError('ingest_budget_exhausted')
        complete=not yielded and len(rows)<fetch_limit
        if complete:conn.execute('DELETE FROM ingest_progress WHERE id=1')
        elif last is not None:conn.execute('INSERT OR REPLACE INTO ingest_progress VALUES(1,?,?,?,?,?,?,?)',(lo,hi,*last))
        if newest is not None:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('cursor',?)",(newest,))
            conn.execute('DELETE FROM seen WHERE at<?',(newest-2*OVERLAP_S,))
        if should_stop is not None and should_stop():raise ValueError('ingest_budget_exhausted')
    delays.sort()
    return dict(rows=scanned,fired=fired,source_rows=len(rows),processed=processed,yielded=yielded,partial=not complete,cursor_at=newest,lag_s=now-newest if newest is not None else None,fresh_fired=fresh,stale_fired=stale,detect_lag_p50_s=delays[len(delays)//2] if delays else None,detect_lag_p90_s=delays[min(len(delays)-1,math.ceil(len(delays)*.9)-1)] if delays else None)


def grade(conn,desk_dir,now):
    """Exact quotes at the frozen times. No proof within GIVE_UP_S is censored and counted, never priced later."""
    n=dict(entered=0,closed=0,censored=0)
    clock=dict(offset_s=0.,sampled_at=now)    # every time here is the tape's own host clock
    for t in conn.execute("SELECT * FROM triggers WHERE state='pending' AND entry_due<=?",(now,)).fetchall():
        quote,why=L._quote(L._ps_buy,desk_dir,t['mint'],CLIP_LAMPORTS,t['entry_due'],now,clock)
        if quote is not None and now-t['entry_due']>GIVE_UP_S:quote,why=None,'deadline_passed'
        if quote is not None and (quote.get('pool')!=t['pool'] or not str(quote.get('tokens_atomic','')).isdigit()):quote,why=None,'other_pool'
        with conn:
            if quote is not None:
                conn.execute("UPDATE triggers SET state='open',entry_at=?,entry_elapsed_s=?,tokens_atomic=?,exit_due=? WHERE pool=? AND wallet=?",(now,now-t['entry_due'],str(quote['tokens_atomic']),t['entry_due']+HOLD_S,t['pool'],t['wallet']));n['entered']+=1
                for variant in VARIANTS:conn.execute("INSERT OR IGNORE INTO twins(pool,wallet,variant,mint,state,spend_lamports,tokens_atomic,filled_at,peak_lamports) VALUES(?,?,?,?,'open',?,?,?,?)",(t['pool'],t['wallet'],variant,t['mint'],str(CLIP_LAMPORTS),str(quote['tokens_atomic']),t['entry_due'],str(CLIP_LAMPORTS)))
            elif now-t['entry_due']>GIVE_UP_S:
                conn.execute("UPDATE triggers SET state='censored',reason=? WHERE pool=? AND wallet=?",('entry:'+str(why),t['pool'],t['wallet']));n['censored']+=1
            else:conn.execute('UPDATE triggers SET entry_tries=entry_tries+1 WHERE pool=? AND wallet=?',(t['pool'],t['wallet']))
    for t in conn.execute("SELECT * FROM triggers WHERE state='open' AND exit_due<=?",(now,)).fetchall():
        quote,why=L._quote(L._ps_sell,desk_dir,t['mint'],int(t['tokens_atomic']),t['exit_due'],now,clock)
        if quote is not None and now-t['exit_due']>GIVE_UP_S:quote,why=None,'deadline_passed'
        if quote is not None and (quote.get('pool')!=t['pool'] or not str(quote.get('lamports','')).isdigit()):quote,why=None,'other_pool'
        with conn:
            if quote is not None:
                gross=(int(quote['lamports'])/CLIP_LAMPORTS-1)*100
                conn.execute("UPDATE triggers SET state='closed',exit_at=?,exit_elapsed_s=?,proceeds_lamports=?,gross_pct=?,net_pct=? WHERE pool=? AND wallet=?",(now,now-t['exit_due'],quote['lamports'],gross,gross-COST_PCT,t['pool'],t['wallet']));n['closed']+=1
            elif now-t['exit_due']>GIVE_UP_S:
                conn.execute("UPDATE triggers SET state='censored',reason=? WHERE pool=? AND wallet=?",('exit:'+str(why),t['pool'],t['wallet']));n['censored']+=1
            else:conn.execute('UPDATE triggers SET exit_tries=exit_tries+1 WHERE pool=? AND wallet=?',(t['pool'],t['wallet']))
    return n


def _close_twin(conn,t,lamports,now,kind):
    gross=(int(lamports)/CLIP_LAMPORTS-1)*100
    conn.execute("UPDATE twins SET state='closed',exit_at=?,exit_elapsed_s=?,exit_fill_kind=?,proceeds_lamports=?,gross_pct=?,net_pct=? WHERE pool=? AND wallet=? AND variant=?",(now,now-t['exit_due'],kind,str(lamports),gross,gross-COST_PCT,t['pool'],t['wallet'],t['variant']))


def twin_tick(conn,desk_dir,now):
    """The ledger's rail function on the twin's marks, its exit timing and its labelled estimate. Reads only the tape; never touches the fixed-exit rows."""
    n=dict(marked=0,requested=0,twin_closed=0,twin_censored=0)
    marks={}
    clock=dict(offset_s=0.,sampled_at=now)
    for row in conn.execute("SELECT * FROM twins WHERE state='open'").fetchall():
        t=dict(row);qty=int(t['tokens_atomic']);value=None;why=None
        if t['last_mark_at'] is not None and now-t['last_mark_at']<MARK_EVERY_S:continue
        committed=L._committed_cut(desk_dir,clock)
        if committed is not None:
            if (t['mint'],qty) not in marks:      # variants share the entry, so one read per coin per tick
                cut=min(now,committed)
                quote,why=L._quote(L._ps_sell,desk_dir,t['mint'],qty,cut,now,clock)
                if quote is None:quote=L._ps_estimate(desk_dir,t['mint'],qty,cut,observed_at=now)[0]   # as the ledger: rails do not go blind, a mark and never a fill
                marks[(t['mint'],qty)]=int(quote['lamports']) if quote is not None and quote.get('pool')==t['pool'] and str(quote.get('lamports','')).isdigit() else None
            value=marks[(t['mint'],qty)]
        with conn:
            if value is None:conn.execute('UPDATE twins SET marks_null=marks_null+1,last_mark_at=? WHERE pool=? AND wallet=? AND variant=?',(now,t['pool'],t['wallet'],t['variant']))
            else:
                t['peak_lamports']=str(max(value,int(t['peak_lamports'])));n['marked']+=1
                conn.execute('UPDATE twins SET peak_lamports=?,last_mark_at=? WHERE pool=? AND wallet=? AND variant=?',(t['peak_lamports'],now,t['pool'],t['wallet'],t['variant']))
            reason=L._rail(VARIANTS[t['variant']],t,value,now)
            if reason:
                conn.execute("UPDATE twins SET state='exiting',requested_at=?,exit_reason=?,exit_due=? WHERE pool=? AND wallet=? AND variant=?",(now,reason,now+(VARIANTS[t['variant']].lag_s or L.EXIT_DELAY_S),t['pool'],t['wallet'],t['variant']));n['requested']+=1
    for row in conn.execute("SELECT * FROM twins WHERE state='exiting' AND exit_due<=?",(now,)).fetchall():
        t=dict(row);qty=int(t['tokens_atomic'])
        quote,why=L._quote(L._ps_sell,desk_dir,t['mint'],qty,t['exit_due'],now,clock)
        if quote is not None and VARIANTS[t['variant']].lag_s and now-t['exit_due']>LAG_CENSOR_S:quote,why=None,'deadline_passed'
        if VARIANTS[t['variant']].lag_s:
            with conn:
                if quote is not None and quote.get('pool')==t['pool'] and str(quote.get('lamports','')).isdigit():
                    _close_twin(conn,t,quote['lamports'],now,'strict');n['twin_closed']+=1
                elif now-t['exit_due']>LAG_CENSOR_S:
                    conn.execute("UPDATE twins SET state='censored',reason=? WHERE pool=? AND wallet=? AND variant=?",('exit:'+str(why),t['pool'],t['wallet'],t['variant']));n['twin_censored']+=1
            continue
        with conn:
            if quote is not None and quote.get('pool')==t['pool'] and str(quote.get('lamports','')).isdigit():
                _close_twin(conn,t,quote['lamports'],now,'strict');n['twin_closed']+=1;continue
            if now-t['requested_at']<=EXIT_STRICT_S:continue
            cut=t['requested_at']+EXIT_LANDING_S
            estimate=L._ps_estimate(desk_dir,t['mint'],qty,cut,observed_at=cut)[0]
            if estimate is not None and estimate.get('pool')==t['pool'] and str(estimate.get('lamports','')).isdigit():
                _close_twin(conn,t,estimate['lamports'],now,'estimated_at_landing');n['twin_closed']+=1
            elif now-t['requested_at']>EXIT_REQUEST_MAX_S:
                conn.execute("UPDATE twins SET state='censored',reason=? WHERE pool=? AND wallet=? AND variant=?",('exit:'+str(why),t['pool'],t['wallet'],t['variant']));n['twin_censored']+=1
    for row in conn.execute("SELECT * FROM twins WHERE variant!='rails' AND requested_at IS NOT NULL AND stress_gross_pct IS NULL AND stress_reason IS NULL AND requested_at+?<=?",(STRESS_LAG_S,now)).fetchall():
        t=dict(row);cut=t['requested_at']+STRESS_LAG_S
        quote,why=L._quote(L._ps_sell,desk_dir,t['mint'],int(t['tokens_atomic']),cut,now,clock)
        if quote is not None and now-cut>LAG_CENSOR_S:quote,why=None,'deadline_passed'
        with conn:
            if quote is not None and quote.get('pool')==t['pool'] and str(quote.get('lamports','')).isdigit():
                conn.execute('UPDATE twins SET stress_gross_pct=?,stress_at=?,stress_elapsed_s=? WHERE pool=? AND wallet=? AND variant=?',((int(quote['lamports'])/CLIP_LAMPORTS-1)*100,now,now-cut,t['pool'],t['wallet'],t['variant']))
            elif now-cut>LAG_CENSOR_S:
                conn.execute('UPDATE twins SET stress_reason=? WHERE pool=? AND wallet=? AND variant=?',(str(why),t['pool'],t['wallet'],t['variant']))
    return n


def report(conn):
    """Counts only until Gamma's first read: 300 usable rows and 3 UTC days."""
    states={r[0]:r[1] for r in conn.execute('SELECT state,count(*) FROM triggers GROUP BY 1')}
    reasons={r[0]:r[1] for r in conn.execute("SELECT reason,count(*) FROM triggers WHERE state='censored' GROUP BY 1")}
    delay=[r[0] for r in conn.execute('SELECT detected_at-trigger_at FROM triggers ORDER BY 1')]
    twins={r[0]+(':'+r[1] if r[1] else ''):r[2] for r in conn.execute('SELECT variant||":"||state,exit_fill_kind,count(*) FROM twins GROUP BY 1,2')}
    return dict(book=BOOK,twin_states=twins,states=states,censored_reasons=reasons,detect_delay_p50_s=delay[len(delay)//2] if delay else None,note='counts only until the preregistered read; paper quotes, no landing')
