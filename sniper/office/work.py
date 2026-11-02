"""Actor-owned written work. Text is data; no execution or policy authority."""
import hashlib
import json

from . import store as S, runtime_observations as O

ACTIONS=frozenset({'msg.send','task.set','task.update'})


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS office_dm_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,'
        'message_id TEXT UNIQUE NOT NULL,from_actor_id TEXT NOT NULL,from_generation INTEGER NOT NULL,'
        'to_actor_id TEXT NOT NULL,to_generation INTEGER NOT NULL,created_at REAL NOT NULL,'
        'text TEXT NOT NULL,refs_json TEXT NOT NULL,receipt_id TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_inbox ON office_dm_events(to_actor_id,to_generation,created_at,seq)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_at ON office_dm_events(created_at,seq)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_pair ON office_dm_events(from_actor_id,to_actor_id,seq)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_task_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,'
        'event_id TEXT UNIQUE NOT NULL,task_id TEXT NOT NULL,actor_id TEXT NOT NULL,generation INTEGER NOT NULL,'
        'status TEXT NOT NULL,title TEXT NOT NULL,note TEXT NOT NULL,created_at REAL NOT NULL,receipt_id TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_tasks_owner ON office_task_events(actor_id,generation,task_id,seq)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_tasks_history ON office_task_events(task_id,seq)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_task_heads(task_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,'
        'generation INTEGER NOT NULL,status TEXT NOT NULL,event_id TEXT NOT NULL,title TEXT NOT NULL,updated_at REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_task_head_owner ON office_task_heads(actor_id,generation,status,task_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_task_head_cut_status ON office_task_heads(actor_id,generation,status,updated_at DESC,task_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_task_head_cut_future ON office_task_heads(actor_id,generation,updated_at DESC,task_id)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_dm_pending(message_id TEXT PRIMARY KEY,to_actor_id TEXT NOT NULL,to_generation INTEGER NOT NULL,created_at REAL NOT NULL,event_seq INTEGER NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_pending_inbox ON office_dm_pending(to_actor_id,to_generation,created_at,event_seq)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_dm_reads(message_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,'
        'generation INTEGER NOT NULL,turn_id TEXT NOT NULL,read_at REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_reads_turn ON office_dm_reads(turn_id)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_work_contexts(turn_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,'
        'generation INTEGER NOT NULL,created_at REAL NOT NULL,payload_json TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_task_at ON office_task_events(created_at,seq)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_dm_read_at ON office_dm_reads(read_at,message_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_work_context_at ON office_work_contexts(created_at,turn_id)')
    conn.execute('CREATE TABLE IF NOT EXISTS office_work_retention(table_name TEXT PRIMARY KEY,retained_from REAL,pruned_n INTEGER NOT NULL DEFAULT 0)')
    for table in ('office_dm_events','office_dm_reads','office_task_events','office_work_contexts'):
        conn.execute('INSERT OR IGNORE INTO office_work_retention(table_name) VALUES(?)',(table,))
    for table in ('office_dm_events','office_task_events','office_dm_reads','office_work_contexts'):
        conn.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_update BEFORE UPDATE ON '+table+
                     " BEGIN SELECT RAISE(ABORT,'insert_only'); END")


def _text(value,maximum,*,empty=False):
    try:
        valid=isinstance(value,str) and (empty or bool(value.strip())) and len(value.encode('utf-8'))<=maximum
    except UnicodeError:valid=False
    if not valid:raise ValueError('work_text_invalid')
    return value


def _actor(conn,aid,generation=None):
    row=conn.execute('SELECT payload_json FROM agents WHERE id=?',(aid,)).fetchone()
    doc=json.loads(row[0]) if row else {}
    if (doc.get('state')!='active' or type(doc.get('generation'))is not int
            or doc['generation']<1 or generation is not None and doc['generation']!=generation):
        raise ValueError('work_actor_unknown')
    return doc


def _key(value):
    return hashlib.sha256(S.canonical_json(value)).hexdigest()


def _prepare(conn,context,kind,args,event_id):
    refs=[]
    if kind=='msg.send':
        if set(args) not in ({'to_actor_id','text'},{'to_actor_id','text','refs'}):raise ValueError('work_args_invalid')
        target=_actor(conn,_text(args['to_actor_id'],256))
        text=_text(args['text'],4096)
        refs=O.validate_refs(conn,[] if args.get('refs') is None else args['refs'],before=context.cut['knowledge_cut'],supplied=visible_refs(conn,context))
        result={'state':'recorded','message_id':event_id,'authority':'data_only'}
    else:
        if kind=='task.set':
            if set(args)!={'title','note'}:raise ValueError('work_args_invalid')
            task_id=event_id;status='open';title=_text(args['title'],256)
        else:
            if set(args)!={'task_id','status','note'}:raise ValueError('work_args_invalid')
            task_id=_text(args['task_id'],64);status=args['status']
            old=conn.execute('SELECT *,updated_at AS created_at FROM office_task_heads WHERE task_id=?',(task_id,)).fetchone()
            if (old is None or old['actor_id']!=context.actor_id or old['generation']!=context.generation
                    or old['created_at']>context.cut['knowledge_cut']):raise ValueError('work_task_unknown')
            if status not in ('open','blocked','done'):raise ValueError('work_status_invalid')
            title=old['title']
        note=_text(args['note'],2048,empty=True)
        result={'state':'recorded','task_id':task_id,'event_id':event_id,'authority':'data_only'}
    return dict(refs=refs,result=result,**(dict(target=target,text=text) if kind=='msg.send' else
                dict(task_id=task_id,status=status,title=title,note=note)))


def record(conn,context,key,kind,args,*,now):
    if kind not in ACTIONS or not isinstance(args,dict) or not O._number(now) or now<context.cut['receipt_cut']:
        raise ValueError('work_request_invalid')
    with S.write_transaction(conn):
        _actor(conn,context.actor_id,context.generation)
        table='office_dm_events' if kind=='msg.send' else 'office_task_events'
        id_column='message_id' if kind=='msg.send' else 'event_id'
        event_id=_key([context.actor_id,context.generation,key,kind])
        request_hash=_key(args)
        prior=conn.execute('SELECT receipt_id FROM '+table+' WHERE '+id_column+'=?',(event_id,)).fetchone()
        if prior:
            receipt=O._receipt(conn,prior[0])
            if receipt['request_hash']!=request_hash:raise ValueError('work_identity_conflict')
            return dict(receipt['result'],receipt_id=prior[0])
        prepared=_prepare(conn,context,kind,args,event_id)
        refs,result=prepared['refs'],prepared['result']
        receipt={'kind':kind,'actor_id':context.actor_id,'generation':context.generation,'role':context.role,
                 'decision_id':context.decision_id,'epoch_id':getattr(context,'epoch_id',None),'known_at':now,'outcome':'success','refs':refs,
                 'request_hash':request_hash,'record_kind':table,'record_id':event_id,'result':result.copy()}
        receipt_id=S.append(conn,'receipts',receipt)
        result['receipt_id']=receipt_id
        if kind=='msg.send':
            conn.execute('INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (event_id,context.actor_id,context.generation,args['to_actor_id'],prepared['target']['generation'],now,prepared['text'],S.canonical_json(refs).decode(),receipt_id))
            conn.execute('INSERT INTO office_dm_pending VALUES(?,?,?,?,?)',(event_id,args['to_actor_id'],prepared['target']['generation'],now,conn.execute('SELECT last_insert_rowid()').fetchone()[0]))
        else:
            task_id,status,title,note=(prepared[k] for k in ('task_id','status','title','note'))
            conn.execute('INSERT INTO office_task_events(event_id,task_id,actor_id,generation,status,title,note,created_at,receipt_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (event_id,task_id,context.actor_id,context.generation,status,title,note,now,receipt_id))
            conn.execute('INSERT INTO office_task_heads VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,event_id=excluded.event_id,updated_at=excluded.updated_at',
                (task_id,context.actor_id,context.generation,status,event_id,title,now))
        row=dict(event_id=_key(['work',event_id]),at=now,from_actor_id=context.actor_id,
            to_actor_id=args['to_actor_id'] if kind=='msg.send' else context.actor_id,
            from_record_kind='receipts',from_record_id=receipt_id,to_record_kind=table,to_record_id=event_id,
            decision_id=context.decision_id,epoch_id=getattr(context,'epoch_id',None),order_id=None,kind=kind,public_summary='Recorded '+kind)
        O._insert(conn,'handoff_events',row,key='event_id',identity=tuple(row))
        return result


def _tasks_at_cut(conn,context):
    """Bound candidates before history reads; later terminal state cannot erase earlier work."""
    owner=(context.actor_id,context.generation)
    cut=context.cut['knowledge_cut'];tasks=[];truncated=False
    for status in ('open','blocked'):
        heads=conn.execute('SELECT task_id,event_id FROM office_task_heads INDEXED BY office_task_head_cut_status '
            'WHERE actor_id=? AND generation=? AND status=? AND updated_at<=? ORDER BY updated_at DESC,task_id LIMIT 17',
            (*owner,status,cut)).fetchall()
        for head in heads:
            row=conn.execute('SELECT * FROM office_task_events WHERE event_id=?',(head['event_id'],)).fetchone()
            if row is None:truncated=True
            else:tasks.append(row)
    heads=conn.execute('SELECT task_id FROM office_task_heads INDEXED BY office_task_head_cut_future '
        'WHERE actor_id=? AND generation=? AND updated_at>? ORDER BY updated_at DESC,task_id LIMIT 17',(*owner,cut)).fetchall()
    truncated|=len(heads)>16
    for head in heads[:16]:
        # shortcut: at most 64 events per future-updated head; a deeper frozen cut is labelled truncated.
        history=conn.execute('SELECT seq,created_at FROM office_task_events INDEXED BY office_tasks_history '
            'WHERE task_id=? ORDER BY seq DESC LIMIT 65',(head['task_id'],)).fetchall()
        found=next((r for r in history[:64] if r['created_at']<=cut),None)
        if found is None:
            retention=conn.execute("SELECT pruned_n FROM office_work_retention WHERE table_name='office_task_events'").fetchone()
            truncated|=len(history)>64 or retention is None or retention['pruned_n']>0
            continue
        row=conn.execute('SELECT * FROM office_task_events WHERE seq=?',(found['seq'],)).fetchone()
        if row['status'] in ('open','blocked'):tasks.append(row)
    return sorted(tasks,key=lambda r:r['task_id']),truncated


def observe(conn,context,*,now,supplied_refs=()):
    """Persist a bounded context before delivery; same-turn retries reuse identical data."""
    if not O._number(now) or now<context.cut['receipt_cut']:raise ValueError('work_clock_unknown')
    with S.write_transaction(conn):
        _actor(conn,context.actor_id,context.generation)
        prior=conn.execute('SELECT * FROM office_work_contexts WHERE turn_id=?',(context.turn_id,)).fetchone()
        if prior:
            if (prior['actor_id'],prior['generation'])!=(context.actor_id,context.generation):raise ValueError('work_context_identity')
            return json.loads(prior['payload_json'])
        rows=conn.execute('SELECT d.* FROM office_dm_pending p JOIN office_dm_events d ON d.message_id=p.message_id '
            'WHERE p.to_actor_id=? AND p.to_generation=? AND p.created_at<=? ORDER BY p.created_at,p.event_seq LIMIT 1001',
            (context.actor_id,context.generation,context.cut['knowledge_cut'])).fetchall()
        # shortcut: backlog above 1000 is a labelled lower bound; add mailbox counters if reached.
        backlog=len(rows);messages=[];budget=24576
        for row in rows[:8]:
            doc=dict(row)
            if len(S.canonical_json(doc))>budget:break
            messages.append(doc);budget-=len(S.canonical_json(doc))
        tasks=[]
        candidates,tasks_truncated=_tasks_at_cut(conn,context)
        for row in candidates[:16]:
            doc=dict(row)
            if len(S.canonical_json(doc))>budget:break
            tasks.append(doc);budget-=len(S.canonical_json(doc))
        refs=O.validate_refs(conn,list(supplied_refs),before=context.cut['knowledge_cut'])
        for message in messages:
            for ref in json.loads(message['refs_json']):
                if ref not in refs:refs.append(ref)
        refs=O.validate_refs(conn,refs[:32],before=context.cut['knowledge_cut'])
        result=dict(supplied_refs=refs,dms=messages,tasks=tasks,dm_context=bool(messages),task_context=bool(tasks),
            dm_count=len(messages),unread_backlog=min(backlog,1000),unread_backlog_lower_bound=backlog>1000,
            tasks_truncated=tasks_truncated or len(tasks)<len(candidates),injection_state='not_evaluated')
        conn.execute('INSERT INTO office_work_contexts VALUES(?,?,?,?,?)',
            (context.turn_id,context.actor_id,context.generation,now,S.canonical_json(result).decode()))
        return result


def visible_refs(conn,context):
    row=conn.execute('SELECT actor_id,generation,payload_json FROM office_work_contexts WHERE turn_id=?',(context.turn_id,)).fetchone()
    if row is None:return []
    if tuple(row[:2])!=(context.actor_id,context.generation):raise ValueError('work_context_identity')
    return json.loads(row['payload_json'])['supplied_refs']


def prune(conn,*,now,limit=200):
    if not O._number(now) or type(limit)is not int or not 1<=limit<=200:raise ValueError('work_retention_invalid')
    removed={table:0 for table in ('office_dm_events','office_dm_reads','office_task_events','office_work_contexts')}
    with S.write_transaction(conn):
        cutoff=O.retention_cutoff(conn,now)
        if cutoff is None:return removed
        conditions={
            'office_dm_events':"created_at<? AND EXISTS(SELECT 1 FROM office_dm_reads r WHERE r.message_id=office_dm_events.message_id)",
            'office_dm_reads':"read_at<? AND NOT EXISTS(SELECT 1 FROM office_dm_events d WHERE d.message_id=office_dm_reads.message_id)",
            'office_task_events':"created_at<? AND NOT EXISTS(SELECT 1 FROM office_task_heads h WHERE h.event_id=office_task_events.event_id AND h.status IN ('open','blocked'))",
            'office_work_contexts':"created_at<?",
        }
        for table,condition in conditions.items():
            cur=conn.execute('DELETE FROM '+table+' WHERE rowid IN(SELECT rowid FROM '+table+' WHERE '+condition+' LIMIT ?)',(cutoff,limit))
            removed[table]=cur.rowcount
            if cur.rowcount:
                at='read_at' if table=='office_dm_reads' else 'created_at'
                first=conn.execute('SELECT min('+at+') FROM '+table).fetchone()[0]
                conn.execute('UPDATE office_work_retention SET retained_from=?,pruned_n=pruned_n+? WHERE table_name=?',(first if first is not None else cutoff,cur.rowcount,table))
    return removed


def validate(conn,context,kind,args):
    if kind not in ACTIONS or not isinstance(args,dict):raise ValueError('work_request_invalid')
    _actor(conn,context.actor_id,context.generation)
    _prepare(conn,context,kind,args,'validation-only')
    return True


def lookup(conn,context,key,kind,args):
    table,column=('office_dm_events','message_id') if kind=='msg.send' else ('office_task_events','event_id')
    event_id=_key([context.actor_id,context.generation,key,kind])
    row=conn.execute('SELECT receipt_id FROM '+table+' WHERE '+column+'=?',(event_id,)).fetchone()
    if row is None:return None
    receipt=O._receipt(conn,row[0])
    if receipt['request_hash']!=_key(args):raise ValueError('work_identity_conflict')
    return dict(receipt['result'],receipt_id=row[0])


def acknowledge(conn,context,*,now):
    """A successful structured response proves input delivery, never cognition or agreement."""
    if not O._number(now) or now<context.cut['receipt_cut']:raise ValueError('work_clock_unknown')
    with S.write_transaction(conn):
        _actor(conn,context.actor_id,context.generation)
        row=conn.execute('SELECT actor_id,generation,payload_json FROM office_work_contexts WHERE turn_id=?',(context.turn_id,)).fetchone()
        if row is None or tuple(row[:2])!=(context.actor_id,context.generation):raise ValueError('work_context_unknown')
        messages=json.loads(row['payload_json'])['dms']
        for msg in messages:
            conn.execute('DELETE FROM office_dm_pending WHERE message_id=?',(msg['message_id'],))
            conn.execute('INSERT OR IGNORE INTO office_dm_reads VALUES(?,?,?,?,?)',
                (msg['message_id'],context.actor_id,context.generation,context.turn_id,now))
