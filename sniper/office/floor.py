"""Public Office projections; bounded observations are never historical totals."""
from datetime import datetime, timezone
import json
import time

from sniper.office.launcher_client import read_regular


def project(data, state, now):
    from sniper.office.report import _number, _text, _public
    public = _public
    agents={r['agent_id']:r for r in state['agents']['items']}
    events=[]; stats={}
    def sheet(actor, at):
        if not isinstance(actor,str) or not _number(at) or now is None or at>now:
            return None
        try: day=datetime.fromtimestamp(at,timezone.utc).date().isoformat()
        except (ValueError,OverflowError,OSError): return None
        return stats.setdefault((actor,day),dict(agent_id=actor,day=day,turns=0,calls=None,cost_usd=None,decisions=None,hit_rate=None,paper_pnl_usd=None,open_positions=None,reason='No complete call or attributed outcome ledger'))
    for row in data['turn_runs']['items']:
        if not public(row):continue
        actor=row.get('actor_id');at=row.get('created_at');value=row.get('value')
        value=value if isinstance(value,dict) else {}
        item=sheet(actor,at)
        if item is not None:item['turns']+=1
        agent=agents.get(actor,{})
        label=agent.get('name') or actor or 'Unknown agent'
        reason=_text(row.get('reason') or value.get('reason'),200)
        if _number(at) and now is not None and at<=now:
            events.append(dict(id='turn:'+row['row_id'],at=at,actor_id=actor,kind='turn',text=f"{label}: turn {row.get('state','unknown')}"+(f" — {reason.replace('_',' ')}" if reason else ''),ref=_text(row.get('decision_id'),128)))
    for row in data['messages']['items']:
        if not public(row) or row.get('kind')=='head_report':continue
        req=row.get('request');req=req if isinstance(req,dict) else {}
        payload=req.get('payload',req);payload=payload if isinstance(payload,dict) else {}
        actor=row.get('actor_id') or row.get('author');at=row.get('known_at')
        text=_text(row.get('text') or payload.get('memo') or payload.get('task') or payload.get('problem'))
        if text and _number(at) and now is not None and at<=now:
            events.append(dict(id='message:'+row['row_id'],at=at,actor_id=actor,kind=_text(row.get('kind') or req.get('kind') or req.get('tool') or 'memo',64),text=f"{agents.get(actor,{}).get('name') or actor or 'Unknown agent'}: {text}",ref=_text(row.get('decision_id'),128)))
    for row in data['tickets']['items']:
        at=row.get('known_at');actor=row.get('author');text=_text(row.get('problem'))
        if public(row) and text and _number(at) and now is not None and 0<=at<=now:
            events.append(dict(id='ticket:'+row['row_id'],at=at,actor_id=actor,kind='proposal',text=f"{agents.get(actor,{}).get('name') or actor or 'Unknown agent'} proposed: {text}",ref=row['row_id']))
    for row in data['spend']['items']:
        if not public(row):continue
        item=sheet(row.get('actor_id'),row.get('at',row.get('created_at')))
        if item is None:continue
        item['calls']=(item['calls'] or 0)+1
        cost=row.get('cost_usd')
        item.setdefault('_cost_complete',True)
        if _number(cost) and cost>=0:item['cost_usd']=(item['cost_usd'] or 0)+cost
        else:item['_cost_complete']=False
    for item in stats.values():
        if not item.pop('_cost_complete',False):item['cost_usd']=None
    state['activity']=dict(state=state['office']['state'],items=sorted(events,key=lambda e:e['at'],reverse=True)[:200],limited=any(data[k]['limited'] for k in ('turn_runs','messages')),coverage='observed_window')
    state['stats']=dict(state=state['office']['state'],items=sorted(stats.values(),key=lambda r:(r['day'],r['agent_id']),reverse=True),limited=any(data[k]['limited'] for k in ('turn_runs','spend')),coverage='observed_window')
    state['funnel_office']={k:dict(state=data[k]['state'],observed_count=sum(public(r) for r in data[k]['items']),limited=data[k]['limited']) for k in ('opportunity_log','decisions')}
    turns=data['turn_runs']['items']
    state['frontiers']=dict(state='observed_window',decisions_through=None,oldest_pending_s=None,partial_count=sum(r.get('state')=='partial' for r in turns if public(r)),limited=data['turn_runs']['limited'] or data['turn_runs']['omitted']>0)
    state['handoffs']=dict(state='observations_only',items=[])
    # A shared decision id is context, not proof that one seat handed work to another.
    messages={r['row_id']:r for r in data['messages']['items'] if public(r)}
    for row in data['messages']['items']:
        source=row.get('from_actor_id');target=row.get('to_actor_id')
        refs=row.get('refs')
        origin=messages.get(refs[0]) if isinstance(refs,list) and refs and isinstance(refs[0],str) else None
        reference_ok=origin is not None and origin.get('actor_id')==source and _number(origin.get('known_at')) and _number(row.get('known_at')) and origin['known_at']<=row['known_at']
        if reference_ok and public(row) and source in agents and target in agents and isinstance(row.get('refs'),list) and row['refs'] and _number(row.get('known_at')) and now is not None and row['known_at']<=now:
            state['handoffs']['items'].append(dict(id=row['row_id'],source=source,target=target,at=row['known_at'],kind=_text(row.get('kind'),64),reference=_text(str(row['refs'][0]),128)))
    state['providers']=dict(state='observations_only',items=[dict(provider=p,reason=a.get('last_refusal') or 'No recorded provider outcome',turn_state=a.get('turn_state'),agent_id=a['agent_id'],served_model=a.get('served_model'),served_model_basis=a.get('served_model_basis')) for a in agents.values() if (p:=a.get('provider'))])


def trackers(root, *, host_now=None, now=None):
    from sniper.office.report import _number, _text, _ro, _charge, LIMIT
    import sqlite3
    host_now=time.time() if host_now is None else host_now
    control=root.parent/'office-control'
    health={'clock_basis':'host_utc','at':host_now}
    for key,path,fields,stamp in (
        ('monitor',control/'health.json',('telemetry_error','cpu_psi_some_avg10','io_psi_some_avg10','mem_available_bytes','funded_heartbeat_age_s','base_caps_applied','roster_valid','resources_verified','guard_policy_version'),'ts'),
        ('guard',control/'guard'/'guard.json',('state','reason','healthy_since','resume_eligible','guard_policy_version'),'at')):
        try:
            raw=read_regular(path);_charge(len(raw))
            row=json.loads(raw)
            if not isinstance(row,dict):raise ValueError('not_object')
            # Only scalar public metadata; never pass arbitrary producer objects to the browser.
            item={k:_text(row[k]) if isinstance(row.get(k),str) else row.get(k) for k in fields if type(row.get(k)) in (str,int,float,bool,type(None))}
            if key=='guard':
                for diagnostic in ('termination','population'):
                    part=row.get(diagnostic)
                    if isinstance(part,dict):
                        item[diagnostic]={k:_text(part[k]) if isinstance(part[k],str) else part[k] if _number(part[k]) else None
                                          for k in ('state','at','error') if type(part.get(k)) in (str,int,float)}
            for field,value in tuple(item.items()):
                if type(value) in (int,float) and not _number(value):item[field]=None
            at=row.get(stamp);age=host_now-at if _number(at) and _number(host_now) and at<=host_now else None
            item.update(observed_at=at if _number(at) else None,age_s=age,freshness='unknown' if age is None else 'fresh' if age<=5 else 'stale',read_state='known')
        except (OSError,ValueError,TypeError,RecursionError):item=dict(state='missing' if not path.exists() else 'read_error',read_state='unknown',freshness='unknown')
        health[key]=item
    try:
        from sniper.office.desk_funnel import read_funnel
        funnel=read_funnel(root,now=now,host_now=host_now,open_ro=_ro,charge=_charge)
    except ImportError:
        funnel=dict(state='unknown',items=[],sources=[],limited=None,coverage='last 200 tickets, all stages',tickets=[],blockers=[])
    funnel['hard_filter_state']=funnel['state']
    funnel['hard_filter_count']=sum(row['count'] for row in funnel['items'] if type(row.get('count')) is int) if funnel['state'] in ('known','partial') else None
    try:
        with _ro(root.parent/'desk'/'intake_emitter.db') as conn:
            rows=conn.execute('SELECT feed,filter,reason,at_host FROM outcomes ORDER BY rowid DESC LIMIT ?', (LIMIT+1,)).fetchall()
            groups={}
            for row in rows[:LIMIT]:
                if not _number(row['at_host']) or row['at_host']>host_now:continue
                key=tuple(_text(row[k],128) for k in ('feed','filter','reason'))
                groups[key]=groups.get(key,0)+1
            funnel['items'] += [dict(source='Desk filter',stage=k[0]+'/'+k[1],seat=None,verdict=k[2],count=v) for k,v in groups.items()]
            funnel['sources'].append(dict(source='Desk filter',state='known',limited=len(rows)>LIMIT))
    except (OSError,sqlite3.Error,ValueError,TypeError):
        funnel['sources'].append(dict(source='Desk filter',state='unknown'))
        funnel['items'].append(dict(source='Desk filter',stage='Intake eligibility',seat=None,verdict='Unknown',count=None))
    day=datetime.fromtimestamp(now,timezone.utc).date().isoformat() if _number(now) else None
    for feed in ('pumpfun','dexscreener','fomo'):
        try:
            if day is None:raise ValueError('clock_unknown')
            with _ro(root.parent/'desk'/'intake'/feed/(day+'.db')) as conn:
                rows=conn.execute('SELECT id,observed_at FROM intake_rows ORDER BY id DESC LIMIT ?', (LIMIT+1,)).fetchall()
                count=len(rows[:LIMIT])
                funnel['items'].append(dict(source='Desk intake',stage=feed,seat=None,verdict='Recorded rows / '+day,count=count))
                funnel['sources'].append(dict(source=feed,state='known',limited=len(rows)>LIMIT))
        except (OSError,sqlite3.Error,ValueError,TypeError):
            funnel['sources'].append(dict(source=feed,state='unknown'))
            funnel['items'].append(dict(source='Desk intake',stage=feed,seat=None,verdict='Unavailable',count=None))
    statuses=[source['state'] for source in funnel['sources']]
    funnel['state']='known' if statuses and all(s=='known' for s in statuses) else 'partial' if any(s in ('known','partial') for s in statuses) else 'unknown'
    funnel['limited']=bool(funnel['limited']) or any(source.get('limited') is True for source in funnel['sources'])
    budget=dict(state='unknown',reason='No complete metered spend history',fixed_monthly_usd=800,limits={})
    try:
        raw=read_regular(root.parent.parent/'config'/'office'/'runtime.json');_charge(len(raw))
        config=json.loads(raw)
        if not isinstance(config,dict):raise ValueError('runtime_config_not_object')
        budget['runtime_sha']=str(config.get('content_addressed_release_root','')).rsplit('/',1)[-1]
        for key in ('host_children_max','turn_deadline_s','max_steps','lease_ttl_s','poll_s'):
            if _number(config.get(key)):budget['limits'][key]=config[key]
        if isinstance(config.get('provider_caps'),dict):budget['provider_caps']={p:n for p,n in config['provider_caps'].items() if p in ('claude','codex') and type(n) is int}
    except (OSError,ValueError,TypeError,RecursionError):pass
    return health,funnel,budget


def bounded_books(conn, positions, *, now, sol_usd, sol_at, orders_limited):
    from sniper.office.report import _number, LIMIT
    from sniper.office import ledger
    capitals=conn.execute('SELECT book_id,at,usd FROM capital_flows ORDER BY id DESC LIMIT ?', (LIMIT+1,)).fetchall()
    flows=conn.execute('SELECT order_id,at,usd FROM cashflows ORDER BY id DESC LIMIT ?', (LIMIT+1,)).fetchall()
    books=conn.execute('SELECT book_id,epoch_id,kind,opened_at FROM books ORDER BY rowid DESC LIMIT 33').fetchall()
    owner={p['order_id']:p['book_id'] for p in positions}
    complete=not(any(r['order_id'] not in owner for r in flows[:LIMIT]) or orders_limited or len(capitals)>LIMIT or len(flows)>LIMIT or any(p.get('observed_after_cut') for p in positions) or now is None or any(not _number(b['opened_at']) or b['opened_at']>now for b in books))
    result=[]
    for book in books[:32]:
        bid=book['book_id'];opened=[p for p in positions if p['book_id']==bid and p['state']=='open']
        cash=None
        if complete and now is not None:
            cash=sum(r['usd'] for r in capitals if r['book_id']==bid and r['at']<=now)+sum(r['usd'] for r in flows if owner.get(r['order_id'])==bid and r['at']<=now)
        fx=ledger._fx(sol_usd,sol_at,now,120.) if now is not None else None
        priced=[p for p in opened if p['mark_state']=='priced' and fx is not None]
        unpriced=[p for p in opened if p not in priced]
        liq=sum((int(p['mark_lamports'])-round(ledger.EXEC_COST_SOL['exit']*ledger.LAMPORTS))/ledger.LAMPORTS*fx for p in priced) if priced or not opened else None
        state='unknown' if not complete or cash is None or (opened and not priced) else 'partial' if unpriced else 'known'
        result.append(dict(book,cash=cash,known_liq_lower=liq,unpriced_count=len(unpriced),unpriced_qty=str(sum(int(p['tokens_atomic'] or '0') for p in unpriced)),state=state,coverage='complete_loaded_history' if complete else 'truncated_history'))
    return dict(state='unknown' if not result or any(b['state']=='unknown' for b in result) else 'partial' if len(books)>32 or any(b['state']=='partial' for b in result) else 'known',books=result,limited=not complete or len(books)>32)


def attributed_stats(conn, state, positions, now):
    """Only exact decision-receipt/order links; credit is descriptive and non-additive."""
    from sniper.office.report import _number, LIMIT
    rows=conn.execute('SELECT action_key,actor_id,order_id,created_at,outcome,reason,decision_id FROM decision_receipts ORDER BY rowid DESC LIMIT ?', (LIMIT+1,)).fetchall()
    stats={(r['agent_id'],r['day']):r for r in state['stats']['items']}
    orders={p['order_id']:p for p in positions}
    seen=set()
    for row in rows[:LIMIT]:
        at=row['created_at'];actor=row['actor_id']
        if not _number(at) or now is None or at>now:continue
        try:day=datetime.fromtimestamp(at,timezone.utc).date().isoformat()
        except (ValueError,OverflowError,OSError):continue
        item=stats.setdefault((actor,day),dict(agent_id=actor,day=day,turns=0,calls=None,cost_usd=None,decisions=0,hit_rate=None,paper_pnl_usd=None,open_positions=None,reason='Only exact decision receipts; outcome credit is non-additive'))
        item['decisions']=(item['decisions'] or 0)+1
        order=orders.get(row['order_id'])
        key=(actor,row['order_id'])
        if order is not None and key not in seen:
            seen.add(key)
            item['open_positions']=(item['open_positions'] or 0)+int(order['state']=='open')
            if order['state']=='closed' and _number(order.get('closed_at')) and 0<=order['closed_at']<=now and all(_number(order.get(k)) for k in ('exit_usd','spend_usd','costs_usd')):
                pnl=order['exit_usd']-order['spend_usd']-order['costs_usd']
                close_day=datetime.fromtimestamp(order['closed_at'],timezone.utc).date().isoformat()
                outcome=stats.setdefault((actor,close_day),dict(agent_id=actor,day=close_day,turns=0,calls=None,cost_usd=None,decisions=None,hit_rate=None,paper_pnl_usd=None,open_positions=None,reason='Linked closes on this UTC day; descriptive non-additive credit'))
                outcome['paper_pnl_usd']=(outcome['paper_pnl_usd'] or 0)+pnl
                outcome['closed_n']=outcome.get('closed_n',0)+1;outcome['wins_n']=outcome.get('wins_n',0)+int(pnl>0)
                outcome['hit_rate']=outcome['wins_n']/outcome['closed_n']
        name=next((a['name'] for a in state['agents']['items'] if a['agent_id']==actor),actor)
        state['activity']['items'].append(dict(id='risk:'+row['action_key'],at=at,actor_id=actor,kind='risk',text=f"{name}: risk {row['outcome']}"+(f" — {row['reason'].replace('_',' ')}" if row['reason'] else '')+(f"; paper order {row['order_id']}" if row['order_id'] else ''),ref=row['decision_id']))
    state['activity']['items']=sorted(state['activity']['items'],key=lambda r:r['at'],reverse=True)[:LIMIT]
    state['stats']['items']=sorted(stats.values(),key=lambda r:(r['day'],r['agent_id']),reverse=True)
    state['stats']['limited']=state['stats'].get('limited',False) or len(rows)>LIMIT
