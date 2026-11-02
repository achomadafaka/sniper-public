"""Bounded, read-only operator views. Official economics come only from released sealed_views."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
from pathlib import Path
import sqlite3
import time

from sniper.office import ledger
from sniper.office.memory import Refused as IntegrityRefused
from sniper.office.memory_store import _verified as _verify_selected_row

# shortcut: latest 200 rows per public collection; add paging when operator history exceeds this window.
LIMIT = 200
READ_BUDGET = ContextVar('office_read_budget',default=None)


def _charge(size=0):
    budget=READ_BUDGET.get()
    if budget is not None:
        if time.monotonic()>budget['deadline'] or budget['bytes']+size>1048576:
            budget['exhausted']=True
            raise ValueError('office_read_budget')
        budget['bytes']+=size


class _ReadCursor(sqlite3.Cursor):
    def _row(self,row):
        if row is not None:
            _charge(sum(len(v.encode('utf-8')) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 8 for v in row))
        return row

    def __next__(self):
        return self._row(super().__next__())

    def fetchone(self):
        return self._row(super().fetchone())

    def fetchall(self):
        return list(self)


class _ReadConnection(sqlite3.Connection):
    def execute(self,*args,**kwargs):
        _charge()
        budget=READ_BUDGET.get()
        if budget is not None:
            if budget['queries']>=512:
                budget['exhausted']=True
                raise sqlite3.OperationalError('office_query_budget')
            budget['queries']+=1
        return self.cursor(factory=_ReadCursor).execute(*args,**kwargs)


def _public(value,depth=0):
    if depth>32:return False
    if isinstance(value,dict):
        return value.get('evaluation_derived') is not True and value.get('blind') is not True and all(_public(v,depth+1) for v in value.values())
    if isinstance(value,list):return all(_public(v,depth+1) for v in value)
    return True


TABLES = ('agents', 'heartbeats', 'turn_runs', 'messages', 'tickets', 'epochs', 'sealed_views', 'spend', 'opportunity_log', 'decisions')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@contextmanager
def _ro(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path.name)
    _charge()
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1,factory=_ReadConnection)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,65536)
    conn.row_factory = sqlite3.Row
    deadline = min(time.monotonic() + .08,(READ_BUDGET.get() or {}).get('deadline',float('inf')))
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def _recent(conn, table):
    if table not in TABLES:
        raise ValueError('unsupported_public_table')
    key = 'seq' if table in ('sealed_views','opportunity_log','decisions') else 'id'
    extra=',prev_hash,row_hash' if table=='sealed_views' else ''
    rows = conn.execute('SELECT '+key+',substr(payload_json,1,32769)'+extra+' FROM '+table+' ORDER BY rowid DESC LIMIT ?', (LIMIT+1,)).fetchall()
    items, omitted = [], 0
    for stored in rows[:LIMIT]:
        identity,raw=stored[:2]
        if table=='sealed_views':
            try:
                if len(raw)>32768:
                    raise IntegrityRefused('oversized_selected_release')
                _verify_selected_row(conn,'sealed_views',identity,raw,stored[2],stored[3])
            except (IntegrityRefused,ValueError,TypeError,RecursionError):
                return dict(state='read_error',items=[],limited=len(rows)>LIMIT,omitted=0,reason='sealed_view_integrity')
        try:
            if len(raw) > 32768:
                raise ValueError('oversized_public_row')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('invalid_public_row')
            item=dict(payload,row_id=str(identity))
            if table=='sealed_views':
                item['view_hash']=stored[3]
            items.append(item)
        except (ValueError, TypeError, RecursionError):
            omitted += 1
    return dict(state='partial' if omitted else 'known', items=items, limited=len(rows)>LIMIT, omitted=omitted)


def _heartbeat(payload, now, timeout):
    at = payload.get('at') if isinstance(payload, dict) else None
    age = now-at if _number(now) and _number(at) and at <= now else None
    if timeout is None and isinstance(payload,dict) and payload.get('timeout_measured') is True:
        timeout=payload.get('stale_after_s')
    timeout = timeout if _number(timeout) and timeout > 0 else None
    return dict(heartbeat_at=at if _number(at) else None, heartbeat_age_s=age,
                heartbeat_state='unknown' if age is None or timeout is None else 'stale' if age>timeout else 'fresh')


def _text(value, limit=2000):
    return value[:limit] if isinstance(value, str) else None


def _read_state(state_root, *, now, stale_after_s=None, sol_usd=None, sol_at=None, host_now=None):
    root = Path(state_root)
    host_now=time.time() if host_now is None else host_now
    now = now if _number(now) and 0<=now<=253402300799 else None
    state = dict(mode=None,clock=dict(state='known' if now is not None else 'unknown', now=now),
                 office=dict(state='missing'), ledger=dict(state='missing'),
                 agents=dict(state='missing', items=[]), messages=dict(state='missing', items=[]),
                 tickets=dict(state='missing', items=[]), epochs=dict(state='missing', items=[]),
                 sealed_views=dict(state='missing', items=[]), models=dict(state='unknown', items=[]),
                 positions=dict(state='missing', items=[]), equity=dict(state='unknown', books=[]),
                 frontiers=dict(state='unknown', decisions_through=None), guards=dict(state='unknown', counters=None),
                 credits=dict(state='unreleased', items=[]), head_commentary=None, monthly_cost_usd=800.)
    try:
        with _ro(root / 'office.db') as conn:
            data = {table: _recent(conn, table) for table in TABLES}
            beats = {row['row_id']: row for row in data['heartbeats']['items']}
            turns = {}
            for row in data['turn_runs']['items']:
                turns.setdefault(row.get('actor_id'), row)
            agents = []
            for row in data['agents']['items']:
                turn = turns.get(row['row_id'], {})
                if row.get('generation') is not None and turn.get('generation') != row['generation']:
                    turn = {}
                value = turn.get('value') if isinstance(turn.get('value'), dict) else {}
                private_output=not _public([row,turn,value])
                persona = row.get('persona') if isinstance(row.get('persona'), dict) else {}
                agents.append(dict(agent_id=row['row_id'], name=_text(row.get('name') or persona.get('name') or row['row_id'], 128),
                                   role=_text(row.get('role'),128), seat=_text(row.get('lane') or row.get('seat') or row['row_id'],128), pod=_text(row.get('pod'),128), provider=_text(row.get('provider'),128),
                                   model=_text(row.get('model'),128), state=_text(row.get('state'),64), generation=row.get('generation'),
                                   task='Private evaluation work' if private_output else _text(row.get('task') or turn.get('task') or value.get('task') or turn.get('reason') or value.get('reason')),
                                   desk_screen=None if private_output else _text(row.get('desk_screen') or value.get('summary') or value.get('text') or value.get('memo')),
                                   desk_screen_state='sealed' if private_output else 'public',
                                   turn_state=_text(turn.get('state'),64), last_refusal=None if private_output else _text(turn.get('reason') or value.get('reason'),200), served_model=None if private_output else _text(value.get('served_model'),128),
                                   served_model_basis=None if private_output else value.get('served_model_basis') if value.get('served_model_basis') in ('echoed','requested_pinned') else 'unknown',
                                   lease_generation=turn.get('lease_generation'),
                                   turn_deadline_at=turn.get('deadline_at') if _number(turn.get('deadline_at')) else None,
                                   **_heartbeat(beats.get(row.get('heartbeat_id',row['row_id'])) if row.get('generation') is None or beats.get(row.get('heartbeat_id',row['row_id']),{}).get('generation')==row['generation'] else None, now, stale_after_s)))
            state['agents'] = dict(data['agents'], items=agents)
            state['mode']=_text(beats.get('supervisor',{}).get('mode'),64)
            state['supervisor'] = _heartbeat(beats.get('supervisor'), now, stale_after_s)
            for table, fields in (('messages', ('kind','author','author_role','text','known_at','refs')),
                                  ('tickets', ('author','problem','expected_impact','done_when','state','known_at')),
                                  ('epochs', ('epoch_id','opened_at','kind','reason','evaluation_broken','closed_at')),
                                  ('sealed_views', ('epoch_id','kind','released_at','aggregates','source_heads','view_hash'))):
                state[table] = dict(data[table], items=[dict(row_id=row['row_id'], **{k:row.get(k) for k in fields}) for row in data[table]['items'] if not(table in ('messages','tickets') and not _public(row))])
            released = [r for r in state['sealed_views']['items'] if now is not None and _number(r.get('released_at')) and r['released_at'] <= now]
            state['sealed_views']['items'] = released
            credits=[]
            seen_credits=set()
            for view in released:
                a=view.get('aggregates')
                if view.get('kind')!='descriptive_credit' or not isinstance(a,dict) or a.get('additive') is not False or a.get('epoch_id')!=view.get('epoch_id') or a.get('epoch_kind') not in ('evaluated','discovery') or not isinstance(a.get('agent_id'),str) or not isinstance(a.get('metric'),str):
                    continue
                key=(a['epoch_id'],a['agent_id'],a['metric'])
                if key in seen_credits:
                    continue
                seen_credits.add(key)
                credits.append(dict(row_id=view['row_id'],released_at=view['released_at'],**{k:a.get(k) for k in ('agent_id','metric','value','sample_n','censored_n','note','epoch_id','epoch_kind','look_id','additive','top3_coin_share')}))
            state['credits']=dict(state='known' if credits else 'unreleased' if now is not None and state['sealed_views']['state']=='known' else 'unknown',items=credits)

            refs = {int(r['row_id']) for r in released}
            public_messages=[]
            for message in state['messages']['items']:
                if message.get('kind')=='head_report':
                    if not (message.get('author_role')=='Head' and _number(message.get('known_at')) and now is not None and message['known_at']<=now and isinstance(message.get('refs'),list) and message['refs'] and all(type(i) is int and i in refs for i in message['refs'])):
                        continue
                    if state['head_commentary'] is None:
                        state['head_commentary']=_text(message.get('text'))
                public_messages.append(message)
            state['messages']['items']=public_messages
            grouped = {}
            for call in data['spend']['items']:
                if not _public(call):continue
                key = (call.get('requested_provider') or 'unknown', call.get('requested_model') or 'unknown')
                group = grouped.setdefault(key,dict(provider=key[0], model=key[1], calls=0, waits_s=0., throttled_s=0., fallbacks=0))
                group['calls'] += 1
                for field, flag in (('waits_s','waiting_measured'),('throttled_s','throttling_measured')):
                    value = call.get('waiting_s' if field=='waits_s' else 'throttled_s')
                    if call.get(flag) is True and _number(value) and value>=0:
                        if group[field] is not None:
                            group[field] += value
                    else:
                        group[field] = None
                if type(call.get('labelled_fallback')) is bool:
                    if group['fallbacks'] is not None:
                        group['fallbacks'] += int(call['labelled_fallback'])
                else:
                    group['fallbacks'] = None
            state['models'] = dict(data['spend'], items=list(grouped.values()), window='latest_200_recorded_attempts')
            audit = beats.get('auditor',{})
            counters = audit.get('counters')
            if isinstance(counters,dict):
                state['guards'] = dict(state='reported', counters={str(k):v if type(v) is int and v>=0 else None for k,v in counters.items()}, **_heartbeat(audit,now,stale_after_s))
            state['office'] = dict(state='partial' if any(v['state']!='known' for v in data.values()) else 'known')
            from sniper.office.floor import project
            project(data,state,now)
            from sniper.office.seat_work import read_seat_work
            state['direct_messages'],state['seat_tasks']=read_seat_work(conn,state['agents']['items'],now=now)
            from sniper.office.reporting_view import read_daily
            epoch_scope=data['epochs']
            public_daily=(epoch_scope['state']=='known' and not epoch_scope.get('limited') and not epoch_scope.get('omitted') and bool(epoch_scope['items'])
                          and all(e.get('kind')=='discovery' and _public(e) and _number(e.get('opened_at')) and now is not None and e['opened_at']<=now for e in epoch_scope['items'])
                          and all(a.get('desk_screen_state')!='sealed' for a in state['agents']['items']))
            state['persisted_daily']=read_daily(conn,now=host_now,public_scope=public_daily,public_epochs={e.get('epoch_id',e.get('row_id')) for e in epoch_scope['items'] if e.get('kind')=='discovery' and _public(e) and _number(e.get('opened_at')) and now is not None and e['opened_at']<=now})
            from sniper.office.runtime_view import read_runtime
            state['runtime_calls'],state['runtime_handoffs']=read_runtime(conn,state['agents']['items'],now=now,
                public_scope=public_daily,public_epochs={e.get('epoch_id',e.get('row_id')) for e in epoch_scope['items'] if e.get('kind')=='discovery' and _public(e) and _number(e.get('opened_at')) and now is not None and e['opened_at']<=now})
            from sniper.office.request_view import read_requests
            state['request_admission'],state['near_pass_receipts']=read_requests(conn,data['turn_runs'],state['agents']['items'],now=now,
                public_scope=public_daily,public_epochs={e.get('epoch_id',e.get('row_id')) for e in epoch_scope['items'] if e.get('kind')=='discovery' and _public(e) and _number(e.get('opened_at')) and now is not None and e['opened_at']<=now})
            current_ids={a['agent_id'] for a in state['agents']['items']}
            typed_dms=set()
            for event in state['runtime_handoffs']['items']:
                if (event['from_actor_id'] in current_ids and event['to_actor_id'] in current_ids
                        and all(isinstance(event[k],str) and event[k] for k in ('from_record_kind','from_record_id','to_record_kind','to_record_id'))):
                    if event['kind']=='msg.send' and event['from_record_kind']=='receipts' and event['to_record_kind']=='office_dm_events':
                        typed_dms.add((event['from_actor_id'],event['to_actor_id'],event['from_record_id'],event['to_record_id']))
                    state['handoffs']['items'].append(dict(id='typed:'+event['event_id'],source=event['from_actor_id'],target=event['to_actor_id'],at=event['at'],kind=event['kind'],reference=event['from_record_kind']+':'+event['from_record_id']+' → '+event['to_record_kind']+':'+event['to_record_id']))
            for message in state['direct_messages']['items']:
                if (message['from_actor_id'],message['to_actor_id'],message['receipt_id'],message['message_id']) in typed_dms:continue
                state['handoffs']['items'].append(dict(id='dm:'+message['message_id'],source=message['from_actor_id'],target=message['to_actor_id'],at=message['created_at'],kind='Written DM',reference=message['message_id']+' / '+message['receipt_id']))
    except (FileNotFoundError, sqlite3.Error, ValueError, TypeError) as exc:
        state['office'] = dict(state='missing' if isinstance(exc,FileNotFoundError) else 'read_error', reason=type(exc).__name__)
    try:
        with _ro(root / 'ledger' / 'ledger.db' if (root / 'ledger').exists() else root / 'ledger.db') as conn:
            fx_source='injected_local_observation'
            if now is not None and sol_usd is None and sol_at is None:
                candidates=[r for r in conn.execute('SELECT id,at,fx_usd,fx_at FROM cashflows ORDER BY id DESC LIMIT ?', (LIMIT,)) if _number(r['at']) and r['at']<=now and _number(r['fx_usd']) and r['fx_usd']>0 and _number(r['fx_at']) and 0<=now-r['fx_at']<=60.]
                if candidates:
                    fx=max(candidates,key=lambda r:r['fx_at'])
                    sol_usd,sol_at=fx['fx_usd'],fx['fx_at']
                    fx_source='settled_paper_fill'
            positions = []
            rows = conn.execute('SELECT order_id,book_id,epoch_id,book_kind,network_id,mint,state,requested_at,filled_at,tokens_atomic,spend_usd,fill_model_version,closed_at,exit_usd,costs_usd FROM orders ORDER BY rowid DESC LIMIT ?', (LIMIT+1,)).fetchall()
            for raw in rows[:LIMIT]:
                row = dict(raw)
                row['observed_after_cut']=now is None or any(_number(row.get(k)) and row[k]>now for k in ('requested_at','filled_at','closed_at'))
                mark = conn.execute('SELECT at,source,reason,lamports FROM marks WHERE order_id=?'+(' AND at<=?' if now is not None else '')+' ORDER BY at DESC,id DESC LIMIT 1', (row['order_id'],now) if now is not None else (row['order_id'],)).fetchone()
                row['mark_lamports'] = mark['lamports'] if mark else None
                row['mark_source'] = mark['source'] if mark else None
                row['mark_reason'] = mark['reason'] if mark else None
                row['mark_age_s'] = now-mark['at'] if mark and _number(now) and _number(mark['at']) and mark['at']<=now else None
                amount=mark['lamports'] if mark else None
                row['mark_state']='unknown' if row['mark_age_s'] is None else 'stale' if row['mark_age_s']>60 else 'unpriced' if not isinstance(amount,str) or not amount.isdigit() else 'priced'
                positions.append(row)
            state['positions'] = dict(state='known',items=positions,limited=len(rows)>LIMIT)
            from sniper.office.floor import bounded_books, attributed_stats
            if 'stats' in state:
                attributed_stats(conn,state,positions,now)
            state['equity']=bounded_books(conn,positions,now=now,sol_usd=sol_usd,sol_at=sol_at,orders_limited=len(rows)>LIMIT)
            from sniper.office.reporting_view import read_equity_history
            state['equity_history']=read_equity_history(conn,state['equity']['books'],now=host_now)
            state['ledger']=dict(state='known',fx=dict(source=fx_source if _number(sol_usd) and _number(sol_at) else None,sol_usd=sol_usd if _number(sol_usd) else None,at=sol_at if _number(sol_at) else None))
    except (FileNotFoundError, sqlite3.Error, ValueError, TypeError) as exc:
        state['ledger']=dict(state='missing' if isinstance(exc,FileNotFoundError) else 'read_error',reason=type(exc).__name__)
    from sniper.office.floor import trackers
    state['health'],state['funnel'],state['budget']=trackers(root,host_now=host_now,now=now)
    for key in ('activity','stats','providers','handoffs','direct_messages','seat_tasks','persisted_daily','equity_history','runtime_calls','runtime_handoffs','request_admission','near_pass_receipts'):
        state.setdefault(key,dict(state='unknown',items=[],coverage='observed_window'))
    state['open_bags']=open_bags(state)
    return state


def read_state(state_root, *, now, stale_after_s=None, sol_usd=None, sol_at=None, host_now=None):
    start=time.monotonic()
    budget=dict(deadline=start+1.,queries=0,bytes=0,exhausted=False)
    token=READ_BUDGET.set(budget)
    try:
        state=_read_state(state_root,now=now,stale_after_s=stale_after_s,sol_usd=sol_usd,sol_at=sol_at,host_now=host_now)
        source_root=Path(__file__).resolve().parents[2].name
        valid_sha=lambda value: isinstance(value,str) and len(value)==40 and all(c in '0123456789abcdef' for c in value)
        runtime_sha=state['budget'].pop('runtime_sha',None)
        state['identity']=dict(served_web_sha=source_root if valid_sha(source_root) else None,runtime_sha=runtime_sha if valid_sha(runtime_sha) else None,runtime_identity='declared_config_only',readiness='not_established_by_ui')
        if len(json.dumps(state).encode())>2097152:
            budget['exhausted']=True
            for key,value in state.items():
                if isinstance(value,dict) and ('items' in value or 'books' in value):
                    value.update(state='unknown',reason='response_byte_budget')
                    if 'items' in value:value['items']=[]
                    if 'books' in value:value['books']=[]
            state['head_commentary']=None
        state['read_budget']=dict(queries=budget['queries'],bytes=budget['bytes'],exhausted=budget['exhausted'],elapsed_ms=round((time.monotonic()-start)*1000,1))
        return state
    finally:
        READ_BUDGET.reset(token)


def _usd(value):
    return ('-$' if value<0 else '$')+format(abs(value),'.2f') if _number(value) else 'unknown'


def open_bags(state):
    """Per book: unsold open positions in the loaded window, oldest age since fill and how many are not priced
    (Gamma Finding 2: a pod stuck in bags is not neutral). Unknown positions, clock or a malformed row make the whole answer unknown, never 0."""
    pos=state.get('positions') or {}
    if pos.get('state')!='known':
        return dict(state='unknown',items=[],limited=False)
    now=(state.get('clock') or {}).get('now')
    unknown=dict(state='unknown',items=[],limited=False)
    if not _number(now):
        return unknown                                  # no clock: the reader marks every row after the cut (Zeta)
    books={}
    for r in pos['items']:
        if not isinstance(r,dict) or not isinstance(r.get('book_id'),str) or not r['book_id']:
            return unknown                              # a malformed row is unknown, never a crash (Gamma)
        if r.get('state')!='open' or r.get('observed_after_cut'):
            continue
        b=books.setdefault(r['book_id'],dict(book_id=r['book_id'],open_n=0,oldest_age_s=None,unpriced_n=0,_unknown=False))
        b['open_n']+=1
        b['unpriced_n']+=r.get('mark_state')!='priced'
        age=now-r['filled_at'] if _number(r.get('filled_at')) and r['filled_at']<=now else None
        if age is None:
            b['_unknown']=True
        elif b['oldest_age_s'] is None or age>b['oldest_age_s']:
            b['oldest_age_s']=float(age)
    items=[]
    for b in sorted(books.values(),key=lambda b:b['book_id']):
        if b.pop('_unknown'):
            b['oldest_age_s']=None                      # one bag of unknown age makes the oldest unknown
        items.append(b)
    return dict(state='known',items=items,limited=bool(pos.get('limited')))


def _bags_line(bags):
    if bags['state']!='known':
        return 'Open bags: unknown.'
    if not bags['items']:
        return 'Open bags: none in loaded window'+(' (window truncated; older rows unread).' if bags['limited'] else '.')
    parts=['%s %s%d (oldest %s, %d not priced)'%(b['book_id'],'at least ' if bags['limited'] else '',b['open_n'],
           'unknown' if b['oldest_age_s'] is None else '%.1fh'%(b['oldest_age_s']/3600),b['unpriced_n']) for b in bags['items']]
    return 'Open bags: '+'; '.join(parts)+'.'


def daily(state):
    """Deterministic released facts; the Head's text remains a labelled quote."""
    agents=state['agents']['items']
    agent_count=str(len(agents)) if state['agents']['state']=='known' else 'unknown'
    position_count=str(len(state['positions']['items'])) if state['positions']['state']=='known' else 'unknown'
    lines=['Office daily snapshot', 'Office store: '+state['office']['state']+'; ledger: '+state['ledger']['state']+'; equity: '+state['equity']['state']+'.', 'Agents in loaded window: '+agent_count+'. Positions in loaded window: '+position_count+'.']
    lines.append(_bags_line(open_bags(state)))
    views=[v for v in state['sealed_views']['items'] if v.get('kind')=='epoch_economics' and isinstance(v.get('aggregates'),dict)]
    if views:
        view=views[0]; a=view['aggregates']
        lines += ['Released epoch '+str(view['epoch_id'])+' (sealed_views '+view['row_id']+'): '+str(a.get('opportunity_count','unknown'))+' eligible; '+str(a.get('entered_count','unknown'))+' entered.',
                  'Trading net '+_usd(a.get('net_pnl_usd'))+'; fixed Office cost '+_usd(a.get('cost_usd'))+'; Office net '+_usd(a.get('office_net_usd'))+'.']
    else:
        lines.append(('Economics unreleased.' if state['sealed_views']['state']=='known' else 'Economics unavailable.')+' Fixed Office cost: $800/month.')
    if state.get('head_commentary'):
        commentary='\n'.join('> '+line for line in state['head_commentary'].splitlines())[:2000]
        lines += ['Head commentary (unverified):',commentary]
    return '\n'.join(lines)


def send_daily(state, *, send_alert):
    return send_alert(kind='office-daily',text=daily(state))
