"""Small operator floor summaries from current-identity host observations; no execution capability."""
import json
import os
from pathlib import Path
import sqlite3
import time
from .report import _number,_ro

ROLES={'head':'Head','judge':'Judge','judge_gpt':'Judge GPT','scout':'Scout','researcher':'Researcher','forensics':'Forensics','narrative':'Narrative'}


def _host_state(root,seat,now):
    if seat['epoch_id']!='office-v2-explore-1' or seat['role'].lower() not in ROLES:return {}
    path=Path(root)/'v2'/('explore-'+('judge-gpt' if seat['role'].lower()=='judge_gpt' else seat['role'].lower())+'.json')
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if info.st_size>16384 or info.st_mtime>time.time()+1:return {}
            value=json.loads(os.read(fd,16385))
        finally:os.close(fd)
        return value if isinstance(value,dict) and value.get('incarnation')==seat['incarnation'] else {}
    except (OSError,ValueError,TypeError):return {}


def _metrics(root,seat,now):
    try:
        with _ro(Path(root)/'v2/quota.db') as conn:
            row=conn.execute("""SELECT count(*),sum(CASE WHEN state='known' AND typeof(used)='integer' AND used>=0 THEN used ELSE 0 END),sum(CASE WHEN state!='known' OR used IS NULL OR typeof(used)!='integer' OR used<0
              THEN 1 ELSE 0 END),sum(CASE WHEN json_extract(payload_json,'$.epoch_id') IS NOT ? THEN 1 ELSE 0 END)
              FROM quota_wakes WHERE actor_id=? AND ended_at>=? AND ended_at<=?""",(seat['epoch_id'],seat['actor_id'],now//86400*86400,now)).fetchone()
            return dict(wakes_today=row[0] if not row[3] else None,tokens_today=(row[1] or 0) if not row[2] and not row[3] else None,
                        tokens_known=(row[1] or 0) if not row[3] else None,tokens_unknown_wakes=(row[2] or 0) if not row[3] else None)
    except (OSError,sqlite3.Error,ValueError):return dict(wakes_today=None,tokens_today=None,tokens_known=None,tokens_unknown_wakes=None)


def seats(conn,root,roster,now):
    out=[]
    for seat in roster:
        rows=conn.execute('SELECT event_id,kind,text,state,corrected_at,refs_json,truncated FROM office_v2_events WHERE actor_id=? AND epoch_id=? AND generation=? AND incarnation=? ORDER BY seq DESC LIMIT 64',
                          tuple(seat[k] for k in ('actor_id','epoch_id','generation','incarnation'))).fetchall()
        life=next((r for r in rows if r['kind']=='lifecycle'),None)
        last=rows[0] if rows else None
        last_at=last['corrected_at'] if last and _number(last['corrected_at']) and 0<=last['corrected_at']<=now else None
        rows=[r for r in rows if _number(r['corrected_at']) and 0<=r['corrected_at']<=now]
        thought=next((r['text'] for r in rows if r['kind']=='thought' and isinstance(r['text'],str) and r['text'].strip() and len(r['text'].encode('utf-8'))<=4096),None)
        host=_host_state(root,seat,now)
        activity=dict(kind='unknown',until=None,reason=None,at=None)
        if life and life['state']=='known' and _number(life['corrected_at']) and 0<=life['corrected_at']<=now:
            at=life['corrected_at'];activity['at']=at
            try:refs=json.loads(life['refs_json'] or '{}')
            except (ValueError,TypeError):refs={}
            if not isinstance(refs,dict):refs={}
            reason=refs.get('reason') if isinstance(refs.get('reason'),str) else host.get('parked_reason')
            if life['text']=='parked' and isinstance(reason,str):activity.update(kind='parked',reason=reason[:256])
            elif life['text']=='wake_start' and now-at<=660:activity['kind']='working'
            elif life['text'] in ('wake_end','sleep','unparked'):
                until=refs.get('sleep_until') if life['text']=='unparked' and _number(refs.get('sleep_until')) else host.get('sleep_until')
                if _number(until) and now<until<=253402300799:activity.update(kind='sleeping',until=until)
                elif life['text']=='unparked':activity['kind']='idle'
        tool=next((r['text'] for r in rows if r['kind']=='tool_call' and isinstance(r['text'],str) and _number(activity['at']) and r['corrected_at']>=activity['at']),None)
        tool_event=next((r for r in rows if r['kind'] in ('tool_call','tool_result')),None)
        try:tool_refs=json.loads(tool_event['refs_json'] or '{}') if tool_event else {}
        except (ValueError,TypeError):tool_refs={}
        last_tool=(tool_refs.get('tool') if isinstance(tool_refs,dict) else None) or (tool_event['text'] if tool_event and tool_event['kind']=='tool_call' else None)
        out.append(dict(actor_id=seat['actor_id'],role=seat['role'],name=ROLES.get(seat['role'].lower(),'Seat'),activity=activity,
                        current_tool=tool[:128] if tool and activity['kind']=='working' else None,last_thought=thought,
                        last_event_id=last['event_id'] if last else None,last_event_at=last_at,last_event_kind=last['kind'] if last else None,
                        last_event_age_s=now-last_at if last_at is not None else None,last_tool=last_tool[:128] if isinstance(last_tool,str) else None,
                        tool_event_at=tool_event['corrected_at'] if tool_event else None,tool_event_state=('called' if tool_event['kind']=='tool_call' else 'returned') if tool_event else None,
                        messages=[dict(event_id=r['event_id'],actor_id=seat['actor_id'],kind='thought',text=r['text'],corrected_at=r['corrected_at'],truncated=r['truncated']) for r in rows if r['kind']=='thought' and isinstance(r['text'],str) and len(r['text'].encode('utf-8'))<=4096][:16]))
    # Optional quota reads must not consume the office connection's SQL deadline.
    for item,seat in zip(out,roster):item.update(_metrics(root,seat,now))
    return dict(state='known',items=out,coverage='Current identity; last 64 events per seat. State is last host observation, not a process probe. Wakes are completed today UTC; counted tokens exclude cache reads.')


def questions(conn,roster,now):
    """The latest recorded requests per current seat, independent of the busy aggregate timeline."""
    items=[];limited=False;omitted=0
    try:
        for seat in roster:
            values=conn.execute("SELECT event_id,actor_id,state,text,corrected_at FROM office_v2_events WHERE actor_id=? AND epoch_id=? AND generation=? AND incarnation=? AND kind='request' ORDER BY seq DESC LIMIT 11",
                                tuple(seat[k] for k in ('actor_id','epoch_id','generation','incarnation'))).fetchall()
            limited=limited or len(values)>10
            for row in values[:10]:
                if (not _number(row['corrected_at']) or not 0<=row['corrected_at']<=now or not isinstance(row['text'],str)
                    or not row['text'].strip() or len(row['text'].encode('utf-8'))>4096):omitted+=1;continue
                items.append(dict(row))
        return dict(state='partial' if limited or omitted else 'known',items=sorted(items,key=lambda r:r['corrected_at'],reverse=True),limited=limited,omitted=omitted,
                    coverage='Latest 10 requests per current seat; replies do not automatically resolve a request.')
    except (sqlite3.Error,ValueError,TypeError):return dict(state='partial' if items else 'unknown',items=items,limited=True,reason='request_read_unknown')
