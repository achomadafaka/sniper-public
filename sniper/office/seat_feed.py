"""Bounded operator-only projection of host-published Office v2 work."""
import json
import sqlite3
from pathlib import Path
from sniper.office.report import _number, _ro
from sniper.office.seat_events import KINDS, STATES, is_host_sender


def _part(reason=None):
    return dict(state='unknown' if reason else 'known',items=[],limited=False,omitted=0,cursor=None,reason=reason)


def _text(value,maximum=4096,*,empty=False):
    return isinstance(value,str) and (empty or bool(value)) and len(value.encode('utf-8'))<=maximum


def _cursor(value):
    if value is None:return None
    if not isinstance(value,str) or not value.isascii() or not value.isdecimal() or len(value)>19 or int(value)>9223372036854775807:
        raise ValueError('cursor_invalid')
    return int(value)


def _notify_marker(raw):
    try:
        if not _text(raw,4096):return None
        refs=json.loads(raw)
        if not isinstance(refs,list) or len(refs)>64 or any(not _text(v,256) for v in refs):return None
        return 'notify_operator' in refs
    except (ValueError,TypeError,RecursionError,UnicodeError):return None


def _message_models(root,result,now):
    """Only requested model from a recorded wake covering the message, never current configuration."""
    windows=[]
    current={r['actor_id']:r for r in result['roster']['items']}
    try:
        with _ro(Path(root)/'v2/quota.db') as conn:
            # Per seat: a busy seat's wakes must not push a quiet seat's older messages out of the window.
            rows=[r for actor in current for r in conn.execute('SELECT actor_id,payload_json FROM quota_wakes WHERE actor_id=? AND ended_at<=? ORDER BY ended_at DESC LIMIT 64',(actor,now))]
            for row in rows:
                raw=row['payload_json']
                if not _text(raw,16384):continue
                try:d=json.loads(raw)
                except (ValueError,TypeError,RecursionError):continue
                seat=current.get(row['actor_id'])
                if (not isinstance(d,dict) or seat is None or d.get('actor_id')!=seat['actor_id']
                    or d.get('epoch_id')!=seat['epoch_id'] or d.get('generation')!=seat['generation']
                    or str(d.get('incarnation'))!=str(seat['incarnation']) or not _text(d.get('model'),128)
                    or not all(_number(d.get(k)) for k in ('started_at','ended_at'))
                    or not 0<=d['started_at']<=d['ended_at']<=now):continue
                windows.append(d)
    except (OSError,sqlite3.Error,ValueError,TypeError):pass
    for part in ('events','mail','questions','seat_messages'):
        values=[m for seat in result['seats']['items'] for m in seat.get('messages',[])] if part=='seat_messages' else result[part]['items']
        for item in values:
            actor=item.get('from_actor_id') if part=='mail' else item.get('actor_id')
            at=item.get('created_at') if part=='mail' else item.get('corrected_at')
            matches={d['model'] for d in windows if d['actor_id']==actor and _number(at) and d['started_at']<=at<=d['ended_at']}
            item['model']=next(iter(matches)) if len(matches)==1 else None
            item['model_basis']='recorded_wake_requested' if item['model'] else 'unknown'


def read_feed(root,*,now,after=None,mail_after=None,actor=None,limit=64):
    after,mail_after=_cursor(after),_cursor(mail_after)
    if type(limit) is not int or not 1<=limit<=200:raise ValueError('limit_invalid')
    if actor is not None and not _text(actor,256):raise ValueError('actor_invalid')
    result=dict(state='unknown',as_of=now if _number(now) else None,roster=_part('source_unavailable'),events=_part('source_unavailable'),mail=_part('source_unavailable'),tasks=_part('source_unavailable'),seats=_part('source_unavailable'),questions=_part('source_unavailable'))
    if not _number(now) or not 0<=now<=253402300799:return result
    try:
        with _ro(Path(root)/'office.db') as conn:
            roster=_part();raw=conn.execute('SELECT * FROM office_v2_roster ORDER BY actor_id LIMIT 65').fetchall()
            roster['limited']=len(raw)>64
            for row in raw[:64]:
                r=dict(row)
                if r['sealed']==1:continue
                if (r['sealed']!=0 or not _number(r['updated_at']) or not 0<=r['updated_at']<=now
                    or not all(_text(r[k],256) for k in ('actor_id','epoch_id','role'))
                    or any(type(r[k]) is not int or r[k]<0 for k in ('generation','incarnation'))):
                    roster['omitted']+=1;continue
                roster['items'].append(r)
            if roster['limited'] or roster['omitted']:roster['state']='partial'
            # A sealed seat is withheld, but does not make the remaining roster unknown.
            result['roster']=roster
            current={r['actor_id']:r for r in roster['items']}
            def tail(table,cursor,*,filter_actor=False):
                part=_part();where=[];args=[]
                if cursor is not None:where.append('seq>?');args.append(cursor)
                if filter_actor and actor is not None:where.append('actor_id=?');args.append(actor)
                sql='SELECT * FROM '+table+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY seq '+('ASC' if cursor is not None else 'DESC')+' LIMIT ?'
                values=conn.execute(sql,(*args,limit+1)).fetchall()
                part['limited']=len(values)>limit
                values=values[:limit]
                part['cursor']=str(max(r['seq'] for r in values)) if values else str(cursor) if cursor is not None else None
                return values,part
            values,part=tail('office_v2_events',after,filter_actor=True)
            workspace_heads={}
            for row in values:
                r=dict(row);seat=current.get(r['actor_id'])
                if seat is not None and r['kind']=='workspace' and all(r[k]==seat[k] for k in ('epoch_id','generation','incarnation')):
                    workspace_heads[r['actor_id']]=max(workspace_heads.get(r['actor_id'],0),r['seq'])
                if (seat is None or any(r[k]!=seat[k] for k in ('epoch_id','generation','incarnation'))
                    or r['corrected_at'] is not None and (not _number(r['corrected_at']) or not 0<=r['corrected_at']<=now)
                    or r['kind'] not in KINDS or r['state'] not in STATES or not _text(r['event_id'],256)
                    or r['text'] is not None and not _text(r['text'],empty=True)):
                    part['omitted']+=1;continue
                try:
                    if r['refs_json'] is not None and not _text(r['refs_json'],empty=True):raise ValueError()
                    refs=json.loads(r['refs_json'] or '{}')
                    if not isinstance(refs,dict):raise ValueError()
                    if r['kind']=='browser_visit':refs={k:refs.get(k) for k in ('host','path')}
                    elif r['kind']=='workspace':
                        files=refs.get('files',[])
                        if not isinstance(files,list):raise ValueError()
                        refs={k:refs.get(k) for k in ('notebook_text','state','truncated','tasks')}
                        refs['files']=[dict(path=f.get('path'),size_bytes=f.get('size_bytes')) for f in files[:64] if isinstance(f,dict)]
                    elif r['kind']=='lifecycle':
                        refs={k:refs.get(k) for k in ('reason','until_s','type','outcome') if isinstance(refs.get(k),(str,int,float))}
                    elif r['kind']=='tool_result':
                        refs=dict(tool=refs.get('tool') if _text(refs.get('tool'),256) else None,
                                  is_error=refs.get('is_error') if type(refs.get('is_error')) is bool else None)
                    else:refs={}
                except (ValueError,TypeError,RecursionError):part['omitted']+=1;continue
                r.pop('refs_json');r['refs']=refs;r['seq']=str(r['seq'])
                if r['corrected_at'] is None or r['kind']=='thought' and r['text'] is None:r['state']='unknown'
                part['items'].append(r)
            kept=[r for r in part['items'] if r['kind']!='workspace' or int(r['seq'])==workspace_heads.get(r['actor_id'])]
            part['omitted']+=len(part['items'])-len(kept);part['items']=kept
            if part['limited'] or part['omitted']:part['state']='partial'
            result['events']=part
            try:
                values,part=tail('office_dm_events',mail_after)
                for row in values:
                    r=dict(row);sender=current.get(r['from_actor_id']);target=sender if r['to_actor_id']=='operator' else current.get(r['to_actor_id'])   # a reply to the operator binds to the replying seat
                    if (target is None or r['to_generation']!=target['generation']
                        or not is_host_sender(r['from_actor_id']) and (sender is None or r['from_generation']!=sender['generation'])
                        or not _number(r['created_at']) or not 0<=r['created_at']<=now
                        or not _text(r['text']) or not _text(r['message_id'],256)):
                        part['omitted']+=1;continue
                    try:
                        binding=conn.execute('SELECT epoch_id,from_incarnation,to_incarnation FROM office_v2_mail_bind WHERE message_id=?',(r['message_id'],)).fetchone()
                        refs=dict(binding) if binding is not None else {}
                        bound=(isinstance(refs,dict) and refs.get('epoch_id')==target['epoch_id']
                               and refs.get('to_incarnation')==target['incarnation']
                               and (is_host_sender(r['from_actor_id']) or
                                    sender['epoch_id']==target['epoch_id'] and refs.get('from_incarnation')==sender['incarnation']))
                    except (sqlite3.Error,ValueError,TypeError,RecursionError):bound=False
                    if not bound:part['omitted']+=1;continue
                    if actor is not None and actor not in (r['from_actor_id'],r['to_actor_id']):continue
                    r['notify_operator']=_notify_marker(r.pop('refs_json',None));r['seq']=str(r['seq']);part['items'].append(r)
                if part['limited'] or part['omitted']:part['state']='partial'
                part['coverage']='last bounded mail events, all seats; epoch/generation/incarnation-bound'
                result['mail']=part
            except (sqlite3.Error,ValueError):pass
            tasks=_part();published=set()
            for event in sorted(result['events']['items'],key=lambda r:int(r['seq']),reverse=True):
                if event['kind']!='workspace' or event['actor_id'] in published:continue
                published.add(event['actor_id']);manifest=event['refs'];values=manifest.get('tasks')
                if event['state']=='unknown' or not isinstance(values,list) or manifest.get('state') not in ('known','partial'):
                    tasks['state']='unknown';continue
                if event['state']=='partial' or manifest.get('truncated') or manifest.get('state')=='partial' or len(values)>64:tasks['limited']=True
                for t in values[:64]:
                    if (not isinstance(t,dict) or t.get('status') not in ('open','blocked','done')
                        or not _text(t.get('task_id'),256) or not _text(t.get('title'),256)
                        or t.get('note') is not None and not _text(t['note'],2048,empty=True)):
                        tasks['omitted']+=1;continue
                    tasks['items'].append(dict(actor_id=event['actor_id'],**{k:t.get(k) for k in ('task_id','status','title','note')}))
            expected={actor} if actor in current else set(current) if actor is None else set()
            if expected-published:tasks['state']='unknown'
            if tasks['limited'] or tasks['omitted']:tasks['state']='partial'
            result['tasks']=tasks
    except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError,KeyError):pass
    if result['roster']['state']=='known':
        try:
            from .floor_view import seats,questions
            # Busy timeline failure must not erase the independent current-seat read.
            with _ro(Path(root)/'office.db') as floor_conn:
                result['questions']=questions(floor_conn,result['roster']['items'],now)
                result['seats']=seats(floor_conn,root,result['roster']['items'],now)
            _message_models(root,result,now)
        except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError,KeyError):pass
    parts=[result[k]['state'] for k in ('roster','events','mail','tasks')]
    result['state']='known' if all(s=='known' for s in parts) else 'unknown' if all(s=='unknown' for s in parts) else 'partial'
    return result
