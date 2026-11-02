"""Bounded public typed observations; no inference from legacy timestamps or prose."""
import sqlite3

LIMIT=64
CALL_FIELDS=('call_id','actor_id','generation','turn_id','decision_id','epoch_id','requested_provider','requested_model','served_provider','served_model','served_model_basis','started_at','ended_at','latency_s','outcome','reason','input_tokens','output_tokens','cached_tokens','cache_creation_tokens','cost_usd','usage_state','cost_basis','invalid_metadata_n')
HANDOFF_FIELDS=('event_id','at','from_actor_id','to_actor_id','from_record_kind','from_record_id','to_record_kind','to_record_id','decision_id','order_id','kind','public_summary','epoch_id')


def read_runtime(conn,agents,*,now,public_epochs,public_scope):
    from sniper.office.report import _number
    sealed={a['agent_id'] for a in agents if a.get('desk_screen_state')=='sealed'}
    def read(table,fields,stamp):
        out=dict(state='unknown',items=[],limited=False,omitted=0,coverage='last 64 typed rows, all seats',retained_from=None,pruned_n=None,dropped_n=None)
        if now is None:return dict(out,reason='clock_unknown')
        try:
            rows=conn.execute('SELECT '+','.join(fields)+' FROM '+table+' ORDER BY seq DESC LIMIT ?', (LIMIT+1,)).fetchall()
            out.update(state='known',limited=len(rows)>LIMIT)
            coverage=conn.execute('SELECT retained_from,pruned_n,dropped_n FROM runtime_observation_state WHERE table_name=?',(table,)).fetchone()
            if coverage:
                for k in ('retained_from','pruned_n','dropped_n'):
                    v=coverage[k];out[k]=v if _number(v) and v>=0 else None
            for raw in rows[:LIMIT]:
                r=dict(raw);epoch=r['epoch_id']
                actors=(r['actor_id'],) if table=='model_calls' else (r['from_actor_id'],r['to_actor_id'])
                if (not _number(r[stamp]) or not 0<=r[stamp]<=now or any(a in sealed for a in actors)
                        or not (epoch in public_epochs if epoch is not None else public_scope)
                        or any(isinstance(v,str) and len(v.encode('utf-8'))>4096 for v in r.values())):
                    out['omitted']+=1;continue
                if table=='model_calls':
                    if type(r['generation']) is not int or r['generation']<0:
                        out['omitted']+=1;continue
                    for k in ('started_at','ended_at','latency_s','input_tokens','output_tokens','cached_tokens','cache_creation_tokens','cost_usd','invalid_metadata_n'):
                        if not _number(r[k]) or r[k]<0:r[k]=None
                    if r['served_model_basis'] not in ('echoed','requested_pinned'):r['served_model_basis']='unknown'
                out['items'].append(r)
            if out['omitted'] or out['limited']:out['state']='partial'
            return out
        except (sqlite3.Error,ValueError,TypeError):return dict(out,state='unknown',items=[],reason='source_unavailable')
    return read('model_calls',CALL_FIELDS,'ended_at'),read('handoff_events',HANDOFF_FIELDS,'at')
