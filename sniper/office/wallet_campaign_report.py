"""Bounded read-only campaign evidence report; no trade or promotion authority."""
import json
import math
import sqlite3
import time
from pathlib import Path
from sniper.office.wallet_campaign_quotes import evaluate_round_trip,day
from sniper.office.wallet_network import WalletNetwork


def _ro(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=0);c.row_factory=sqlite3.Row
    deadline=time.monotonic()+1
    c.set_progress_handler(lambda:time.monotonic()>deadline,1000)
    return c


def _unknown(reason):return dict(state='unknown',reason=reason,items=[])


def _resource(c,table,keys,cut):
    try:
        row=c.execute('SELECT r.origin_epoch,r.first_write_epoch,r.last_write_epoch FROM resource_record_epochs r JOIN resource_epochs e ON e.epoch_id=r.last_write_epoch WHERE r.table_name=? AND r.record_key=? AND e.first_seen_host<=?',(table,json.dumps(keys,separators=(',',':')),cut)).fetchone()
        if row:return dict(row)
    except sqlite3.Error:pass
    return dict(origin_epoch=None,first_write_epoch=None,last_write_epoch=None)


def build_report(state_root,*,as_of=None,limit=100):
    now=time.time() if as_of is None else as_of
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0 or type(limit) is not int or not 1<=limit<=200:raise ValueError('bad_report_cutoff_or_limit')
    root=Path(state_root);path=root/'data/office/wallet_network.db'
    out=dict(as_of=now,funded_authority=False,profit_proven=False,coverage='bounded observed evidence; missing is not absence')
    try:
        from sniper.office.wallet_campaign import read_campaigns
        out['campaigns']=read_campaigns(WalletNetwork(path),as_of=now,limit=min(limit,100))
    except (ImportError,OSError,sqlite3.Error,ValueError,TypeError):out['campaigns']=_unknown('campaign_history_unavailable')
    out['preparation']=dict(state=out['campaigns']['state'],basis='persisted funding/setup preparations, including no observed launch')
    from sniper.office.wallet_campaign_history import operation_history
    out['history']=operation_history(WalletNetwork(path),as_of=now,limit=min(limit,100))
    c=None
    try:
        c=_ro(path)
        timing=[dict(r) for r in c.execute('SELECT * FROM watch_flow_timing WHERE graph_commit_before<=? AND (graph_commit_after IS NULL OR graph_commit_after<=?) ORDER BY rowid DESC LIMIT ?',(now,now,limit+1))]
        for r in timing[:limit]:
            before,after=r['graph_commit_before'],r['graph_commit_after']
            r['availability']='bounded' if after is not None and after>=before else 'unknown'
            r['receipt_to_commit_upper_s']=after-r['received_at'] if after is not None and after>=r['received_at'] else None
        out['timing']=dict(state='partial',items=timing[:limit],truncated=len(timing)>limit,note='Commit availability is a bracket; provider ACK and gap coverage are separate.')
        def token_rows(table):
            return [dict(r) for r in c.execute('SELECT a.*,t.graph_commit_after AS available_at FROM '+table+" a JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id AND t.signature=a.signature WHERE t.flow_state='known' AND t.graph_commit_after>0 AND t.graph_commit_after<=? AND a.known_at<=? ORDER BY a.rowid DESC LIMIT ?",(now,now,limit+1))]
        accounts=token_rows('watch_token_accounts');transfers=token_rows('watch_token_transfers')
        out['inventory']=dict(state='partial',items=accounts[:limit],transfers=transfers[:limit],truncated=len(accounts)>limit or len(transfers)>limit,total_wallet_holdings='unknown',note='Touched accounts and supported transfers only. Transfers are not sales or control.')
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        out.setdefault('timing',_unknown('flow_timing_unavailable'));out['inventory']=_unknown('token_observations_unavailable')
    finally:
        if c is not None:c.close()
    roles=[]
    for transfer in out['inventory'].get('transfers',[]):
        if transfer['state']!='known':continue
        for role,key in [('token_sender','source_owner'),('token_recipient','destination_owner')]:
            address=transfer.get(key)
            if address:
                roles.append(dict(role=role,address=address,mint=transfer['mint'],signature=transfer['signature'],source=transfer['source'],source_flow_id=transfer['flow_id'],
                    proof_ref=transfer['fetch_id'],known_at=transfer['available_at'],ownership_proven=False,relation='observed_token_transfer_not_control'))
    out['roles']=dict(state=out['inventory']['state'],items=roles,note='Transaction roles only; related wallets are not automatically one controller.')
    directory=root/'data/research/dex_boost';studies=[];round_trips=[];files=[];errors=[];remaining=limit;censors={}
    for p in sorted(directory.glob('????-??-??.db'),reverse=True):
        if p.stem>day(now):continue
        if len(files)>=3 or remaining<=0:break
        c=None
        try:
            c=_ro(p)
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='campaign_studies'").fetchone():continue
            rows=c.execute('SELECT * FROM campaign_studies WHERE recorded_at<=? ORDER BY rowid DESC LIMIT ?',(now,remaining)).fetchall();files.append(p.stem)
            for raw in rows:
                s=dict(raw);q=[dict(r) for r in c.execute('SELECT * FROM campaign_quotes WHERE study_id=? AND (requested_host<=? OR (requested_host IS NULL AND observed_host<=?))',(s['study_id'],now,now))]
                for r in q:
                    r['resource']=_resource(c,'campaign_quotes',[s['study_id'],r['arm'],r['offset_s']],now)
                    if r['response_host'] is not None and r['response_host']>now:
                        r.update(state='censored',reason='response_not_available_as_of',acquired_qty_atomic=None,expected_output_atomic=None)
                    if r['state']!='known':censors[r['reason'] or 'unknown']=censors.get(r['reason'] or 'unknown',0)+1
                entries={r['arm']:r for r in q if r['offset_s']==0}
                for r in q:
                    if r['offset_s'] and r['arm'] in entries:
                        value=evaluate_round_trip(entries[r['arm']],r)
                        entry_resource=entries[r['arm']]['resource'];exit_resource=r['resource']
                        attributed=all(x['origin_epoch'] not in (None,'legacy_unattributed') for x in (entry_resource,exit_resource))
                        value.update(entry_resource=entry_resource,exit_resource=exit_resource,resource_comparison='unattributed' if not attributed else 'same_epoch' if entry_resource['origin_epoch']==exit_resource['origin_epoch'] and entry_resource['last_write_epoch']==exit_resource['last_write_epoch'] else 'mixed_epochs')
                        round_trips.append(dict(study_id=s['study_id'],arm=r['arm'],offset_s=r['offset_s'],**value))
                studies.append(dict(study_id=s['study_id'],mint=s['mint'],version=s['version'],source_receipt=s['source_receipt'],recorded_at=s['recorded_at'],quotes_available_at_cut=len(q),net_profit_state='unproved',resource=_resource(c,'campaign_studies',[s['study_id']],now),snapshot=s['snapshot_json']))
                if s['reason'] and s['reason']!='quote_only_not_fills':censors['source:'+s['reason']]=censors.get('source:'+s['reason'],0)+1
                remaining-=1
        except (OSError,sqlite3.Error,ValueError,TypeError):errors.append(p.name)
        finally:
            if c is not None:c.close()
    out['quotes']=dict(state='partial' if files else 'unknown',reason='quote_only_not_fills' if files else 'campaign_quotes_unavailable',studies=studies,round_trips=round_trips,censor_reasons=censors,files=files,errors=errors,limit=limit,note='Three latest available study-day files at most. Gross is conditional on expected entry quantity; fees/landing/censored attempts prevent a net strategy verdict.')
    out['unresolved']=[name for name in ('campaigns','preparation','inventory','history','timing','quotes') if out[name]['state']!='known']
    return out
