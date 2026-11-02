"""Plain host facts for seats: how far a coin has run, who else closed it, and what should wake a Judge on an open position. Read-only and bounded; reported, never a gate."""
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import time

from sniper.services.memecoin_desk_ps_tape import _proof

ROWS=2000
READ_BUDGET_S=.25   # 2000 rows with payloads measured 43 ms p50, 129 p90, 195 max warm, more cold (2026-10-31); .1 interrupted 17 of 25 live pools on a cold pass
FLOW_WINDOW_S=30
WATCH_FALL_PCT=15.
HOLDER_SOLD_SHARE=.5


def _tape(desk_dir):
    conn=sqlite3.connect(Path(desk_dir,'pumpswap/tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
    deadline=time.monotonic()+READ_BUDGET_S
    conn.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
    return conn


def _rows(conn,pool,now_host):
    """Newest rows at or before the read, oldest first."""
    rows=conn.execute('SELECT received_at,actor,is_buy,quote_atomic,post_base_reserve_atomic,post_quote_reserve_atomic,payload_json FROM swaps_ps INDEXED BY swaps_ps_pool_received WHERE pool=? AND received_at<=? ORDER BY received_at DESC LIMIT ?',(pool,now_host,ROWS)).fetchall()
    return rows[::-1],len(rows)>=ROWS


def _prices(rows):
    out=[];virtual=None
    for at,_a,_b,_q,base,quote,payload in rows:
        proof=_proof(payload)
        if proof is not None:virtual=proof
        try:
            if virtual is not None and base and quote is not None and int(base)>0:out.append((at,(int(quote)+virtual)/int(base)))
        except (TypeError,ValueError):continue
    return out


def run_up(desk_dir,pool,now_host):
    """Price against the first retained print and the retained low and high, age since the high. First print means first we hold, not launch; short retained tape reads partial."""
    try:
        with closing(_tape(desk_dir)) as conn:rows,limited=_rows(conn,pool,now_host)
    except sqlite3.OperationalError as exc:return dict(state='unknown',reason='read_budget_exceeded' if 'interrupted' in str(exc) else 'tape_unreadable',budget_s=READ_BUDGET_S)   # our own limit is not an unreadable tape
    except (OSError,sqlite3.Error):return dict(state='unknown',reason='tape_unreadable')
    prices=_prices(rows)
    if not prices:return dict(state='unknown',reason='no_priced_tape')
    now=prices[-1][1];values=[p for _,p in prices];peak=max(prices,key=lambda r:r[1])
    pct=lambda ref:round((now/ref-1)*100,1) if ref>0 else None
    return dict(state='partial' if limited else 'known',vs_first_retained_print_pct=pct(values[0]),vs_low_pct=pct(min(values)),vs_high_pct=pct(peak[1]),s_since_high=round(now_host-peak[0]),
                span_s=round(prices[-1][0]-prices[0][0]),prints=len(prices),note='retained tape only; first print is the first we hold, not launch')


def closes_on_coin(ledgers,mint,now,limit=5):
    """Closes of this coin in every Judge book, newest first. A book that cannot be read is named, not treated as no closes."""
    items=[];unread=[]
    for path in ledgers or ():
        try:
            conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
            with closing(conn):
                for epoch,reason,closed,spend,exit_usd,costs in conn.execute("SELECT epoch_id,exit_reason,closed_at,spend_usd,exit_usd,costs_usd FROM orders WHERE mint=? AND state='closed' AND closed_at<=?",(mint,now)):
                    items.append(dict(book=epoch,exit_reason=str(reason)[:80],s_ago=round(now-closed),net_pct=round(((exit_usd-(costs or 0))/spend-1)*100,1) if spend and exit_usd is not None else None))
        except (OSError,sqlite3.Error):unread.append(Path(path).parent.name)
    items.sort(key=lambda i:i['s_ago'])
    return dict(state='partial' if unread else 'known',items=items[:limit],unread=unread)


def triggers(order,marks,desk_dir,now_host,fall_pct=WATCH_FALL_PCT):
    """What should wake the Judge on one open position. Each is a name plus facts; the caller sends each once per position. A missing input fires nothing."""
    out={}
    try:spend=int(order['spend_lamports']);peak=int(order['peak_lamports'] or spend)
    except (TypeError,ValueError):return out
    priced=[m for m in marks if m['lamports'] is not None]
    if priced and peak>0 and spend>0:
        value=int(priced[-1]['lamports']);top=max(peak,spend)
        fall=(1-value/top)*100
        if math.isfinite(fall) and fall>=fall_pct:out['mark_fall']=dict(fall_from_peak_pct=round(fall,1),pnl_pct=round((value/spend-1)*100,1),mark_kind=priced[-1]['binding_kind'])
    try:
        with closing(_tape(desk_dir)) as conn:rows,_=_rows(conn,order['pool'],now_host)
    except (OSError,sqlite3.Error):return out
    cut=now_host-FLOW_WINDOW_S;net=0;earlier=0;held={};sold={}
    for at,actor,is_buy,quote,*_ in rows:
        try:sol=int(quote)
        except (TypeError,ValueError):continue
        signed=sol if is_buy else -sol
        if at>=cut:net+=signed
        elif at>=cut-FLOW_WINDOW_S:earlier+=signed
        if actor:
            if order['filled_at'] and at<order['filled_at'] and is_buy:held[actor]=held.get(actor,0)+sol
            if at>=cut and not is_buy:sold[actor]=sold.get(actor,0)+sol
    if earlier>0 and net<0:out['flow_flip']=dict(prior_30s_net_sol=earlier/1e9,last_30s_net_sol=net/1e9)
    if held:
        actor,size=max(held.items(),key=lambda kv:kv[1])
        if sold.get(actor,0)>=size*HOLDER_SOLD_SHARE:out['large_buyer_selling']=dict(actor=actor,bought_sol=size/1e9,sold_30s_sol=sold[actor]/1e9)
    return out
