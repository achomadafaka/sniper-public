"""Paged stored research paths. A lead key is never an organization or control claim."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from . import wallet_watch as W
from .wallet_campaign import _Stop,_address,_connect,_finite,_json,_launch_fact,_strict,_write

VERSION='wallet-group-leads-v1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS wallet_group_leads(version TEXT NOT NULL,lead_id TEXT PRIMARY KEY,kind TEXT NOT NULL,anchor_json TEXT NOT NULL,first_known_at REAL);
CREATE INDEX IF NOT EXISTS group_lead_first ON wallet_group_leads(first_known_at);
CREATE TABLE IF NOT EXISTS wallet_group_lead_evidence(evidence_id TEXT PRIMARY KEY,lead_id TEXT NOT NULL,source TEXT NOT NULL,source_key TEXT NOT NULL,source_known_at REAL,recorded_at REAL,path_json TEXT NOT NULL,quality_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS group_lead_evidence ON wallet_group_lead_evidence(lead_id,recorded_at);
CREATE INDEX IF NOT EXISTS group_lead_pending ON wallet_group_lead_evidence(recorded_at);
CREATE TABLE IF NOT EXISTS wallet_group_lead_cursors(source TEXT PRIMARY KEY,cursor INTEGER NOT NULL,last_read REAL NOT NULL,identity TEXT);
'''


def _key(*parts):return hashlib.sha256(_json([VERSION,*parts]).encode()).hexdigest()


def _cursor(net,c,now,source,cursor,identity):
    old=c.execute('SELECT 1 FROM wallet_group_lead_cursors WHERE source=?',(source,)).fetchone()
    _write(net,c,now,'INSERT INTO wallet_group_lead_cursors VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,last_read=excluded.last_read',
        (source,cursor,now,identity),rows=0 if old else 1)


def _clock(net,c,now,value):
    old=c.execute("SELECT last_read FROM wallet_group_lead_cursors WHERE source='clock'").fetchone()
    if old and (not _finite(old[0]) or value<old[0]):raise _Stop('group_clock_regressed')
    if old is None or value>old[0]:_cursor(net,c,value,'clock',0,None)
    return max(value,old[0] if old else value)


def _graph(row,chain,c,now):
    if not (_address(row['src']) and _address(row['dst'])):return None
    quality=dict(strength='stored_claim',source=row['source'],proof_state=row['proof_state'],proof_ref=row['proof_ref'],method=row['method_version'])
    path=dict(network_id=row['network_id'],edge_id=row['edge_id'],src=row['src'],dst=row['dst'],observed_at=row['observed_at'],proof_ref=row['proof_ref'])
    if row['type']=='created_mint':
        # Keep the legacy key for immutable IDs; imported creator fields do not identify a signer.
        kind,anchors='developer_key',[row['src']];path.update(creator_claim=row['src'],mint=row['dst']);quality['creation_role']='creator_claim'
        if row['source']=='import':quality['strength']='import_lead'
        elif row['source']=='host' and row['proof_state']=='verified':
            try:fact=_launch_fact(row,chain,None,c,now,'')
            except (ValueError,KeyError,TypeError,sqlite3.Error):fact=None
            quality['strength']='strict_creation' if fact else 'unverified_creation_lead'
            if fact:
                path.pop('creator_claim');path.update(developer=fact['developer'],signature=fact['signature'],slot=fact['slot']);quality['creation_role']='signing_developer'
    elif row['type']=='funded_by':
        kind,anchors='funder_key',[row['dst']];path.update(funder=row['dst'],recipient=row['src'])
        quality['strength']='strict_funding' if _strict(row) else 'import_lead' if row['source']=='import' else 'legacy_funding_lead'
    else:return None
    return kind,anchors,path,quality,row['known_at']


def _token(row,c,now):
    src,dst=row.get('source_owner'),row.get('destination_owner')
    owners=_address(src) and _address(dst)
    anchors=[src,dst] if owners else [row.get('source_account'),row.get('destination_account')]
    if not all(_address(a) for a in anchors) or not _address(row.get('mint')):return None
    known=None
    if _finite(row.get('known_at')) and row['known_at']<=now:
        try:
            t=c.execute('SELECT signature,flow_state,graph_commit_after FROM watch_flow_timing WHERE source=? AND flow_id=?',(row.get('source'),row.get('flow_id'))).fetchone()
            if t and t['signature']==row['signature'] and t['flow_state']=='known' and _finite(t['graph_commit_after']) and row['known_at']<=t['graph_commit_after']<=now:known=t['graph_commit_after']
        except sqlite3.Error:pass
    amount=row.get('amount_atomic');valid_amount=isinstance(amount,str) and amount.isascii() and amount.isdigit() and len(amount)<=78
    gross=(known is not None and owners and valid_amount and row.get('program') in {
        'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
        and (row['state']=='known' or row.get('reason')=='token2022_fee_unknown'))
    quality=dict(strength='gross_instruction' if gross and row.get('credit_atomic') is None else 'observed_transfer' if gross else 'unverified_token_lead',
        net_credit_state='unknown' if row.get('credit_atomic') is None else 'recorded',reason=row.get('reason'),proof_ref=row.get('fetch_id'),ownership_proven=False)
    path={k:row.get(k) for k in ('signature','outer_ix','inner_ix','source_account','destination_account','source_owner','destination_owner','mint','program','amount_atomic','decimals','credit_atomic','fee_atomic','source','flow_id')}
    return 'token_owner_pair' if owners else 'token_account_pair',anchors,path,quality,known


def _record(net,c,now,source,source_key,proposal,infrastructure,tips):
    if proposal is None:return 0
    kind,anchors,path,quality,known=proposal
    known=known if _finite(known) and known<=now else None
    quality['service_boundary']=W._service_boundary([*anchors,path.get('src'),path.get('dst'),path.get('source_owner'),path.get('destination_owner')],infrastructure,tips)
    quality.update(shared_infrastructure=None if infrastructure is None else any(a in infrastructure for a in anchors),source_knowledge_state='known' if known is not None else 'unknown')
    lid=_key(kind,path.get('network_id','solana'),anchors);eid=_key(lid,source,source_key)
    if c.execute('SELECT 1 FROM wallet_group_lead_evidence WHERE evidence_id=?',(eid,)).fetchone():return 0
    if not c.execute('SELECT 1 FROM wallet_group_leads WHERE lead_id=?',(lid,)).fetchone():
        _write(net,c,now,'INSERT INTO wallet_group_leads VALUES(?,?,?,?,NULL)',(VERSION,lid,kind,_json(anchors)))
    _write(net,c,now,'INSERT INTO wallet_group_lead_evidence VALUES(?,?,?,?,?,NULL,?,?)',(eid,lid,source,source_key,known,_json(path),_json(quality)))
    return 1


def scan_group_leads(net,now,limit=100):
    """One bounded raw page per lane; durable cursors allow an uncapped number of calls."""
    if not _finite(now) or type(limit) is not int or not 1<=limit<=1000:return dict(state='refused',reason='bad_arguments',inserted=0,items=[],has_more=False)
    entry=net.clock()
    if not _finite(entry) or now>entry:return dict(state='refused',reason='future_or_invalid_clock',inserted=0,items=[],has_more=False)
    deadline=time.monotonic()+1;work_deadline=deadline-.2;c=chain=None;inserted=0;coverage={}
    try:
        stat=Path(net.path).stat();identity=str(stat.st_dev)+':'+str(stat.st_ino)
        c=_connect(net.path,deadline,write=True);c.execute('PRAGMA busy_timeout=100');c.executescript(SCHEMA);c.execute('BEGIN IMMEDIATE')
        floor=_clock(net,c,now,entry);c.commit();c.execute('BEGIN IMMEDIATE')
        try:chain=_connect(net.chain_db,deadline) if net.chain_db else None
        except (OSError,sqlite3.Error):pass
        infrastructure=W._known_infrastructure();tips=W._known_tip_receivers() if infrastructure is not None else frozenset()
        turn=c.execute("SELECT cursor FROM wallet_group_lead_cursors WHERE source='turn'").fetchone();turn=turn[0] if turn else 0
        quotas=[limit//3]*3
        for i in range(limit%3):quotas[(turn+i)%3]+=1
        for source,table,pk,quota in zip(('edges','tokens','imports'),('edges','watch_token_transfers','edges'),('edge_id','rowid','edge_id'),quotas):
            if quota==0:coverage[source]=dict(state='partial',has_more=True,reason='next_page_turn');continue
            old=c.execute('SELECT * FROM wallet_group_lead_cursors WHERE source=?',(source,)).fetchone()
            if old and old['identity']!=identity:raise _Stop('group_source_replaced')
            cursor=old['cursor'] if old else 0
            try:
                if source!='tokens':
                    prefix="network_id='solana' AND (source='import')=?"
                    maximum=c.execute('SELECT edge_id FROM edges INDEXED BY edges_overview WHERE '+prefix+' ORDER BY edge_id DESC LIMIT 1',(int(source=='imports'),)).fetchone()
                else:maximum=c.execute(f'SELECT {pk} FROM {table} ORDER BY {pk} DESC LIMIT 1').fetchone()
                if maximum and maximum[0]<cursor:raise _Stop('group_source_replaced')
                watermark=maximum[0] if maximum else 0
                if source!='tokens':
                    rows=c.execute('SELECT edge_id AS scan_id,* FROM edges INDEXED BY edges_overview WHERE '+prefix+' AND edge_id>? AND edge_id<=? ORDER BY edge_id LIMIT ?',(int(source=='imports'),cursor,watermark,quota+1)).fetchall()
                else:rows=c.execute(f'SELECT {pk} AS scan_id,* FROM {table} WHERE {pk}>? AND {pk}<=? ORDER BY {pk} LIMIT ?',(cursor,watermark,quota+1)).fetchall()
            except sqlite3.Error:coverage[source]=dict(state='unknown',reason='source_table_unknown',has_more=False);continue
            more=len(rows)>quota;scanned=0;deferred=None
            for raw in rows[:quota]:
                if time.monotonic()>work_deadline:deferred='work_budget';more=True;break
                row=dict(raw);known=row.get('known_at');observed=row.get('observed_at')
                if _finite(known) and known>now or _finite(observed) and observed>now:deferred='future_source_clock';more=True;break
                proposal=_graph(row,chain,c,now) if source!='tokens' else _token(row,c,now)
                key=str(row['edge_id']) if source!='tokens' else _json([row['signature'],row['outer_ix'],row['inner_ix']])
                if proposal is not None:proposal[3].update(discovery_as_of=now,source_watermark=watermark,source_identity=identity)
                inserted+=_record(net,c,now,source,key,proposal,infrastructure,tips)
                cursor=row['scan_id'];scanned+=1
            if scanned:_cursor(net,c,now,source,cursor,identity)
            coverage[source]=dict(state='partial' if more else 'known',cursor=cursor,scanned=scanned,has_more=more,reason=deferred,as_of=now,watermark=watermark,identity=identity)
        if limit%3:_cursor(net,c,now,'turn',(turn+1)%3,None)
        before=net.clock()
        if not _finite(before) or before<floor:raise _Stop('group_clock_regressed')
        floor=_clock(net,c,now,before);c.commit()
        emitted=net.clock()
        if _finite(emitted) and emitted>=floor:
            c.execute('BEGIN IMMEDIATE');_clock(net,c,emitted,emitted);c.commit();c.execute('BEGIN IMMEDIATE')
            for table,column in (('wallet_group_lead_evidence','recorded_at'),('wallet_group_leads','first_known_at')):
                eligible=" AND EXISTS(SELECT 1 FROM wallet_group_lead_evidence e WHERE e.lead_id=wallet_group_leads.lead_id AND e.recorded_at IS NOT NULL)" if table=='wallet_group_leads' else ''
                for row in c.execute(f'SELECT rowid FROM {table} WHERE {column} IS NULL'+eligible+' LIMIT ?',(limit,)).fetchall():
                    if time.monotonic()>deadline-.02:break
                    _write(net,c,emitted,f'UPDATE {table} SET {column}=? WHERE rowid=?',(emitted,row[0]))
            c.commit()
        pending=sum(c.execute(f'SELECT count(*) FROM {table} WHERE {column} IS NULL').fetchone()[0] for table,column in (('wallet_group_leads','first_known_at'),('wallet_group_lead_evidence','recorded_at')))
        return dict(state='partial',inserted=inserted,has_more=bool(pending) or any(v['has_more'] for v in coverage.values()),pending_markers=pending,coverage=coverage,organization_count=None)
    except _Stop as exc:
        if c is not None:c.rollback()
        return dict(state='unknown',reason=str(exc),inserted=0,has_more=True,items=[])
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError) as exc:
        if c is not None:c.rollback()
        diagnostic={key:getattr(exc,key) for key in ('sqlite_errorcode','sqlite_errorname') if isinstance(exc,sqlite3.Error) and hasattr(exc,key)}
        return dict(state='unknown',reason='group_read_budget' if time.monotonic()>deadline else 'group_store_unknown',inserted=0,has_more=True,items=[],**diagnostic)
    finally:
        for connection in (chain,c):
            if connection is not None:connection.close()


def read_group_leads(net,as_of=None,limit=100):
    now=net.clock();cut=now if as_of is None else as_of
    if not _finite(now) or not _finite(cut) or cut>now or type(limit) is not int or not 1<=limit<=100:return dict(state='refused',reason='bad_arguments',items=[])
    c=None;deadline=time.monotonic()+1
    try:
        c=_connect(net.path,deadline)
        infrastructure=W._known_infrastructure();tips=W._known_tip_receivers() if infrastructure is not None else frozenset()
        rows=c.execute('SELECT * FROM wallet_group_leads WHERE first_known_at<=? ORDER BY first_known_at DESC,lead_id LIMIT ?',(cut,limit+1)).fetchall();items=[]
        for row in rows[:limit]:
            evidence=c.execute('SELECT * FROM wallet_group_lead_evidence WHERE lead_id=? AND recorded_at<=? AND (source_known_at IS NULL OR source_known_at<=?) ORDER BY recorded_at,evidence_id LIMIT 101',(row['lead_id'],cut,cut)).fetchall()
            facts=[dict(evidence_id=e['evidence_id'],source=e['source'],source_key=e['source_key'],source_known_at=e['source_known_at'],recorded_at=e['recorded_at'],path=json.loads(e['path_json']),quality=json.loads(e['quality_json'])) for e in evidence[:100]]
            for fact in facts:
                path,quality=fact['path'],fact['quality']
                quality['service_boundary']=W._service_boundary([*json.loads(row['anchor_json']),path.get('src'),path.get('dst'),path.get('source_owner'),path.get('destination_owner')],infrastructure,tips)
            if row['kind']=='developer_key':
                for fact in facts:
                    path,quality=fact['path'],fact['quality']
                    strict=(quality.get('strength')=='strict_creation' and quality.get('source')=='host' and quality.get('proof_state')=='verified'
                        and quality.get('method') in ('confirmed_pump_create_v1','wallet-watch-launch-v1') and path.get('developer') in json.loads(row['anchor_json'])
                        and isinstance(path.get('signature'),str) and type(path.get('slot')) is int and path['slot']>0)
                    quality['creation_role']='signing_developer' if strict else 'creator_claim'
                    if not strict and 'developer' in path:path['creator_claim']=path.pop('developer')
            items.append(dict(version=row['version'],lead_id=row['lead_id'],kind='creation_key' if row['kind']=='developer_key' else row['kind'],anchors=json.loads(row['anchor_json']),first_known_at=row['first_known_at'],evidence=facts,evidence_truncated=len(evidence)>100,
                shared_infrastructure=any(e['quality']['shared_infrastructure'] is True for e in facts),control_state='unproven',classification='research_lead_not_organization'))
        pending=sum(c.execute(f'SELECT count(*) FROM {table} WHERE {column} IS NULL').fetchone()[0] for table,column in (('wallet_group_leads','first_known_at'),('wallet_group_lead_evidence','recorded_at'))) if as_of is None else None
        return dict(state='partial',items=items,has_more=len(rows)>limit,as_of=cut,organization_count=None,pending_markers=pending,coverage='Stored paged evidence only; common services and token ownership are not common control.')
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return dict(state='unknown',reason='group_report_unknown',items=[])
    finally:
        if c is not None:c.close()
