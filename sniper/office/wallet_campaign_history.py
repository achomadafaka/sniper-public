"""Descriptive repeat sequences from immutable prior revisions, never operator identity or an edge score."""
import hashlib
import json
import math
import sqlite3
import time


def _finite(v):return type(v) in (int,float) and math.isfinite(v) and v>0


def _operations(doc,cutoff):
    boundary=doc.get('service_boundary') or {}
    if boundary.get('state')=='unknown' or boundary.get('control_link_eligible') is False or doc.get('family_link_eligible') is False:return []
    blocked={r.get('mint') for r in doc.get('roles',[]) if r.get('role')=='developer' and (r.get('service_boundary') or {}).get('control_link_eligible') is False}
    out=[]
    for launch in doc.get('launches',[])[:100]:
        mint=launch.get('mint');coverage=launch.get('coverage') or {}
        if coverage.get('creation_verified') is not True or mint in blocked:continue
        buys={}
        for b in doc.get('buys',[])[:100]:
            if b.get('mint')!=mint or not _finite(b.get('received_at')) or b['received_at']>cutoff:continue
            key=b.get('signature')
            if not isinstance(key,str) or not key:continue
            item=buys.setdefault(key,dict(kind='buy_tx',signature=key,event_at=b['received_at'],actors=set(),same_transaction_as_create=key==launch.get('source_signature')))
            item['actors'].add(b['actor']);item['event_at']=min(item['event_at'],b['received_at'])
        participants={a for b in buys.values() for a in b['actors']}|{r['address'] for r in doc.get('roles',[]) if r.get('role')=='developer' and r.get('mint')==mint}
        events=[];seen=set()
        for r in doc.get('roles',[])[:200]:
            if (r.get('service_boundary') or {}).get('control_link_eligible') is False:continue
            if r.get('address') not in participants or r.get('role') not in ('funding_recipient','setup_user'):continue
            p=r.get('proof') or {};key=p.get('source_key')
            if not key or key in seen or not _finite(p.get('event_at')) or not _finite(p.get('known_at')) or max(p['event_at'],p['known_at'])>cutoff:continue
            seen.add(key);events.append(dict(kind=p.get('kind','preparation'),event_at=p['event_at'],source_key=key))
        receipt=launch.get('source_receipt')
        if _finite(receipt) and receipt<=cutoff:events.append(dict(kind='create',event_at=receipt,signature=launch.get('source_signature')))
        events.extend(dict(b,actors=len(b['actors'])) for b in buys.values())
        events.sort(key=lambda e:(e['event_at'],e['kind'],e.get('signature',e.get('source_key',''))))
        shape=[(e['kind'],e.get('actors')) for e in events]
        out.append(dict(mint=mint,launch_id=launch['launch_id'],source_signature=launch.get('source_signature'),action_cut=launch.get('emitted_at'),sequence=events,
            sequence_fingerprint=hashlib.sha256(json.dumps(shape).encode()).hexdigest(),atomic_seed_observed=any(b['same_transaction_as_create'] for b in buys.values()),coverage='observed events only; absent stages and equal-time ordering unknown'))
    return out


def operation_history(net,*,as_of=None,limit=100):
    now=net.clock();cut=now if as_of is None else as_of
    if not _finite(cut) or not _finite(now) or cut>now or type(limit) is not int or not 1<=limit<=100:return dict(state='refused',reason='bad_arguments',items=[])
    c=None;deadline=time.monotonic()+1
    try:
        c=sqlite3.connect(net.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0);c.row_factory=sqlite3.Row
        c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
        from .wallet_campaign import _label_service_boundaries
        from . import wallet_watch as W
        infrastructure=W._known_infrastructure();tips=W._known_tip_receivers() if infrastructure is not None else frozenset()
        campaigns=c.execute('SELECT * FROM wallet_campaigns WHERE first_recorded_at<=? ORDER BY first_recorded_at DESC,campaign_id LIMIT ?',(cut,limit+1)).fetchall()
        def latest(cid,at,strict=False):
            op='<' if strict else '<='
            row=c.execute('SELECT doc_json FROM wallet_campaign_versions WHERE campaign_id=? AND recorded_at'+op+'? AND source_as_of'+op+'? ORDER BY revision DESC LIMIT 1',(cid,at,at)).fetchone()
            if row is None:return None
            doc=json.loads(row[0])
            for launch in doc.get('launches',[]):
                if 'emitted_at' not in launch:
                    stamp=c.execute('SELECT emitted_at FROM wallet_campaign_launches WHERE launch_id=?',(launch['launch_id'],)).fetchone()
                    launch['emitted_at']=stamp[0] if stamp and _finite(stamp[0]) and stamp[0]<=at else None
            return _label_service_boundaries(doc,infrastructure,tips)
        items=[];truncated=len(campaigns)>limit
        for target in campaigns[:limit]:
            if time.monotonic()>deadline:truncated=True;break
            current=latest(target['campaign_id'],cut)
            if current is None:continue
            boundary=current.get('service_boundary') or {}
            if boundary.get('state')=='unknown' or boundary.get('control_link_eligible') is False or current.get('family_link_eligible') is False:
                items.append(dict(campaign_id=target['campaign_id'],service_boundary=boundary,prior_state='unknown',prediction_state=current.get('prediction_state'),ownership_proven=False));continue
            for operation in _operations(current,cut):
                action=operation['action_cut'];counts={};seen=set();no_launch=0;prior_count=0
                if not _finite(action) or action>cut:
                    items.append(dict(campaign_id=target['campaign_id'],**operation,prior_state='unknown',prior_launches=None,prior_sequence_counts={},ownership_proven=False));continue
                peers=c.execute('SELECT campaign_id FROM wallet_campaigns WHERE seed=? AND campaign_id!=? AND first_recorded_at<? ORDER BY first_recorded_at DESC,campaign_id LIMIT 21',(target['seed'],target['campaign_id'],action)).fetchall()
                for peer in peers[:20]:
                    prior=latest(peer[0],action,True)
                    if prior is None:continue
                    boundary=prior.get('service_boundary') or {}
                    if boundary.get('state')=='unknown' or boundary.get('control_link_eligible') is False or prior.get('family_link_eligible') is False:continue
                    old=_operations(prior,action)
                    if not old:no_launch+=1
                    for op in old:
                        key=(op['mint'],op['source_signature'])
                        if op['mint']==operation['mint'] or key in seen or not _finite(op['action_cut']) or op['action_cut']>=action:continue
                        seen.add(key);prior_count+=1;f=op['sequence_fingerprint'];counts[f]=counts.get(f,0)+1
                items.append(dict(campaign_id=target['campaign_id'],**operation,prior_as_of=action,prior_state='partial',prior_launches=prior_count,prior_sequence_counts=counts,
                    prior_preparations_without_observed_launch=no_launch,prior_truncated=len(peers)>20,ownership_proven=False,relation='same_seed_observed_pattern_not_controller',prediction_state='unvalidated'))
        return dict(state='partial',as_of=cut,items=items,truncated=truncated,note='Current sequence is descriptive. Prior counts use only revisions available before launch action, excluding current mint and duplicate launches. Same seed can be a shared service; no full-history or profit inference.')
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return dict(state='unknown',reason='operation_history_unavailable',items=[])
    finally:
        if c is not None:c.close()
