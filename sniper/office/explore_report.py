"""Bounded exploratory operations data, never a verdict or a seat-facing tool.

Only the supplied connections are used (market_conn is optional). No writes, settings, files, model
calls or connection-global hooks. Separate databases share a logical event cut,
not an atomic snapshot. Missing telemetry remains unknown, never an inferred zero.
"""
from collections import Counter
from sniper.office.partial_sheet import failure_class
from datetime import datetime, timezone
import json
import math
import sqlite3
import statistics

from . import costs, ledger as L
from .seat_host import proof_report
from .quota_measurement import RETENTION_S

EPOCH='office-v2-explore-1'
ROW_CAP=2000
OUTPUT_BYTES=1048576   # about 370 full order rows; _fit then slims them, then drops the oldest
MARKET_ROW_CAP=10000
MARKET_STOP_CAP=100
MARKET_RETENTION_S=30*86400  # seat_retention.prune_market call rows


def _unknown(reason):return dict(state='unknown',complete=False,reason=reason)

def _number(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=1e11

def _json(value):return json.dumps(value,allow_nan=False,separators=(',',':')).encode()

def _load(raw):
    if not isinstance(raw,str) or len(raw.encode())>65536:raise ValueError('payload_size')
    value=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if not isinstance(value,dict):raise ValueError('payload_shape')
    return value


class _Limit(ValueError):pass


class _Cursor:
    def __init__(self,rows):self.rows=rows;self.index=0
    def __iter__(self):return self
    def __next__(self):
        if self.index==len(self.rows):raise StopIteration
        row=self.rows[self.index];self.index+=1;return row
    def fetchone(self):return next(self,None)
    def fetchall(self):return list(self)


class _Read:
    """Shared result/query/byte budget, including the existing ledger/proof helpers."""
    def __init__(self,conn,budget):self.conn,self.budget=conn,budget
    def execute(self,sql,args=()):
        if not sql.lstrip().upper().startswith('SELECT '):raise _Limit('read_only_required')
        self.budget['queries']+=1
        if self.budget['queries']>512:raise _Limit('query_cap')
        cursor=self.conn.execute(sql,args)
        rows=[]
        for row in cursor:
            if not hasattr(row,'keys'):row=sqlite3.Row(cursor,row)
            self.budget['bytes']+=len(_json(dict(row)))
            if self.budget['bytes']>16*1024**2:raise _Limit('byte_cap')
            rows.append(row)
            if len(rows)>10001:raise _Limit('row_cap')
        return _Cursor(rows)
    def rows(self,sql,args=(),cap=ROW_CAP):
        rows=[dict(r) for r in self.execute(sql+' LIMIT ?',(*args,cap+1))]
        if len(rows)>cap:raise _Limit('row_cap')
        return rows


_ERRORS=(sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,RecursionError)


def _section(call):
    try:return call()
    except _ERRORS as exc:return _unknown(str(exc) if isinstance(exc,_Limit) else 'source_unavailable_or_invalid:'+type(exc).__name__)


def _trim_orders(out):
    """Rows are never all withheld. Newest first: slim every row, then drop the oldest and say how many."""
    orders=out.get('orders')
    if not isinstance(orders,dict) or not orders.get('items'):return False
    items=orders['items']
    if any('recorded_fields' in r for r in items):
        orders['items']=[{k:v for k,v in r.items() if k!='recorded_fields'} for r in items]
        orders['recorded_fields_omitted']=True
    else:
        over=len(_json(out))-OUTPUT_BYTES
        per=max(1,len(_json(items))//len(items))
        drop=min(len(items),max(1,over//per+1))
        orders['items']=items[:-drop]
        orders['items_omitted']=orders.get('items_omitted',0)+drop
    orders['truncated']=True;out['truncated']=True
    return True


def _fit(out):
    while len(_json(out))>OUTPUT_BYTES:
        candidates=[(len(_json(v)),k) for k,v in out.items() if k!='orders' and isinstance(v,dict) and 'items' in v]
        if candidates:
            _,key=max(candidates);value=out[key]
            out[key]={k:v for k,v in value.items() if k!='items'}
            out[key].update(truncated=True,items_omitted=len(value['items']))
        elif _trim_orders(out):continue
        else:
            candidates=[(len(_json(v)),k) for k,v in out.items() if isinstance(v,dict) and len(_json(v))>1024]
            if not candidates:return _unknown('output_budget')
            _,key=max(candidates);out[key]=dict(state='partial',complete=False,truncated=True,reason='output_budget')
        out['truncated']=True
    return out


def _segments(o,start,end,now):
    rows=o.rows('SELECT segment,at,release_sha,content_hash,kind,note FROM office_v2_changes WHERE epoch_id=? AND at<=? ORDER BY segment',(EPOCH,now))
    if not rows or [r['segment'] for r in rows]!=list(range(1,len(rows)+1)) or any(
        not _number(r['at']) or i and r['at']<rows[i-1]['at'] for i,r in enumerate(rows)):
        return _unknown('segment_sequence_unknown')
    return dict(state='known',complete=True,items=rows,current_segment=rows[-1]['segment'],
                changes_in_window=sum(start<=r['at']<end for r in rows))


def _segment(changes,at):
    if changes.get('state')!='known' or not _number(at):return None
    found=[r['segment'] for r in changes['items'] if r['at']<=at]
    return found[-1] if found else None


def _field_state(value):
    if value is None or isinstance(value,str) and value=='unknown':return 'unknown'
    if isinstance(value,str) and value in ('absent','absent_by_manifest'):return value
    return 'known'


def _policy_hash(value):
    return value if isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value) else None


def _proof_binding(raw):
    """Stored display label only; this does not validate execution provenance."""
    if raw is None:return 'unknown'
    try:kind=_load(raw).get('binding_kind')
    except (ValueError,TypeError):return 'invalid_proof'
    if kind is None:return 'unknown'
    return kind if isinstance(kind,str) and 0<len(kind)<=80 else 'invalid_proof'


def _orders(o,l,start,end,changes,epoch):
    rows=l.rows('SELECT order_id,network_id,mint,filled_at,requested_at,state,spend_usd,entry_unit_price_usd,closed_at,exit_requested_at,exit_reason,exit_fill_kind,exit_trigger_kind,ticker,reason,exit_usd,costs_usd,fill_proof,exit_proof,sheet_state,sheet_class,sells_basis,sells_n,sheet_ticket_age_s,probe_age_s,probe_loss_pct FROM orders '
                'WHERE epoch_id=? AND book_kind=? AND COALESCE(filled_at,requested_at)>=? AND COALESCE(filled_at,requested_at)<? ORDER BY COALESCE(filled_at,requested_at) DESC,order_id DESC',
                (EPOCH,'evaluated',start,end))
    labels=o.rows('SELECT order_id,decision_hash,incarnation,source,sheet_age_s,at,labels_json FROM office_v2_order_labels WHERE epoch_id=? AND at<? ORDER BY at,order_id',(EPOCH,end))
    receipts=l.rows('SELECT order_id,judgements_hash FROM decision_receipts WHERE epoch_id=? AND order_id IS NOT NULL AND created_at<? ORDER BY created_at',(EPOCH,end))
    hashes={r['order_id']:r['judgements_hash'] for r in receipts}
    by_key={(r['order_id'],r['decision_hash']):r for r in labels}
    identity=epoch.get('book_policy')
    stored=_policy_hash(identity.get('sha256')) if isinstance(identity,dict) else None
    items=[]
    for row in rows:
        entry_binding=_proof_binding(row.pop('fill_proof'));exit_binding=_proof_binding(row.pop('exit_proof'))
        spend,proceeds,cost=(row.get(k) for k in ('spend_usd','exit_usd','costs_usd'))
        net=proceeds-spend-cost if row['state']=='closed' and all(_number(v) for v in (spend,proceeds,cost)) else None
        pct=net/spend*100 if net is not None and spend>0 else None
        if pct is not None and not math.isfinite(pct):pct=None
        last=row['closed_at'] if row['state']=='closed' else end if row['state']=='open' else None
        held=last-row['filled_at'] if _number(last) and _number(row['filled_at']) and last>=row['filled_at'] else None
        requested=row['exit_requested_at'];pending=last-requested if _number(last) and _number(requested) and last>=requested else None
        label=by_key.get((row['order_id'],hashes.get(row['order_id'])))
        item=dict(row,status=row['state'],clip_usd=row['spend_usd'],source='unknown',label_state='label_missing',
                  entry_binding_kind=entry_binding,exit_binding_kind=exit_binding,modelled_net_usd=net,modelled_net_pct=pct,hold_s=held,exit_pending_s=pending,
                  incarnation=None,sheet_age_s=None,virtual_quote_source_raw=None,virtual_quote_source='unknown',
                  tainted_by_web=None,operator_influenced=None,segment_id=None,
                  decision_latency=_unknown('feed_hard_filter_stamp_unavailable'),book_policy_sha256=None,policy_identity_state='unknown',
                  recorded_field_states={},recorded_fields_state='unknown',recorded_fields=_unknown('label_missing'))
        if label is not None:
            data=_load(label['labels_json'])
            if label['source'] not in ('feed','self'):raise ValueError('source_label_invalid')
            required=('equity_snapshot_id','post_swap_first_wake','workspace_manifest_hash','brain_fact_ids','db_read_hashes',
                      'notebook_manifest_hash','granted_request_ids','tainted_by_web','operator_influenced')
            states={k:_field_state(data.get(k)) for k in required}
            for k in ('tainted_by_web','operator_influenced'):
                if type(data.get(k)) is not bool:states[k]='unknown'
            fields_state='unknown' if 'unknown' in states.values() else 'partial' if any(v!='known' for v in states.values()) else 'known'
            policy=_policy_hash(data.get('book_policy_sha256'))
            policy_state='unknown'
            if policy is not None and stored is not None and changes.get('state')=='known':
                policy_state='stored' if policy==stored else 'override' if any(
                    c['kind']=='override' and c['content_hash']==policy and c['at']<=label['at'] and c['note'].strip()
                    for c in changes['items']) else 'unrecorded'
            raw=data.get('virtual_quote_source')
            item.update(source=label['source'],label_state='known',incarnation=label['incarnation'],sheet_age_s=label['sheet_age_s'],
                        virtual_quote_source_raw=raw,virtual_quote_source='exact' if raw=='pumpswap_rpc:pool_state' else 'tape' if raw in ('tape_tip','ps_tape','pumpswap_grpc:buy_event_virtual_quote') else 'unknown',
                        tainted_by_web=data.get('tainted_by_web'),operator_influenced=data.get('operator_influenced'),
                        segment_id=data.get('segment_id'),entry_reason=data.get('entry_reason'),plan=data.get('plan'),book_policy_sha256=policy,policy_identity_state=policy_state,
                        recorded_field_states=states,recorded_fields_state=fields_state,recorded_fields=data)
        items.append(item)
    return dict(state='known' if all(r['label_state']=='known' and r['recorded_fields_state']=='known' and r['policy_identity_state'] in ('stored','override') for r in items) else 'partial',complete=True,
                count=len(items),items=items,label_missing=sum(r['label_state']=='label_missing' for r in items),
                stored_book_policy_sha256=stored,by_policy_identity=dict(Counter(r['book_policy_sha256'] or 'unknown' for r in items)),
                by_policy_authorization=dict(Counter(r['policy_identity_state'] for r in items)),
                by_source=dict(Counter(r['source'] for r in items)),by_sheet_stratum=dict(Counter(r['virtual_quote_source'] for r in items)),
                by_segment=dict(Counter(str(r['segment_id']) for r in items)),
                by_sheet_and_sells=dict(Counter(_sheet_key(r) for r in items)),
                partial_canary=_partial_canary([dict(r) for r in l.execute("SELECT order_id,mint,state,requested_at,filled_at,closed_at,sheet_state,sheet_class,sells_basis,sells_n,sheet_ticket_age_s,probe_age_s,probe_loss_pct,spend_usd,exit_usd,costs_usd FROM orders "
                                                                          "WHERE epoch_id=? AND book_kind=? AND sheet_state='partial' AND requested_at<? ORDER BY requested_at,order_id LIMIT 10",(EPOCH,'evaluated',end)).fetchall()]),
                by_exit_fill_kind=dict(Counter(r['exit_fill_kind'] or 'none_or_pre_label' for r in items if r['state']=='closed')))


def _candidates(o,l,start,end):
    rows=o.rows('SELECT network_id,mint,actor_id,known_at FROM office_v2_candidates WHERE epoch_id=? AND known_at>=? AND known_at<? ORDER BY known_at',(EPOCH,start,end))
    ordered=l.rows('SELECT network_id,mint FROM orders WHERE epoch_id=? AND requested_at<?',(EPOCH,end))
    coins={(r['network_id'],r['mint']) for r in ordered}
    return dict(state='known',complete=True,count=len(rows),by_actor=dict(Counter(r['actor_id'] for r in rows)),
                registered_never_ordered=sum((r['network_id'],r['mint']) not in coins for r in rows))


def _refusals(o,start,end,changes):
    rows=o.rows('SELECT corrected_at,text,refs_json,truncated FROM office_v2_events WHERE epoch_id=? AND kind=? '
                "AND json_extract(refs_json,'$.tool') IN ('paper_order','candidate_register','book_halt') "
                'AND corrected_at>=? AND corrected_at<? ORDER BY corrected_at,seq',(EPOCH,'tool_result',start,end))
    observed=[]
    for row in rows:
        if row['truncated']:raise ValueError('tool_result_truncated')
        tool=_load(row['refs_json'])['tool'];result=_load(row['text'])
        if not isinstance(result.get('state'),str):raise ValueError('tool_result_state_missing')
        if result['state'] not in ('submitted','registered','halt','resume'):
            observed.append(dict(tool=tool,outcome=result['state'],reason=result.get('reason') or result['state'],
                                 segment_id=_segment(changes,row['corrected_at'])))
    return dict(state='known',complete=True,denominator='persisted paper_order, candidate_register and book_halt tool results',
                count=len(observed),by_reason=dict(Counter(r['reason'] for r in observed)),
                by_outcome=dict(Counter(r['outcome'] for r in observed)),
                by_segment=dict(Counter(str(r['segment_id']) for r in observed)),
                refused_by_sheet_class=dict(Counter(failure_class(r['reason'][6:])[0] for r in observed if r['tool']=='paper_order' and r['reason'].startswith('sheet_'))),
                self_attempt_refused=sum(r['tool']=='paper_order' and r['reason'] in ('self_sheet_unavailable','candidate_not_registered') for r in observed))


def _equity(l,at,*,sol_usd=None,sol_at=None):
    # Historical FX is causal ledger evidence; optional fresh display FX never enters past cuts.
    fx=l.rows('SELECT fx_usd,fx_at FROM cashflows c JOIN orders o USING(order_id) WHERE o.epoch_id=? AND c.at<=? AND fx_at<=? ORDER BY fx_at DESC',
              (EPOCH,at,at),cap=4000)
    last=fx[0] if fx else {}
    source='latest_causal_ledger_cashflow'
    if L._fx(sol_usd,sol_at,at,120.) is not None:
        last=dict(fx_usd=sol_usd,fx_at=sol_at);source='universe_heartbeat'
    eq=L.equity(l,now=at,mark_max_age_s=60.,sol_usd=last.get('fx_usd'),sol_at=last.get('fx_at'),epoch_id=EPOCH,require_proven=True)
    capital=l.rows('SELECT usd FROM capital_flows WHERE book_id=? AND at<=?',(EPOCH,at),cap=4000)
    total=eq.cash+eq.known_liq_lower if eq.state=='known' and eq.known_liq_lower is not None else None
    return dict(state=eq.state,at=at,equity_usd=total,trading_net=None if total is None else total-sum(r['usd'] for r in capital),
                cash_usd=eq.cash,unpriced_count=eq.unpriced_count,legs=eq.legs,binding_kind_counts=eq.binding_kind_counts,
                fx_source=source,fx_at=last.get('fx_at'),fx_age_s=None if last.get('fx_at') is None else at-last['fx_at'])


def _partial_canary(items):
    """The first ten partial-sheet orders of the book in submission order, whatever window the report covers: the change is read order by order before any rate."""
    rows=sorted((r for r in items if r.get('sheet_state')=='partial'),key=lambda r:(r['requested_at'],r['order_id']))[:10]
    keep=('order_id','mint','state','requested_at','filled_at','closed_at','sheet_class','sells_basis','sells_n','sheet_ticket_age_s','probe_age_s','probe_loss_pct')
    def net(r):
        spend,out,cost=(r.get(k) for k in ('spend_usd','exit_usd','costs_usd'))
        return out-spend-cost if r.get('state')=='closed' and all(_number(v) for v in (spend,out,cost)) else None
    return [dict({k:r.get(k) for k in keep},modelled_net_usd=net(r)) for r in rows]


def _sheet_key(r):
    """Sheet state and sells basis of an order. NULL stays 'unstamped', never read as known."""
    return '%s/%s'%(r.get('sheet_state') or 'unstamped',r.get('sells_basis') or 'unstamped')


def _stratify_sheets(stats,closed,nets):
    """Partial-sheet orders have weaker entry evidence: never one mean over both."""
    groups={}
    for r in closed:groups.setdefault(_sheet_key(r),[]).append(nets[r['order_id']])
    stats['by_sheet_and_sells']={k:dict(n=len(v),mean_trading_net=statistics.mean(v),median=statistics.median(v),win_count=sum(x>0 for x in v),worst=min(v)) for k,v in groups.items()}
    if any(r.get('sheet_state')!='known' for r in closed) and len(groups)>1:stats.update(mean_trading_net=None,median=None,blended_mean_withheld=True)


def _stratify_exit_fills(stats,closed,nets):
    """Estimated closes exist exactly where strict proof fails: never one mean over both kinds."""
    groups={}
    for r in closed:groups.setdefault(r['exit_fill_kind'] or 'strict',[]).append(nets[r['order_id']])
    stats['by_exit_fill_kind']={k:dict(n=len(v),mean_trading_net=statistics.mean(v),median=statistics.median(v),win_count=sum(x>0 for x in v),worst=min(v)) for k,v in groups.items()}
    if 'estimated_terminal' in groups:stats.update(mean_trading_net=None,median=None,blended_mean_withheld=True,all_closes_figures='win_count, worst and top3_profit_share include estimated closes')


def _book(l,epoch,start,end,now,changes,*,sol_usd=None,sol_at=None):
    allorders=l.rows('SELECT order_id,state,filled_at,closed_at,spend_usd,exit_fill_kind,exit_requested_at,due_at,sheet_state,sells_basis FROM orders WHERE epoch_id=? AND requested_at<=?',(EPOCH,now))
    flows=l.rows('SELECT at,kind,usd FROM capital_flows WHERE book_id=? AND at<=? ORDER BY at',(EPOCH,now),cap=4000)
    if not flows:raise ValueError('book_unfunded')
    current=_equity(l,now,sol_usd=sol_usd,sol_at=sol_at)
    bounds=[current if t==now else _equity(l,t) for t in (start,end)]
    cost=costs.epoch_cost(opened_at=start,ended_at=end,look_at=now)
    cost=dict(state='known',amount_usd=cost['known_cost_usd'],touched_days=cost['days'],rule_id='office-cost-allocation-v4',
              cost_basis='touched_days_full',window_warning='Adjacent windows sharing a UTC day double-count that day; use whole epoch for additive C.')
    if changes.get('state')!='known' or any(r['kind']=='override' and r['at']<end for r in changes.get('items',[])):
        cost=_unknown('measurement_definition_override_or_log_unknown')
    closed=[r for r in allorders if r['closed_at'] is not None and start<=r['closed_at']<end]
    cashflows=l.rows('SELECT c.order_id,c.kind,c.at,c.usd FROM cashflows c JOIN orders o USING(order_id) WHERE o.epoch_id=? AND c.at<? ORDER BY c.at',(EPOCH,end),cap=4000)
    nets={r['order_id']:sum(c['usd'] for c in cashflows if c['order_id']==r['order_id']) for r in closed}
    # Missing either leg is not a zero-return closed coin.
    complete=all({'entry','exit'}<={c['kind'] for c in cashflows if c['order_id']==r['order_id']} for r in closed)
    stats=_unknown('equity_or_closed_cashflows_unknown')
    stats['withheld_reason']='equity_or_closed_cashflows_unknown'
    if current['state']=='known' and all(v['state']=='known' for v in bounds) and complete:
        values=list(nets.values());profits=sum(v for v in values if v>0)
        days=Counter()
        for r in closed:days[datetime.fromtimestamp(r['closed_at'],timezone.utc).date().isoformat()]+=nets[r['order_id']]
        stats=dict(state='known',denominator='closed ENTER coins in window',closed_coins=len(values),mean_trading_net=statistics.mean(values) if values else None,
                   win_count=sum(v>0 for v in values),median=statistics.median(values) if values else None,worst=min(values) if values else None,
                   top3_profit_share=sum(sorted((v for v in values if v>0),reverse=True)[:3])/profits if profits else None,
                   positive_utc_days=sum(v>0 for v in days.values()))
        _stratify_exit_fills(stats,closed,nets)
        _stratify_sheets(stats,closed,nets)
    stats['terminal_exits']=dict(closed_estimated_terminal=sum(r['exit_fill_kind']=='estimated_terminal' for r in allorders),
        exit_pending_past_hold=sum(r['state']=='open' and r['exit_requested_at'] is not None and r['due_at'] is not None and now-r['due_at']>L.EXIT_CUT_HOLD_S for r in allorders),coverage='open past hold is censored, not zero')
    window_net=None
    if all(v['state']=='known' for v in bounds):
        window_net=bounds[1]['equity_usd']-bounds[0]['equity_usd']-sum(r['usd'] for r in flows if start<r['at']<=end)
    marks=[]
    for r in allorders:
        if r['filled_at'] is not None and r['filled_at']<=now and (r['closed_at'] is None or r['closed_at']>now):
            m=l.rows('SELECT MAX(at) AS at FROM marks WHERE order_id=? AND at<=?',(r['order_id'],now),cap=1)
            marks.append(dict(order_id=r['order_id'],mark_age_s=now-m[0]['at'] if m and m[0]['at'] is not None else None))
    safety=l.rows('SELECT * FROM epoch_entry_safety WHERE epoch_id=?',(EPOCH,),cap=1)
    safety=safety[0] if safety else {}
    drawdown=safety.get('drawdown_equity_usd')
    drawdown=None if drawdown is None or safety.get('peak_equity_usd') is None else safety['peak_equity_usd']-drawdown
    daily_rows=l.rows('SELECT c.usd FROM cashflows c JOIN orders o USING(order_id) WHERE o.epoch_id=? AND o.closed_at>=? AND o.closed_at<=? AND c.at<=?',
                      (EPOCH,now-now%86400,now,now),cap=4000)
    daily=sum(r['usd'] for r in daily_rows)
    return dict(state=current['state'],now=current,window_bounds=bounds,window_trading_net=window_net,
                office_net=window_net-cost['amount_usd'] if window_net is not None and cost['state']=='known' and current['state']=='known' else None,
                cost=cost,statistics=stats,open_positions=marks,oldest_mark_age_s=max((m['mark_age_s'] for m in marks if m['mark_age_s'] is not None),default=None),
                closed_count=len(closed),capital_flows=flows,unexpected_capital_flows=[r for i,r in enumerate(flows) if i>0 or r['kind']!='initial'],
                drawdown_usd=drawdown,drawdown_distance_usd=None if drawdown is None else 200-drawdown,
                daily_realized_usd=daily,daily_loss_limit_usd=100.,unknown_since=safety.get('unknown_since'),
                safety_observed_at=safety.get('last_at'),watcher_heartbeat=_unknown('watcher_heartbeat_not_in_supplied_databases'))


def _outages(l,start,end):
    rows=l.rows('SELECT x.* FROM outages x JOIN orders o USING(order_id) WHERE o.epoch_id=? AND x.from_at<? AND x.to_at>=? ORDER BY x.from_at',(EPOCH,end,start))
    intervals=sorted((max(start,r['from_at']),min(end,r['to_at'])) for r in rows)
    merged=[]
    for a,b in intervals:
        if b<a:raise ValueError('outage_interval_invalid')
        if merged and a<=merged[-1][1]:merged[-1][1]=max(b,merged[-1][1])
        else:merged.append([a,b])
    durations=[b-a for a,b in merged]
    return dict(state='partial',complete=False,reason='outage_rows_are_sampled_intervals_not_open_outage_receipts',count=len(rows),
                sampled_seconds=sum(durations),longest_sampled_s=max(durations,default=0.),open=_unknown('open_outage_state_not_recorded'))


def _account_polls(q,start,end,now,window_closed):
    rows=q.rows('SELECT observed_at,payload_json FROM quota_account_snapshots WHERE observed_at>=? AND observed_at<? ORDER BY observed_at,snapshot_id',
                (start,end),cap=10000)
    failed=[];readings=[];reasons=Counter()
    for row in rows:
        if not _number(row['observed_at']):raise ValueError('poll_timestamp_invalid')
        doc=_load(row['payload_json'])
        if not isinstance(doc.get('windows'),dict):raise ValueError('poll_windows_invalid')
        if doc['windows']:readings.append(row['observed_at'])
        else:
            failed.append(row['observed_at']);reasons[doc.get('reason') or 'unknown']+=1
    partial=start<now-RETENTION_S
    newest=max(readings,default=None)
    return dict(state='partial' if partial else 'known',complete=not partial,lower_bound=partial,
                total=len(rows),with_windows=len(readings),failed=len(failed),by_reason=dict(reasons),
                last_failed_at=max(failed,default=None),last_success_at=newest,
                newest_reading_age_s=None if newest is None else now-newest,
                age_at_window_end_s=end-newest if window_closed and newest is not None else None,
                reason='retention_boundary' if partial else None,
                reading_definition='nonempty windows, not account-guard clearance',
                retention_caveat='Snapshots older than 30 days may be pruned; older windows have lower-bound counts.')


def _market_data(conn,budget,start,end,now):
    def unknown(reason):
        return dict(**_unknown(reason),lower_bound=True,funded_breaker=_unknown('not_visible_to_market_tool'))
    if conn is None:return unknown('market_source_unavailable')
    from .market_data import STOP_429
    try:
        reader=_Read(conn,budget)
        columns={r['name'] for r in reader.rows("SELECT name FROM pragma_table_info('calls')",cap=16)}
        if not {'at','outcome'}<=columns:return unknown('market_db_unreadable')
        extra=(',dial' if 'dial' in columns else '')+(',stop_until' if 'stop_until' in columns else '')
        rows=reader.rows('SELECT at,outcome'+extra+' FROM calls WHERE at>=? AND at<? ORDER BY at,rowid',(start,end),cap=MARKET_ROW_CAP)
    except _Limit as exc:return unknown('market_rows_capped' if str(exc)=='row_cap' else 'market_read_budget_exhausted')
    except sqlite3.Error:return unknown('market_db_unreadable')
    except _ERRORS:return unknown('market_data_invalid')
    outcomes=Counter();dials=Counter();others=Counter();stops=[];inferred=0
    non_dials={'cache_hit','paced','budget_exhausted','provider_backoff'}
    for row in rows:
        if not _number(row['at']) or row['outcome'] is not None and not isinstance(row['outcome'],str):
            return unknown('market_data_invalid')
        outcome=row['outcome'] or 'outcome_unknown'
        outcomes[outcome]+=1
        dial=row.get('dial')
        if dial is None:
            inferred+=1;dial=int(outcome not in non_dials)
        elif type(dial) is not int or dial not in (0,1):return unknown('market_data_invalid')
        (dials if dial else others)[outcome]+=1
        stop=row.get('stop_until')
        if stop is not None:
            if not _number(stop) or stop<STOP_429:return unknown('market_data_invalid')
            stops.append(dict(start=stop-STOP_429,end=stop,start_basis='response_time'))
        elif outcome=='provider_rate_limited':
            stops.append(dict(start=row['at'],end=row['at']+STOP_429,start_basis='reservation_time_lower_bound'))
    covered=start>=now-MARKET_RETENTION_S
    basis='dial_column'
    if 'dial' not in columns or rows and inferred==len(rows):basis='inferred_from_legacy_outcome'
    elif inferred:basis='mixed_column_and_legacy_inference'
    return dict(state='partial',complete=False,read_complete=True,lower_bound=True,total=len(rows),by_outcome=dict(outcomes),
                dials=sum(dials.values()),dials_by_outcome=dict(dials),other_by_outcome=dict(others),
                dial_basis=basis,
                inferred_classification_rows=inferred,
                count_definition='Recorded rows include reservations and best-effort non-dial events; not requests received by the provider.',
                activity='calls recorded' if rows else 'no calls recorded',
                stop_windows=stops[:MARKET_STOP_CAP],
                stops_truncated=len(stops)>MARKET_STOP_CAP,stops_omitted=max(0,len(stops)-MARKET_STOP_CAP),
                row_cap=MARKET_ROW_CAP,stop_cap=MARKET_STOP_CAP,retention_s=MARKET_RETENTION_S,retention_covered=covered,
                coverage_note='Non-dial events are best effort. Nominal retention is 30 days; protected epochs may retain longer. Proxy groups are not measured provider requests.',
                funded_breaker=_unknown('not_visible_to_market_tool'))


def _by_release(changes):
    if changes.get('state')!='known':return _unknown('segment_log_unknown')
    groups={}
    for segment in changes['items']:
        group=groups.setdefault(segment['release_sha'],dict(release_sha=segment['release_sha'],segments=[],orders=0,fills=0,wakes=0,parks=0))
        group['segments'].append(segment['segment'])
        for metric in ('orders','fills','wakes','parks'):
            value=segment.get(metric)
            group[metric]=group[metric]+value if group[metric] is not None and type(value) is int else None
    items=list(groups.values())
    known=all(r[k] is not None for r in items for k in ('orders','fills','wakes','parks'))
    return dict(state='known' if known else 'partial',complete=known,items=items,
                reason=None if known else 'segment_counts_unknown',count=len(items))


def _seats(o,q,start,end,changes,window_closed):
    raw=q.rows('SELECT actor_id,payload_json FROM quota_wakes ORDER BY ended_at',cap=10000)
    docs=[_load(r['payload_json']) for r in raw]
    docs=[r for r in docs if r.get('epoch_id')==EPOCH]
    roster=o.rows('SELECT actor_id,generation FROM office_v2_roster WHERE epoch_id=?',(EPOCH,))
    pairs=sorted({(r['actor_id'],r['generation']) for r in docs+roster})
    if len(pairs)>32:raise _Limit('seat_cap')
    items=[]
    for actor,generation in pairs:
        cohort=[r for r in docs if r['actor_id']==actor and r['generation']==generation and start<=r['ended_at']<end]
        counters={k:sum(r['counters'][k] for r in cohort) if all(type(r.get('counters',{}).get(k)) is int and r['counters'][k]>=0 for r in cohort) else None
                  for k in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens')}
        items.append(dict(actor_id=actor,generation=generation,proof=proof_report(q,actor_id=actor,epoch_id=EPOCH,generation=generation,
                     window_start=start,window_end=end,window_closed=window_closed),counters=counters,
                     counted_tokens=sum(counters[k] for k in ('input_tokens','output_tokens','cache_creation_input_tokens')) if all(v is not None for v in counters.values()) else None,
                     by_segment=dict(Counter(str(_segment(changes,r['ended_at'])) for r in cohort)),
                     wake_types=_unknown('wake_type_not_recorded_in_quota_wakes'),no_useful_action=_unknown('useful_action_not_recorded'),
                     calibration=dict(wakes=[r['wake_id'] for r in cohort if r.get('calibration')],sdk_versions=sorted({r['sdk_version'] for r in cohort if r.get('sdk_version')}))))
    events=o.rows('SELECT actor_id,corrected_at,text,refs_json,state FROM office_v2_events WHERE epoch_id=? AND kind=? AND corrected_at>=? AND corrected_at<? ORDER BY corrected_at,seq',
                  (EPOCH,'lifecycle',start,end))
    parks=[];swaps=[]
    for e in events:
        refs=_load(e['refs_json'] or '{}')
        if e['text']=='parked':parks.append(dict(actor_id=e['actor_id'],at=e['corrected_at'],reason=refs.get('reason','unknown'),segment_id=_segment(changes,e['corrected_at'])))
        if e['text'] in ('swap','session_reset'):
            a,b=refs.get('business_disabled_at'),refs.get('business_enabled_at')
            swaps.append(dict(actor_id=e['actor_id'],at=e['corrected_at'],trigger=refs.get('trigger'),
                              unavailable_s=b-a if _number(a) and _number(b) and b>=a else None))
    halts=o.rows('SELECT action,actor_id,reason,at,book_state FROM office_v2_halts WHERE epoch_id=? AND at>=? AND at<? ORDER BY at',(EPOCH,start,end))
    return dict(state='known',complete=True,items=items,parks=parks,parks_by_reason=dict(Counter(r['reason'] for r in parks)),swaps=swaps,head_actions=halts,
                calibration_scope_proof=_unknown('calibration_host_configuration_not_supplied'))


def explore_report(office_conn,ledger_conn,quota_conn,*,now,window_start=None,window_end=None,epoch_id=EPOCH,market_conn=None,sol_usd=None,sol_at=None):
    if epoch_id!=EPOCH or not _number(now):return _unknown('window_bounds_unknown')
    budget=dict(queries=0,bytes=0)
    o,l,q=(_Read(c,budget) for c in (office_conn,ledger_conn,quota_conn))
    try:
        epoch=o.rows('SELECT payload_json FROM epochs WHERE id=?',(EPOCH,),cap=1)
        if not epoch:return _unknown('epoch_missing')
        epoch=_load(epoch[0]['payload_json'])
        if epoch.get('epoch_id')!=EPOCH or epoch.get('kind')!='v2_explore' or epoch.get('exploratory') is not True:
            return _unknown('epoch_identity_unknown')
        if window_start is None and window_end is None:
            window_start,window_end=epoch.get('opened_at'),epoch.get('closed_at') or now
        closed=epoch.get('closed_at')
        if closed is not None and (not _number(closed) or window_end is not None and _number(window_end) and window_end>closed):return _unknown('window_bounds_unknown')
        if (not _number(window_start) or not _number(window_end) or not window_start<window_end<=now
                or window_end-window_start>31*86400 or window_start<epoch['opened_at']):return _unknown('window_bounds_unknown')
    except _ERRORS:return _unknown('epoch_unavailable_or_invalid')
    start,end=window_start,window_end
    changes=_section(lambda:_segments(o,start,end,now))
    orders=_section(lambda:_orders(o,l,start,end,changes,epoch))
    book=_section(lambda:_book(l,epoch,start,end,now,changes,sol_usd=sol_usd,sol_at=sol_at))
    window_closed=end<now or epoch.get('closed_at') is not None
    seats=_section(lambda:_seats(o,q,start,end,changes,window_closed))
    seats['account_polls']=_section(lambda:_account_polls(q,start,end,now,window_closed))
    seats['market_data']=_market_data(market_conn,budget,start,end,now)
    roster=_section(lambda:dict(state='known',items=o.rows('SELECT actor_id,role,generation,incarnation,sealed FROM office_v2_roster WHERE epoch_id=?',(EPOCH,))))
    self_count=orders.get('by_source',{}).get('self',0) if orders.get('complete') and not orders.get('label_missing') else None
    taints=[r.get('tainted_by_web') for r in orders.get('items',[])]
    integrity=dict(state='partial',writer_audit=dict(**_unknown('brain_audit_source_unavailable'),run_by='Gamma weekly bounded coverage tooling'),
        walls=_unknown('complete_wall_counter_source_unavailable'),
        kill_conditions={k:_unknown('kill_rule_source_or_continuity_unavailable') for k in ('drawdown_200','readiness_zero_tolerance','funded_boundary','wall_reached','equity_unknown_24h')},
        taint_share=sum(v is True for v in taints)/len(taints) if taints and all(type(v) is bool for v in taints) else None,
        operator_influenced=sum(r['operator_influenced'] is True for r in orders.get('items',[])) if orders.get('complete') and all(type(r.get('operator_influenced')) is bool for r in orders.get('items',[])) else None)
    if changes.get('state')=='known':
        segment_ids={c['segment'] for c in changes['items']}
        order_segments_known=orders.get('complete') and not orders.get('label_missing') and all(
            type(r.get('segment_id')) is int and r['segment_id'] in segment_ids for r in orders.get('items',[]))
        for change in changes['items']:
            segment=change['segment']
            change['exit_discretion']='recorded' if any(c['kind']=='judge_exit' and c['segment']<=segment for c in changes['items']) else 'exit discretion not measured'
            change['orders']=sum(r.get('segment_id')==segment for r in orders.get('items',[])) if order_segments_known else None
            change['fills']=sum(r.get('segment_id')==segment and r['filled_at'] is not None for r in orders.get('items',[])) if change['orders'] is not None else None
            change['wakes']=sum(r['by_segment'].get(str(segment),0) for r in seats.get('items',[])) if seats.get('complete') else None
            change['parks']=sum(r.get('segment_id')==segment for r in seats.get('parks',[])) if seats.get('complete') else None
    if book.get('drawdown_usd') is not None:
        integrity['kill_conditions']['drawdown_200']=dict(state='reached' if book['drawdown_usd']>=200 else 'not-reached',observed_at=book.get('safety_observed_at'))
    out=dict(state='partial',exploratory=True,labelled='exploratory, no inference',epoch_id=EPOCH,now=now,window_start=start,window_end=end,
        epoch={k:epoch.get(k) for k in ('epoch_id','opened_at','closed_at','first_order_at','kind','release_sha','record_sha256','evaluation_allowed','exploratory')},
        tools_unwired=_unknown('tools_unwired_not_persisted_in_roster'),roster=roster,segments=changes,by_release=_by_release(changes),orders=orders,candidates=_section(lambda:_candidates(o,l,start,end)),refusals=_section(lambda:_refusals(o,start,end,changes)),
        coverage=_unknown('complete_host_feed_not_in_supplied_databases'),book=book,outages=_section(lambda:_outages(l,start,end)),seats=seats,integrity=integrity,
        null_arm=dict(**_unknown('complete_feed_path_fill_inputs_unavailable'),offline_source='Gamma computes null_arm.run from desk feed/tape with the paper fill model, read-only',labelled='exploratory, no inference',self=dict(count=self_count,null_counterpart=None),
                      office_total_book_net=book.get('office_net'),office_feed_only_net=_unknown('source_attributed_equity_unavailable')),
        source_consistency='logical cut across three independent connections; not an atomic cross-database snapshot',read_budget=budget)
    return _fit(out)
