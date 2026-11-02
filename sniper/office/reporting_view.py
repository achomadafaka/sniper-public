"""Bounded read-only projection of the public reporting producer's typed tables."""
from datetime import datetime, timezone
import json
import sqlite3

LIMIT=200
COUNTS=('turns_started','turns_completed','turns_partial','calls','calls_not_ok','cost_unknown_n','handoffs_out','handoffs_in','late_after_close','decisions','linked_closed_n','wins_n','pnl_unknown_n','open_n','overlap_n','missing_n')
DAILY=('utc_day','actor_id','generation',*COUNTS,'measured_cost_usd','realized_paper_pnl_usd','source_heads','covered_through','written_at')
HISTORY=('book_id','at','bucket_key','cash_usd','known_liq_usd','equity_usd','priced_n','unpriced_n','fx_usd','fx_at','source_heads','state','fill_model_version','written_at')


def _unknown(reason):
    return dict(state='unknown',items=[],limited=False,omitted=0,reason=reason,coverage='latest 200 persisted rows')


def _heads(raw):
    from .report import _public
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>8192:raise ValueError('source_heads_invalid')
    heads=json.loads(raw)
    if not isinstance(heads,dict) or not _public(heads):raise ValueError('source_heads_invalid')
    release=heads.get('release_sha')
    if release is not None and (not isinstance(release,str) or len(release)!=40 or any(c not in '0123456789abcdef' for c in release)):raise ValueError('release_sha_invalid')
    # Keep provenance shallow and bounded; never return an arbitrary producer payload.
    from .report import _number
    return {str(k)[:128]:v if _number(v) or v is None else str(v)[:256] for k,v in list(heads.items())[:32]}


def _indexed_tail(conn,query,params):
    plan=[str(r[3]) for r in conn.execute('EXPLAIN QUERY PLAN '+query,params)]
    if not any('SEARCH ' in line for line in plan) or any('TEMP B-TREE' in line for line in plan):
        raise ValueError('reporting_index_missing')
    return conn.execute(query,params).fetchall()


def read_daily(conn,*,now,public_scope,public_epochs=()):
    from .report import _number
    if not _number(now):return _unknown('clock_unknown')
    day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
    try:
        names=[r[0] for r in conn.execute("SELECT name FROM pragma_table_info('agent_daily') LIMIT 33")]
        if len(names)>32:return _unknown('summary_schema_unknown')
        epoch_keyed='epoch_id' in names
        if not epoch_keyed and not public_scope:return _unknown('release_scope_unknown')
        columns=(*DAILY,'epoch_id') if epoch_keyed else DAILY
        rows=_indexed_tail(conn,'SELECT '+','.join(columns)+' FROM agent_daily WHERE utc_day<=? ORDER BY utc_day DESC LIMIT 201',(day,))
        result=dict(state='known',items=[],limited=len(rows)>LIMIT,omitted=0,coverage='latest 200 persisted actor/day/generation rows',clock_basis='host_utc',currency='USD')
        for raw in rows[:LIMIT]:
            r=dict(raw)
            if epoch_keyed and not (public_scope if r['epoch_id'] is None else r['epoch_id'] in public_epochs):
                result['omitted']+=1;continue
            try:
                parsed=datetime.strptime(r['utc_day'],'%Y-%m-%d').date().isoformat()
                if parsed!=r['utc_day'] or not isinstance(r['actor_id'],str) or not r['actor_id'] or len(r['actor_id'])>256 or type(r['generation']) is not int or (r['generation']<1 and r['generation']!=-1):raise ValueError('identity_invalid')
                if not all(_number(r[k]) and 0<=r[k]<=now for k in ('covered_through','written_at')):raise ValueError('future_or_unknown_cut')
                if r['covered_through']>r['written_at']:raise ValueError('coverage_after_write')
                r['source_heads']=_heads(r['source_heads'])
            except (ValueError,TypeError,UnicodeError,RecursionError):
                result['omitted']+=1;continue
            if epoch_keyed and r['epoch_id'] is not None:
                for k in ('turns_started','turns_completed','turns_partial','calls','calls_not_ok','measured_cost_usd','cost_unknown_n','handoffs_out','handoffs_in'):r[k]=None
            r['epoch_scope']=r.get('epoch_id') or 'not epoch-attributed'
            for k in COUNTS:
                if type(r[k]) is not int or r[k]<0:r[k]=None
            for k in ('measured_cost_usd','realized_paper_pnl_usd'):
                if not _number(r[k]):r[k]=None
            r['freshness']='stale' if now-r['written_at']>900 else 'fresh'
            r['coverage_state']='partial' if r['missing_n']!=0 or r['cost_unknown_n']!=0 or r['pnl_unknown_n']!=0 or any(r[k] is None for k in COUNTS) else 'known'
            result['items'].append(r)
        if result['limited'] or result['omitted'] or any(r['coverage_state']=='partial' or r['freshness']=='stale' for r in result['items']):result['state']='partial'
        if result['omitted'] and not result['items']:result['state']='unknown'
        return result
    except (sqlite3.Error,ValueError,TypeError,OverflowError):return _unknown('summary_unavailable')


def read_equity_history(conn,books,*,now):
    from .report import _number
    if not _number(now):return _unknown('clock_unknown')
    public={b['book_id']:b for b in books if b.get('kind')=='discovery'}
    try:
        rows=_indexed_tail(conn,'SELECT '+','.join(HISTORY)+' FROM equity_snapshots INDEXED BY equity_snapshots_at_book WHERE at<=? ORDER BY at DESC,book_id DESC LIMIT 201',(now,))
        result=dict(state='known',items=[],limited=len(rows)>LIMIT,omitted=0,coverage='latest 200 persisted snapshots, all books; discovery subset',currency='USD',clock_basis='host_utc')
        for raw in rows[:LIMIT]:
            r=dict(raw)
            if r['book_id'] not in public:
                result['omitted']+=1;continue
            try:
                if not all(_number(r[k]) and 0<=r[k]<=now for k in ('at','bucket_key','written_at')) or r['bucket_key']>r['at']:raise ValueError('snapshot_cut_invalid')
                r['source_heads']=_heads(r['source_heads'])
            except (ValueError,TypeError,UnicodeError,RecursionError):
                result['omitted']+=1;continue
            for k in ('cash_usd','known_liq_usd','equity_usd','fx_usd','fx_at'):
                if not _number(r[k]):r[k]=None
            for k in ('priced_n','unpriced_n'):
                if type(r[k]) is not int or r[k]<0:r[k]=None
            # A priceable subset is not a complete book. Historic points keep their own valuation cut.
            fx_known=r['fx_usd'] is not None and r['fx_usd']>0 and r['fx_at'] is not None and 0<=r['at']-r['fx_at']<=120
            if (r['state']!='known' or r['unpriced_n']!=0 or r['priced_n'] is None or r['cash_usd'] is None or r['known_liq_usd'] is None
                    or r['priced_n']>0 and not fx_known):r['equity_usd']=None
            r['state']=r['state'] if r['state'] in ('known','partial','unknown') else 'unknown'
            r['fill_model_version']=str(r['fill_model_version'])[:128]
            r['epoch_id']=public[r['book_id']].get('epoch_id')
            result['items'].append(r)
        result['items'].sort(key=lambda r:(r['at'],r['book_id']))
        if result['limited'] or result['omitted'] or any(r['equity_usd'] is None for r in result['items']):result['state']='partial'
        return result
    except (sqlite3.Error,ValueError,TypeError):return _unknown('snapshots_unavailable')
