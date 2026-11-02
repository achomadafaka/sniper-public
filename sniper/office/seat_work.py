"""Read-only current-generation seat DMs and task states from bounded typed event tails."""
import sqlite3

LIMIT=64


def read_seat_work(conn,agents,*,now):
    from sniper.office.report import _number
    current={a['agent_id']:a.get('generation') for a in agents if a.get('desk_screen_state')!='sealed'}
    def unknown(reason):return dict(state='unknown',items=[],limited=False,omitted=0,coverage='last 64 events, all seats',reason=reason)
    if not _number(now):return unknown('clock_unknown'),unknown('clock_unknown')
    def selected(table,columns):
        try:
            rows=conn.execute('SELECT '+columns+' FROM '+table+' ORDER BY seq DESC LIMIT ?', (LIMIT+1,)).fetchall()
            return rows[:LIMIT],dict(state='partial' if len(rows)>LIMIT else 'known',items=[],limited=len(rows)>LIMIT,omitted=0,coverage='last 64 events, all seats')
        except (sqlite3.Error,ValueError,TypeError):return [],unknown('source_unavailable')
    def bound(value,size):return isinstance(value,str) and len(value.encode('utf-8'))<=size
    def generation(actor,gen):return isinstance(actor,str) and type(gen) is int and current.get(actor)==gen
    rows,messages=selected('office_dm_events','seq,message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,receipt_id')
    for raw in rows:
        r=dict(raw)
        if (not _number(r['created_at']) or not 0<=r['created_at']<=now
                or not generation(r['from_actor_id'],r['from_generation']) or not generation(r['to_actor_id'],r['to_generation'])
                or not bound(r['message_id'],256) or not r['message_id'] or not bound(r['receipt_id'],256) or not r['receipt_id']
                or not bound(r['text'],4096) or not r['text']):
            messages['omitted']+=1;continue
        # Body and identity come from the typed host receipt. Reference payloads stay private to the producer.
        r['seq']=str(r['seq']);messages['items'].append(r)
    columns='seq,event_id,task_id,actor_id,generation,status,title,note,created_at,receipt_id'
    try:
        heads=conn.execute('SELECT task_id,actor_id,generation,status,event_id,title,updated_at FROM office_task_heads ORDER BY rowid DESC LIMIT ?', (LIMIT+1,)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table: office_task_heads' not in str(exc):return messages,unknown('task_projection_unavailable')
        rows,tasks=selected('office_task_events',columns)
        tasks['projection']='event_tail'
    except (sqlite3.Error,ValueError,TypeError):return messages,unknown('task_projection_unavailable')
    else:
        tasks=dict(state='partial' if len(heads)>LIMIT else 'known',items=[],limited=len(heads)>LIMIT,omitted=0,coverage='last 64 task heads, all seats',projection='task_heads')
        try:
            if not heads and conn.execute('SELECT 1 FROM office_task_events LIMIT 1').fetchone() is not None:
                return messages,unknown('task_projection_incomplete')
            ids=[h['event_id'] for h in heads[:LIMIT]]
            events={r['event_id']:r for r in conn.execute('SELECT '+columns+' FROM office_task_events WHERE event_id IN ('+','.join('?' for _ in ids)+')',ids)} if ids else {}
            rows=[]
            for h in heads[:LIMIT]:
                event=events.get(h['event_id'])
                if event is None and h['status']=='done':
                    rows.append(dict(h,seq=None,note=None,created_at=h['updated_at'],receipt_id=None,history_state='unavailable'))
                    tasks['history_omitted']=tasks.get('history_omitted',0)+1
                    continue
                if event is None or any(event[k]!=h[k] for k in ('task_id','actor_id','generation','status','title')) or event['created_at']!=h['updated_at']:
                    tasks['omitted']+=1;continue
                rows.append(event)
        except (sqlite3.Error,ValueError,TypeError):return messages,unknown('task_projection_unavailable')
    seen=set()
    for raw in rows:
        r=dict(raw)
        if (not _number(r['created_at']) or not 0<=r['created_at']<=now or not generation(r['actor_id'],r['generation'])
                or not bound(r['task_id'],256) or not r['task_id']):
            tasks['omitted']+=1;continue
        key=(r['task_id'],r['actor_id'],r['generation'])
        if key in seen:continue
        seen.add(key)
        if (r['status'] not in ('open','blocked','done') or not bound(r['event_id'],256) or not r['event_id']
                or (r.get('history_state')!='unavailable' and (not bound(r['receipt_id'],256) or not r['receipt_id'])) or not bound(r['title'],256)
                or not r['title'] or r['note'] is not None and not bound(r['note'],2048)):
            tasks['omitted']+=1;continue
        r['seq']=str(r['seq']) if r['seq'] is not None else None;tasks['items'].append(r)
    for part in (messages,tasks):
        if part['omitted'] or part.get('history_omitted'):part['state']='partial'
    return messages,tasks
