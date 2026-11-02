"""Read-only request accounting; stored turn snapshots and indexed broker receipts have separate denominators."""
import json
import sqlite3

TOOLS=('desk.near_pass_list','desk.near_pass_sheet')
LIMIT=32


def read_requests(conn,turns,agents,*,now,public_epochs,public_scope):
    from .report import _number, _verify_selected_row, IntegrityRefused
    from .runtime_observations import _public
    def empty(coverage):return dict(state='unknown',items=[],limited=False,omitted=0,coverage=coverage,observed_at=now)
    admission=empty('latest 200 distinct stored turn snapshots; cumulative once per turn, not step events or all-time totals; known and unknown accounting separate')
    receipts=empty('latest 32 stored receipts per near-pass tool; request counts and list-item counts are separate')
    if now is None or not(public_scope or public_epochs):return admission,receipts
    sealed={a['agent_id'] for a in agents if a.get('desk_screen_state')=='sealed'}
    def visible(r):return (_public(r) and r.get('actor_id') not in sealed
                          and (r.get('epoch_id') in public_epochs if r.get('epoch_id') is not None else public_scope))
    def identity(r):return isinstance(r.get('actor_id'),str) and bool(r['actor_id']) and type(r.get('generation')) is int and r['generation']>=0
    def count(v):return type(v) is int and v>=0
    if turns['state'] in ('known','partial'):
        admission.update(state='known',limited=bool(turns.get('limited')),omitted=turns.get('omitted',0))
        groups={}
        for r in turns['items']:
            if (not visible(r) or not identity(r) or not _number(r.get('created_at')) or not 0<=r['created_at']<=now
                    or r.get('ended_at') is not None and (not _number(r['ended_at']) or not r['created_at']<=r['ended_at']<=now)):
                admission['omitted']+=1;continue
            value=r.get('value') if isinstance(r.get('value'),dict) else {}
            pending=r.get('pending_step');pending=pending.get('value') if isinstance(pending,dict) else None
            if isinstance(pending,dict) and 'request_admission' in pending:value=pending
            reason=value.get('reason') or r.get('reason')
            reason=reason[:128] if isinstance(reason,str) and reason else None
            a=value.get('request_admission')
            known=(isinstance(a,dict) and a.get('state')=='known' and all(count(a.get(k)) for k in ('requested_n','refused_n','truncated_n'))
                   and a['refused_n']<=a['requested_n'] and type(a.get('truncated')) is bool and a['truncated']==(a['truncated_n']>0))
            if known and a['refused_n'] and not(isinstance(value.get('reason'),str) and value['reason']):reason=None
            key=(r['actor_id'],r['generation'],r.get('epoch_id'),reason,'known' if known else 'unknown')
            g=groups.setdefault(key,dict(actor_id=key[0],generation=key[1],epoch_id=key[2],reason=reason,accounting_state=key[4],turns_n=0,
                requested_n=0,refused_n=0,truncated_n=0,truncated=False,unknown_n=0,turn_ids=[]))
            g['turns_n']+=1;g['turn_ids'].append(r['row_id'])
            if not known:g['unknown_n']+=1;continue
            for k in ('requested_n','refused_n','truncated_n'):g[k]+=a[k]
            g['truncated']|=a['truncated']
        for g in groups.values():
            if g['unknown_n']:
                for k in ('requested_n','refused_n','truncated_n','truncated'):g[k]=None
        admission['items']=list(groups.values())
        if admission['limited'] or admission['omitted'] or any(g['unknown_n'] for g in groups.values()):admission['state']='partial'
    try:
        groups={};receipts.update(state='known',source_heads={})
        for tool in TOOLS:
            rows=conn.execute("SELECT seq,substr(payload_json,1,32769),prev_hash,row_hash FROM receipts INDEXED BY receipts_near_pass_tool "
                "WHERE json_extract(payload_json,'$.tool') IN ('desk.near_pass_list','desk.near_pass_sheet') "
                "AND json_extract(payload_json,'$.tool')=? ORDER BY seq DESC LIMIT ?",(tool,LIMIT+1)).fetchall()
            receipts['limited']|=len(rows)>LIMIT
            receipts['source_heads'][tool]=rows[0][0] if rows else None
            for seq,raw,prev,ref in rows[:LIMIT]:
                if len(raw)>32768:raise ValueError('receipt_oversized')
                _verify_selected_row(conn,'receipts',seq,raw,prev,ref)
                r=json.loads(raw);cut=r.get('cut');at=cut.get('receipt_cut') if isinstance(cut,dict) else None
                if not visible(r) or not identity(r) or not _number(at) or not 0<=at<=now:
                    receipts['omitted']+=1;continue
                result=r.get('result') if isinstance(r.get('result'),dict) else {}
                outcome=r.get('outcome') if isinstance(r.get('outcome'),str) else None
                reason=result.get('reason');reason=reason[:128] if isinstance(reason,str) and reason else None
                key=(r['actor_id'],r['generation'],r.get('epoch_id'),tool,outcome,reason)
                g=groups.setdefault(key,dict(actor_id=key[0],generation=key[1],epoch_id=key[2],tool=tool,outcome=outcome,reason=reason,
                    requests_n=0,list_items_n=0 if tool==TOOLS[0] else None,unknown_n=0,result_limited=False if tool==TOOLS[0] else None,receipt_ids=[],latest_receipt_cut=at))
                g['requests_n']+=1;g['receipt_ids'].append(ref);g['latest_receipt_cut']=max(g['latest_receipt_cut'],at)
                if tool==TOOLS[0]:
                    units=r.get('units');n=units.get('items') if isinstance(units,dict) else None
                    if outcome=='success' and result.get('state')=='known' and count(n) and type(result.get('limited')) is bool:
                        g['list_items_n']+=n;g['result_limited']|=result['limited']
                    else:g['unknown_n']+=1
        for g in groups.values():
            if g['unknown_n']:g['list_items_n']=None;g['result_limited']=None
        receipts['items']=list(groups.values())
        if receipts['limited'] or receipts['omitted'] or any(g['unknown_n'] for g in groups.values()):receipts['state']='partial'
    except (sqlite3.Error,ValueError,TypeError,RecursionError,IntegrityRefused):
        receipts.update(state='unknown',items=[],reason='receipt_source_unavailable_or_unverified')
    return admission,receipts
