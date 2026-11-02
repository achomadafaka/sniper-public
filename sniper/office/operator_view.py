"""Dashboard-only exploratory report. Fixed read-only Office sources; no seat export."""
from contextlib import ExitStack
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json
import math
import sqlite3
import os
from time import time as _host_now
from .explore_report import explore_report
from .report import _ro


def _display_fx(root):
    from scripts.office_paper_watcher import read_fx
    from sniper.services.memecoin_desk_intake import clock_sample
    host=_host_now()
    offset,_=clock_sample(os.getenv('SNIPER_CLOCK_OFFSET_FILE') or root.parent/'clock_offset.json',now=host)
    if offset is None:return {}
    rate,at=read_fx(root.parent/'desk',now_host=host,offset=offset)
    return dict(sol_usd=rate,sol_at=at) if rate is not None else {}


def _makers(conn,now):
    """Exact recorded decision-to-order links. An incomplete or conflicting window is unknown."""
    try:
        rows=conn.execute('SELECT order_id,actor_id FROM decision_receipts WHERE created_at<=? ORDER BY created_at DESC LIMIT 401',(now,)).fetchall()
        if len(rows)>400:return {}
        grouped={}
        for row in rows:
            if isinstance(row['actor_id'],str) and row['actor_id']:
                grouped.setdefault(row['order_id'],set()).add(row['actor_id'])
        return {oid:next(iter(actors)) for oid,actors in grouped.items() if len(actors)==1}
    except (sqlite3.Error,ValueError):return {}


def _marked(conn,*,now,fx,**scope):
    """Display only: book value at the newest marks, labelled estimates included. equity_usd stays proven-only for the report."""
    from . import ledger as L
    eq=L.equity(conn,now=now,mark_max_age_s=60.,**scope,**fx)
    return dict(marked_state=eq.state,marked_unpriced_count=eq.unpriced_count,marked_basis='newest marks, may be carried or estimated; display only',
                marked_equity_usd=eq.cash+eq.known_liq_lower if eq.state=='known' and eq.known_liq_lower is not None else None)


def _watcher_heartbeat(path,now):
    """The file the watcher writes; the report's databases do not hold it. Display only. `now` is corrected; the stamp is host time, so it is corrected by its own sample first."""
    try:
        beat=json.loads(Path(path).read_text())
        at,sample=beat['ts_host'],beat['clock_sample']   # no sample, no age: host time cannot be read against a corrected now
        offset=sample['host_utc']-sample['corrected_utc']
        age=now-(at-offset)
        if type(at) not in (int,float) or not math.isfinite(age) or age<-.5:raise ValueError('heartbeat_clock')   # .5 s: written during this read
        return dict(state='known',complete=True,watcher_state=beat.get('state'),fx_state=beat.get('fx_state'),last_good_utc=beat.get('last_good_utc'),age_s=age)
    except (OSError,ValueError,TypeError,KeyError,AttributeError):return dict(state='unknown',complete=False,reason='watcher_heartbeat_unreadable')


def _main_marked(root,now,fx):
    """Its own handle: the report leaves a read budget on its ledger connection, and this figure must never fail the report."""
    try:
        with _ro(Path(root)/'v2/explore-ledger/ledger.db') as conn:return _marked(conn,now=now,fx=fx,epoch_id='office-v2-explore-1')
    except (OSError,sqlite3.Error,ValueError,TypeError):return dict(marked_state='unknown',marked_equity_usd=None,marked_unpriced_count=None,marked_basis='source_unavailable')


def _order_plan(root,ledger,order_id,now):
    """Thesis, disproof and exit plan of the admitted decision only, known by this read: an abandoned attempt or a later label is not the order's plan. Older orders have none."""
    empty=dict(entry_reason=None,plan=None)
    try:
        receipt=ledger.execute('SELECT judgements_hash FROM decision_receipts WHERE order_id=? AND created_at<=?',(order_id,now)).fetchone()
        if receipt is None:return empty
        with _ro(Path(root)/'office.db') as conn:
            row=conn.execute('SELECT labels_json FROM office_v2_order_labels WHERE order_id=? AND decision_hash=? AND at<=?',(order_id,receipt[0],now)).fetchone()
        data=json.loads(row[0]) if row else {}
        return dict(entry_reason=data.get('entry_reason'),plan=data.get('plan'))
    except (OSError,sqlite3.Error,ValueError,TypeError):return empty


def _gpt_book(root,now,fx):
    from .v2_explore import GPT_BOOK,GPT_EPOCH
    from . import ledger as L
    from .report import _number
    out=dict(state='unknown',book_id=GPT_BOOK,epoch_id=GPT_EPOCH,label='Judge GPT',scope='single_book_only',
             as_of=now,reason='source_unavailable',cash_usd=None,equity_usd=None,unpriced_count=None,orders=[],limited=False)
    try:
        with _ro(Path(root)/'v2/explore-gpt-ledger/ledger.db') as conn:
            identity=conn.execute('SELECT epoch_id,kind FROM books WHERE book_id=?',(GPT_BOOK,)).fetchone()
            if identity is None or identity['epoch_id']!=GPT_EPOCH or identity['kind']!='evaluated':
                out['reason']='book_identity_unknown';return out
            equity=L.equity(conn,book_id=GPT_BOOK,now=now,mark_max_age_s=60.,require_proven=True,**fx)
            out.update(state=equity.state,reason=None,cash_usd=equity.cash,unpriced_count=equity.unpriced_count,
                       equity_usd=equity.cash+equity.known_liq_lower if equity.state=='known' and equity.known_liq_lower is not None else None,
                       **_marked(conn,now=now,fx=fx,book_id=GPT_BOOK))
            makers=_makers(conn,now)
            rows=conn.execute('SELECT order_id,network_id,mint,ticker,state,requested_at,filled_at,closed_at,spend_usd,exit_usd,costs_usd,exit_fill_kind FROM orders '
                'WHERE book_id=? AND epoch_id=? AND requested_at<=? ORDER BY requested_at DESC,order_id DESC LIMIT 201',(GPT_BOOK,GPT_EPOCH,now)).fetchall()
            out['limited']=len(rows)>200
            for row in rows[:200]:
                item=dict(row,book_id=GPT_BOOK,actor_id=makers.get(row['order_id']))
                item.update(_order_plan(root,conn,row['order_id'],now))
                if _number(item['closed_at']) and item['closed_at']>now:item.update(state='open',exit_usd=None,closed_at=None)
                values=[item[k] for k in ('exit_usd','spend_usd','costs_usd')]
                item['modelled_net_usd']=values[0]-values[1]-values[2] if item['state']=='closed' and all(_number(v) for v in values) else None
                out['orders'].append(item)
        return out
    except (OSError,sqlite3.Error,ValueError,TypeError):return dict(out,state='unknown',reason='source_unavailable',equity_usd=None)


def _display_symbols(root,orders,now):
    """Exact Solana mint display enrichment only; never edits the recorded order ticker."""
    from .report import _number
    from sniper.services.memecoin_desk_intake import clock_sample
    root=Path(root);desk=root.parent/'desk'
    rows=orders[:400]
    if not _number(now) or not 0<=now<=253402300799:return
    def symbol(value):return value.strip() if isinstance(value,str) and value.strip() and len(value.encode('utf-8'))<=160 else None
    pending={r['mint'] for r in rows if r.get('network_id')=='solana' and isinstance(r.get('mint'),str) and not symbol(r.get('ticker'))}
    found={}
    offset,_=clock_sample(os.getenv('SNIPER_CLOCK_OFFSET_FILE') or root.parent/'clock_offset.json',now=now)
    if offset is not None:
        try:
            with _ro(desk/'narrative_guess/narrative_guess.db') as conn:
                for mint in pending:
                    row=conn.execute("SELECT symbol,first_seen_known_at FROM launches WHERE network_id='solana' AND mint=? AND first_seen_known_at<=?",(mint,now-offset)).fetchone()
                    if row and symbol(row['symbol']) and _number(row['first_seen_known_at']):found[mint]=(symbol(row['symbol']),'narrative_guess.launches',row['first_seen_known_at'])
        except (OSError,sqlite3.Error,ValueError,TypeError):pass
    # Only the current/prior UTC day is inspected; absence is unknown, never a guessed ticker.
    for day in (datetime.fromtimestamp(now,timezone.utc).date(),datetime.fromtimestamp(now,timezone.utc).date()-timedelta(days=1)):
        try:
            with _ro(desk/'x'/(day.isoformat()+'.db')) as conn:
                for mint in pending-found.keys():
                    row=conn.execute("SELECT symbol,received_at FROM token_meta WHERE mint=? AND (chain IS NULL OR lower(chain) IN ('solana','sol')) AND received_at<=? ORDER BY received_at DESC,id DESC LIMIT 1",(mint,now)).fetchone()
                    if row and symbol(row['symbol']) and _number(row['received_at']):found[mint]=(symbol(row['symbol']),'x.token_meta',row['received_at'])
        except (OSError,sqlite3.Error,ValueError,TypeError):pass
    for row in rows:
        original=symbol(row.get('ticker'))
        value,source,at=(original,'orders.ticker',None) if original else found.get(row.get('mint'),(None,'unknown',None)) if row.get('network_id')=='solana' else (None,'unknown',None)
        mint=row.get('mint')
        if value is None and isinstance(mint,str) and len(mint)>=8:value,source=mint[:4]+'…'+mint[-4:],'mint_short'   # a row always has a name; the source says it is no ticker
        row.update(display_ticker=value,display_ticker_source=source,display_ticker_at=at)


def _head_halt(root,now):
    from .report import _number
    out=dict(state='unknown',epoch_id='office-v2-explore-1',halted=None,reason='source_unavailable',at=None)
    try:
        with _ro(Path(root)/'office.db') as conn:
            row=conn.execute('SELECT action,actor_id,reason,at FROM office_v2_halts WHERE epoch_id=? AND at<=? ORDER BY seq DESC LIMIT 1',('office-v2-explore-1',now)).fetchone()
            if row is None:return dict(out,state='known',halted=False,reason='no_recorded_halt')
            if row['action'] not in ('halt','resume') or not _number(row['at']) or row['at']<0:return out
            return dict(out,**dict(row),state='known',halted=row['action']=='halt')
    except (OSError,sqlite3.Error,ValueError,TypeError):return out


def read_operator_report(root,now):
    root=Path(root)
    fx={}
    try:
        fx=_display_fx(root)
        with ExitStack() as stack:
            office=stack.enter_context(_ro(root/'office.db'))
            ledger=stack.enter_context(_ro(root/'v2/explore-ledger/ledger.db'))
            quota=stack.enter_context(_ro(root/'v2/quota.db'))
            report=explore_report(office,ledger,quota,now=now,**fx)
            makers=_makers(ledger,now)
            for row in (report.get('orders') or {}).get('items',[]):row['actor_id']=makers.get(row.get('order_id'))
        current=(report.get('book') or {}).get('now')
        if isinstance(current,dict):current.update(_main_marked(root,now,fx))
        if isinstance(report.get('book'),dict) and 'watcher_heartbeat' in report['book']:report['book']['watcher_heartbeat']=_watcher_heartbeat(root/'v2/watcher/paper-watcher.json',now)
        result={k:report.get(k) for k in ('state','epoch_id','now','window_start','window_end','book','orders','segments','null_arm','source_consistency','read_budget')}
    except (OSError,sqlite3.Error,ValueError,TypeError):
        result=dict(state='unknown',reason='report_source_unavailable',epoch_id='office-v2-explore-1')
    result['separate_books']=[dict(_gpt_book(root,now,fx),watcher_heartbeat=_watcher_heartbeat(root/'v2/watcher-gpt/paper-watcher.json',now))]
    result['head_halt']=_head_halt(root,now)
    orders=list((result.get('orders') or {}).get('items',[]))+[r for b in result['separate_books'] for r in b['orders']]
    _display_symbols(root,orders,now)
    return result
