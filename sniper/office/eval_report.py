"""Bounded, non-mutating eval-1 reporting. Economics require an external look seal.

Independent SQLite snapshots share an explicit logical cut, not an atomic cross-file
snapshot. A changed input refuses a saved look. No report call registers a look.
"""
import hashlib
import calendar
import json
import math
import re
import random
import statistics
import sys
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import store as S, judge_decisions as J, ledger as L, costs
from .tools import sheet as SH
from .supervisor import action_key

LOOK_CEILINGS = {'max_rows':10000,'max_queries':10000,'max_bytes':64*1024*1024,'deadline_s':10.0}
DEFAULT_LIMITS = {'max_rows': 1000, 'max_queries': 512, 'max_bytes': 4*1024*1024, 'deadline_s': 1.0}
DENOMINATOR = 'admitted by the near-pass cohort hard filter (desk tickets)'


def digest(value):
    return hashlib.sha256(S.canonical_json(value)).hexdigest()


def script_sha256():
    """Pin the reporting program and the shared helpers that can affect its output."""
    root=Path(__file__).parents[2]
    files=('sniper/office/eval_report.py','scripts/office_eval_report.py','scripts/office_eval_seal_prepare.py',
           'sniper/office/ledger.py','sniper/office/store.py','sniper/office/judge_decisions.py',
           'sniper/office/supervisor.py','sniper/office/tools/sheet.py','sniper/office/costs.py')
    return digest({'files':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
                   'python':list(sys.version_info[:3])})


class ReadLimit(ValueError):
    pass


class Reader:
    """One query/byte/deadline budget shared by every source and collection."""
    def __init__(self, limits):
        self.limits = limits
        self.until = time.monotonic()+limits['deadline_s']
        self.queries = self.bytes = 0
        self.heads = {}
        self.optional_heads = {}
        self.optional_until = self.until
        self.missing = []
        self.optional = []
        self.connections = []

    def check(self):
        if time.monotonic() > self.until:
            raise ReadLimit('deadline')

    def open(self, path):
        # mode=ro refuses missing files; no open_store/open_ledger/bootstrap here.
        conn = sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True, timeout=0.025)
        self.connections.append(conn)
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.limits['max_bytes'])
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        conn.set_progress_handler(lambda: int(time.monotonic() > self.until), 1000)
        return conn

    def read(self, conn, label, sql, args=(), *, required=True, cap=None, project=None):
        try:
            deadline=self.until if required else self.optional_until
            if time.monotonic()>deadline:raise ReadLimit('optional_deadline' if not required else 'deadline')
            conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            self.check()
            self.queries += 1
            if self.queries > self.limits['max_queries']:
                raise ReadLimit('query_limit')
            rows = []
            cap = self.limits['max_rows'] if cap is None else cap
            cursor = conn.execute(sql+' LIMIT ?', (*args, cap+1))
            for row in cursor:
                self.check()
                if time.monotonic()>deadline:raise ReadLimit('optional_deadline')
                item = dict(row)
                self.bytes += len(S.canonical_json(item))
                if self.bytes > self.limits['max_bytes']:
                    raise ReadLimit('byte_limit')
                rows.append(project(item) if project is not None else item)
                if len(rows) > cap:
                    raise ReadLimit('row_limit')
            (self.heads if required else self.optional_heads)[label] = {'count': len(rows), 'sha256': digest(rows)}
            return rows
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            (self.missing if required else self.optional).append(label)
            (self.heads if required else self.optional_heads)[label] = {'state': 'incomplete'}
            return []
        finally:
            conn.set_progress_handler(lambda:int(time.monotonic()>self.until),1000)

    def close(self):
        for conn in self.connections:
            conn.close()


def _object(raw):
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError('object_required')
    return obj


def _number(v):
    return type(v) in (int, float) and math.isfinite(v)


def _receipt(row):
    payload = _object(row['payload_json'])
    canonical = S.canonical_json(payload)
    if (canonical.decode() != row['payload_json'] or
            hashlib.sha256(canonical+row['prev_hash'].encode('ascii')).hexdigest() != row['row_hash']):
        raise ValueError('receipt_integrity')
    if 'prior_hash' in row and row['prev_hash']!=('' if row['seq']==1 else row['prior_hash']):
        raise ValueError('receipt_predecessor')
    return payload


def _order_asof(row, now):
    """Hash causal economic columns, never mutable after-cut state or cumulative fees."""
    fixed=('order_id','book_id','epoch_id','book_kind','network_id','mint','pool','decision_bundle_hash',
           'policy_json','policy_hash','fill_model_version','requested_at')
    out={key:row[key] for key in fixed}
    filled=row['filled_at'] is not None and row['filled_at']<=now
    closed=filled and row['closed_at'] is not None and row['closed_at']<=now
    out.update(filled_at=row['filled_at'] if filled else None,closed_at=row['closed_at'] if closed else None,
               state='closed' if closed else 'open' if filled else 'unfilled_status_unknown')
    for key in ('spend_usd','spend_lamports','tokens_atomic'):
        out[key]=row[key] if filled else None
    requested=row.get('exit_requested_at')
    out['exit_by']=row.get('exit_by') if closed or (requested is not None and requested<=now) else None
    halt=row.get('epoch_halted_at')
    out['epoch_halted_at']=halt if halt is not None and halt<=now else None
    out['post_halt']=bool(row.get('post_halt')) if out['epoch_halted_at'] is not None else False
    return out


def _epoch_asof(row, now):
    epoch=_object(row['payload_json']);epoch.pop('first_order_at',None)
    if epoch.get('closed_at') is not None and epoch['closed_at']>now:epoch['closed_at']=None
    return {'id':row['id'],'payload_json':S.canonical_json(epoch).decode()}


def _safety_snapshot(row):
    fields=('state','config_hash','last_equity_read_at','halt_kind','halted_at')
    return {key:row.get(key) for key in fields} if row else {'state':'unknown'}


def _load_required(r,o,l,epoch_id,now,record):
    epochs=r.read(o,'office.epochs','SELECT * FROM epochs WHERE id=?',(epoch_id,),project=lambda x:_epoch_asof(x,now))
    epoch=_object(epochs[0]['payload_json']) if epochs else {}
    if not epochs:r.missing.append('office.epoch_absent')
    control=r.read(o,'office.office_epoch_control','SELECT * FROM office_epoch_control WHERE id=1',
        project=lambda row:{'id':row['id'],'schema_valid':isinstance(_object(row['payload_json']).get('active_epoch'),str)})
    if not control or not control[0]['schema_valid']:r.missing.append('office.control_absent')
    # The active pointer is live metadata, not a claim that this historical epoch is still active.
    book=epoch.get('book_id',epoch_id)
    data={'epoch':epoch,'book':book,'o':o,'l':l,'cohort':epoch.get('record',{}).get('near_pass_cohort',{})}
    data['config']=r.read(l,'ledger.ledger_epochs','SELECT * FROM ledger_epochs WHERE epoch_id=?',(epoch_id,))
    if not data['config']:r.missing.append('ledger.epoch_absent')
    else:
        cfg=_object(data['config'][0]['note']).get('paper_eval',{})
        keys={'version','drawdown_usd','unknown_max_s','mark_max_age_s','clock_skew_tolerance_s','leg_stale_demote_s'}
        if set(cfg)!=keys or cfg.get('version')!=epoch_id or any(not _number(cfg[k]) or cfg[k]<0 for k in keys-{'version'}):
            r.missing.append('ledger.config_invalid')
    data['books']=r.read(l,'ledger.books','SELECT * FROM books WHERE book_id=? AND epoch_id=?',(book,epoch_id))
    if len(data['books'])!=1 or data['books'][0]['kind']!='evaluated':r.missing.append('ledger.book_scope')
    data['orders']=r.read(l,'ledger.orders',
        'SELECT * FROM orders INDEXED BY orders_book_state WHERE book_id=? AND epoch_id=? AND requested_at<=?',
        (book,epoch_id,now),project=lambda row:_order_asof(row,now))
    data['cashflows']=r.read(l,'ledger.cashflows',
        'SELECT c.* FROM orders o INDEXED BY orders_book_state JOIN cashflows c USING(order_id) '
        'WHERE o.book_id=? AND o.requested_at<=? AND c.at<=?',(book,now,now))
    data['orders'].sort(key=lambda row:row['order_id'])
    data['cashflows'].sort(key=lambda row:row['id'])
    for key in ('orders','cashflows'):
        if 'ledger.'+key not in r.missing:r.heads['ledger.'+key]={'count':len(data[key]),'sha256':digest(data[key])}
    flowkeys=defaultdict(list)
    for c in data['cashflows']:flowkeys[(c['order_id'],c['kind'])].append(c)
    for order in data['orders']:
        if order['filled_at'] is None:continue
        for kind in ('entry','exit') if order['closed_at'] is not None else ('entry',):
            rows=flowkeys[(order['order_id'],kind)]
            if len(rows)!=1 or not _number(rows[0]['usd']):r.missing.append('ledger.cashflow_incomplete')
    capital=r.read(l,'ledger.capital_flows','SELECT * FROM capital_flows WHERE id>0 AND at<=? ORDER BY id',(now,))
    data['capital']=[c for c in capital if c['book_id']==book]
    if not any(c['kind']=='initial' and _number(c['usd']) and c['usd']>=0 for c in data['capital']):
        r.missing.append('ledger.initial_capital_absent')
    if 'ledger.capital_flows' not in r.missing:
        r.heads['ledger.capital_flows']={'count':len(data['capital']),'sha256':digest(data['capital'])}
    data['askers']=r.read(l,'ledger.exit_askers',
        'SELECT a.* FROM orders o INDEXED BY orders_book_state JOIN exit_askers a USING(order_id) '
        'WHERE o.book_id=? AND o.requested_at<=? AND a.asked_at<=?',(book,now,now))
    data['askers'].sort(key=lambda row:(row['order_id'],row['slot']))
    if 'ledger.exit_askers' not in r.missing:r.heads['ledger.exit_askers']={'count':len(data['askers']),'sha256':digest(data['askers'])}
    data['marks']=[];data['priced_marks']={};data['leg_safety']=[]
    for order in data['orders']:
        if order['filled_at'] is None or order['closed_at'] is not None:continue
        oid=order['order_id']
        for suffix,extra in (('latest',''),('priced',' AND lamports IS NOT NULL')):
            rows=r.read(l,'ledger.marks:'+oid+':'+suffix,
                'SELECT * FROM (SELECT * FROM marks WHERE order_id=? AND at<=?'+extra+' ORDER BY at DESC,id DESC LIMIT 1)',(oid,now))
            if suffix=='latest':data['marks'].extend(rows)
            elif rows:data['priced_marks'][oid]=rows[0]
    cutrows=r.read(o,'office.receipt_cut_query','SELECT COALESCE(MAX(seq),0) AS seq FROM receipts')
    observed_cut=cutrows[0]['seq'] if cutrows else 0
    r.heads.pop('office.receipt_cut_query',None)
    cut=observed_cut if record is None else record['receipt_seq_cut']
    if type(cut) is int and cut>observed_cut:raise ValueError('receipt_cut_unobserved')
    if type(cut) is not int or cut<0:raise ValueError('receipt_seq_cut')
    data['receipt_seq_cut']=cut;r.heads['office.receipts_cut']={'seq':cut}
    rows=r.read(o,'office.epoch.end','SELECT * FROM receipts WHERE cache_key=? AND seq<=? '
        "AND json_extract(payload_json,'$.known_at')<=? ORDER BY seq",('epoch.end:'+epoch_id,cut,now))
    data['receipts']=[(_receipt(row),row['row_hash']) for row in rows]
    data['safety']=r.read(l,'ledger.epoch_entry_safety.current','SELECT * FROM epoch_entry_safety WHERE epoch_id=?',
                           (epoch_id,),required=False)
    current=_safety_snapshot(data['safety'][0] if data['safety'] else None)
    data['safety_snapshot']=record['safety_snapshot'] if record is not None else current
    r.heads['ledger.safety_projection']={'snapshot':data['safety_snapshot']}
    data['tickets']=[];data['refused_unknown']=[];data['rounds']=[];data['calls']=[]
    data['call_retention']=[];data['heartbeats']=[];data['memos']={};data['decision_receipts']={}
    data['order_sheets']={};data['order_feed']={};data['coin_core']={};data['own_sheets']={}
    data['basis_aggregates']=[]
    data['list_snapshots']=[];data['ticket_aggregates']=[];data['sheet_aggregates']=[];data['decision_aggregates']=[]
    return data


def _optional_receipts(r,o,keys,cut,now,label):
    out=[]
    keys=sorted(keys)
    for start in range(0,len(keys),200):
        chunk=keys[start:start+200];marks=','.join('?' for _ in chunk)
        rows=r.read(o,label+':'+str(start),'SELECT * FROM receipts WHERE cache_key IN ('+marks+") AND seq<=? AND json_extract(payload_json,'$.known_at')<=? ORDER BY seq",(*chunk,cut,now),required=False)
        out.extend((_receipt(row),row['row_hash']) for row in rows)
    return out


def _attribution(r,data,d,epoch_id,now):
    """Only the order's own verified immutable bundle/sheet/ticket; never borrow sealed counts."""
    o=data['o'];cut=data['receipt_seq_cut']
    for order in data['orders']:
        if time.monotonic()>r.optional_until:
            r.optional.append('optional.order_attribution_budget');break
        r.check();oid=order['order_id']
        bundles=r.read(o,'office.order_bundle:'+oid,'SELECT * FROM decision_bundles WHERE row_hash=?',
                       (order['decision_bundle_hash'],),required=False)
        data['order_feed'][oid]='unknown'
        if not bundles:continue
        try:
            bundle=_receipt(bundles[0]);rid=bundle['receipt_row_hash'];data['order_sheets'][oid]=rid
            rows=r.read(o,'office.order_sheet:'+oid,"SELECT * FROM receipts WHERE row_hash=? AND seq<=? "
                "AND json_extract(payload_json,'$.clock_sample.corrected_utc')<=?",(rid,cut,now),required=False)
            if not rows:continue
            sheet=_receipt(rows[0])
            verified=SH._verify(rows[0],bundle['cache_key'],sheet.get('definition_bundle_hash'),data['cohort'].get('id'))
            req=sheet.get('request',{})
            if (verified.get('state')!='known' or any(req.get(k)!=order[k] for k in ('network_id','mint','pool')) or
                sheet.get('lenses_hash')!=bundle.get('lenses_hash') or sheet.get('kind')!='sheet' or sheet.get('state')!='known' or
                sheet.get('sheet_hash')!=bundle['sheet_hash'] or digest(sheet['sheet'])!=sheet['sheet_hash'] or
                digest(sheet['lenses'])!=sheet['lenses_hash'] or digest(sheet['request'])!=sheet['cache_key']):
                raise ValueError('order_sheet_integrity')
            data['own_sheets'][rid]=sheet;data['order_feed'][oid]=_feed(sheet)
            if d is None:continue
            req=sheet['request']
            tickets=r.read(d,'desk.order_ticket:'+oid,'SELECT * FROM desk_tickets INDEXED BY idx_desk_tickets_coin '
                'WHERE network_id=? AND mint=? AND pool=? AND stage=? AND cohort_id=? AND ts<=? ORDER BY ts',
                (order['network_id'],order['mint'],order['pool'],'hard_filter',data['cohort'].get('id'),
                 datetime.fromtimestamp(now,timezone.utc).isoformat()),required=False)
            matched=[]
            for ticket in tickets:
                body=_object(ticket['inputs_json'])
                if digest(body)!=ticket['inputs_hash']:raise ValueError('order_ticket_integrity')
                if body.get('emission_candidate',{}).get('snapshot',{}).get('hash')==req.get('judged_snapshot_hash'):matched.append(body)
            if len(matched)==1:
                checks=matched[0].get('evidence',{}).get('checks',{})
                dg=checks.get('dump_group',{}).get('status')!='pass';cl=checks.get('cluster',{}).get('status')!='pass'
                data['coin_core'][(order['network_id'],order['mint'])]='both_unknown' if dg and cl else 'unknown_dump_group' if dg else 'unknown_cluster' if cl else 'all_known'
        except (ValueError,TypeError,KeyError):
            r.optional.append('office.order_attribution:'+oid)
            data['order_feed'][oid]='unknown'


def _descriptive(r,data,d,epoch_id,now):
    o=data['o'];cut=data['receipt_seq_cut'];cohort=data['cohort'].get('id')
    # Am13: exact same-turn memo identity. These private-connection UDFs only hash
    # a bounded host id / read an already-loaded dict; no SQL, I/O or inference.
    o.create_function('sniper_eval_memo_key',1,lambda turn:action_key(turn,'memo.commit')
        if isinstance(turn,str) and 0<len(turn)<=256 else None,deterministic=True)
    basis_order_feeds=dict(data['order_feed'])
    o.create_function('sniper_eval_order_feed',1,lambda oid:basis_order_feeds.get(oid,'unknown'),deterministic=True)
    data['basis_aggregates']=r.read(o,'optional.decide_basis',
        "SELECT json_extract(j.payload_json,'$.slot') AS seat,"
        "CASE WHEN json_extract(j.payload_json,'$.decision')='EXIT' THEN sniper_eval_order_feed(json_extract(j.payload_json,'$.order_id')) "
        "WHEN COALESCE(json_extract(s.payload_json,'$.request.feed'),'pass') IN ('pass','near_pass') "
        "AND json_extract(s.payload_json,'$.kind')='sheet' AND json_extract(s.payload_json,'$.state')='known' "
        "AND json_extract(s.payload_json,'$.clock_sample.corrected_utc')<=? "
        "AND json_extract(s.payload_json,'$.strategy_epoch')=? THEN COALESCE(json_extract(s.payload_json,'$.request.feed'),'pass') ELSE 'unknown' END AS feed,"
        "CASE WHEN json_extract(m.payload_json,'$.actor_id')=json_extract(j.payload_json,'$.actor_id') "
        "AND json_extract(m.payload_json,'$.decision_id')=json_extract(j.payload_json,'$.turn_decision_id') "
        "AND json_type(m.payload_json,'$.known_at') IN ('integer','real') AND json_extract(m.payload_json,'$.known_at')>=0 "
        "AND json_extract(m.payload_json,'$.known_at')<=? AND json_type(m.payload_json,'$.request.served_model')='text' "
        "AND length(trim(json_extract(m.payload_json,'$.request.served_model')))>0 "
        "AND json_extract(m.payload_json,'$.request.served_model_basis') IN ('echoed','requested_pinned') "
        "THEN json_extract(m.payload_json,'$.request.served_model_basis') ELSE 'unknown' END AS basis,COUNT(*) AS n "
        "FROM receipts j INDEXED BY receipts_judge_decide_seq "
        "LEFT JOIN messages m ON m.id=sniper_eval_memo_key(json_extract(j.payload_json,'$.turn_decision_id')) "
        "LEFT JOIN receipts s ON s.row_hash=json_extract(j.payload_json,'$.candidate') AND s.seq<=? "
        "WHERE json_extract(j.payload_json,'$.kind')='judge.decide' AND j.seq<=? "
        "AND json_extract(j.payload_json,'$.known_at')<=? AND json_extract(j.payload_json,'$.epoch_id')=? "
        "AND json_extract(j.payload_json,'$.slot') IN ('judge_claude','judge_gpt') GROUP BY seat,feed,basis",
        (now,cohort,now,cut,cut,now,epoch_id),required=False)
    # Prefix range uses the existing cache-key index. Legacy unscoped markers
    # remain unattributed; timestamps never manufacture epoch or coin identity.
    data['sheet_failed_aggregates']=r.read(o,'optional.sheet_failed_aggregates',
        "SELECT json_extract(payload_json,'$.epoch_id') AS epoch_id,"
        "json_extract(payload_json,'$.feed') AS feed,json_extract(payload_json,'$.reason') AS reason,"
        "CASE WHEN json_type(payload_json,'$.reason_detail')='object' "
        "AND length(json_extract(payload_json,'$.reason_detail'))<=1024 "
        "THEN json_extract(payload_json,'$.reason_detail') ELSE NULL END AS detail_json,COUNT(*) AS n "
        "FROM receipts INDEXED BY receipts_cache_key_seq WHERE cache_key>=? AND cache_key<? AND seq<=? "
        "AND json_extract(payload_json,'$.kind')='judge.round.sheet_failed' "
        "AND json_type(payload_json,'$.known_at') IN ('integer','real') "
        "AND json_extract(payload_json,'$.known_at')>=? AND json_extract(payload_json,'$.known_at')<=? "
        "AND (json_extract(payload_json,'$.epoch_id')=? OR json_extract(payload_json,'$.epoch_id') IS NULL) "
        "GROUP BY epoch_id,feed,reason,detail_json",('judge.round.sheet_failed:','judge.round.sheet_failed;',cut,
        data['epoch']['opened_at'],now,epoch_id),required=False)
    # Aggregates read indexed source paths without fetching the large sheet/ticket bodies.
    data['sheet_aggregates']=r.read(o,'optional.sheet_aggregates',
        "SELECT COALESCE(json_extract(payload_json,'$.request.feed'),'pass') AS feed,json_extract(payload_json,'$.sheet.pool_state.virtual_quote_source') AS source,"
        "COUNT(*) AS n,COUNT(DISTINCT json_array(json_extract(payload_json,'$.request.network_id'),json_extract(payload_json,'$.request.mint'))) AS coins "
        "FROM receipts INDEXED BY receipts_sheet_at WHERE json_extract(payload_json,'$.kind')='sheet' AND json_extract(payload_json,'$.state')='known' AND seq<=? "
        "AND json_extract(payload_json,'$.clock_sample.corrected_utc')<=? AND json_extract(payload_json,'$.strategy_epoch')=? GROUP BY feed,source",
        (cut,now,cohort),required=False)
    data['decision_aggregates']=r.read(o,'optional.decision_aggregates',
        "WITH firsts AS (SELECT MIN(seq) AS seq FROM receipts INDEXED BY receipts_judge_decide_seq "
        "WHERE json_extract(payload_json,'$.kind')='judge.decide' AND seq<=? AND json_extract(payload_json,'$.known_at')<=? "
        "AND json_extract(payload_json,'$.epoch_id')=? AND json_extract(payload_json,'$.slot') IN ('judge_claude','judge_gpt') "
        "GROUP BY json_extract(payload_json,'$.decision_id'),json_extract(payload_json,'$.slot')), "
        "pairs AS (SELECT json_extract(r.payload_json,'$.decision_id') AS did,COUNT(*) AS n,"
        "SUM(json_extract(r.payload_json,'$.decision')='EXIT') AS exits,MIN(json_extract(r.payload_json,'$.decision')) AS lo,"
        "MAX(json_extract(r.payload_json,'$.decision')) AS hi FROM receipts r JOIN firsts f ON r.seq=f.seq GROUP BY did) "
        "SELECT CASE WHEN exits>0 THEN 'EXIT' WHEN n<2 THEN 'unpublished' WHEN lo=hi THEN lo ELSE 'split' END AS label,"
        "COUNT(*) AS n FROM pairs GROUP BY label",(cut,now,epoch_id),required=False)
    judges=r.read(o,'optional.judge_receipts',"SELECT * FROM receipts INDEXED BY receipts_judge_decide_seq "
        "WHERE json_extract(payload_json,'$.kind')='judge.decide' AND seq<=? AND json_extract(payload_json,'$.known_at')<=? "
        "AND json_extract(payload_json,'$.epoch_id')=? ORDER BY seq",(cut,now,epoch_id),required=False)
    data['receipts'].extend((_receipt(row),row['row_hash']) for row in judges)
    # Bounded metadata projection: explicitly not a body-verification claim.
    sheets=r.read(o,'optional.sheet_metadata',
        "SELECT seq,row_hash,json_extract(payload_json,'$.request') AS request_json,json_extract(payload_json,'$.strategy_epoch') AS strategy_epoch,"
        "json_extract(payload_json,'$.clock_sample.corrected_utc') AS at,json_extract(payload_json,'$.sheet.pool_state.virtual_quote_source') AS source,"
        "json_extract(payload_json,'$.sheet.creator_history') AS creator_json FROM receipts INDEXED BY receipts_sheet_at "
        "WHERE json_extract(payload_json,'$.kind')='sheet' AND json_extract(payload_json,'$.state')='known' AND seq<=? "
        "AND json_extract(payload_json,'$.clock_sample.corrected_utc')<=? AND json_extract(payload_json,'$.strategy_epoch')=? ORDER BY seq",
        (cut,now,cohort),required=False)
    for row in sheets:
        sheet=data['own_sheets'].get(row['row_hash'])
        if sheet is None:
            sheet={'kind':'sheet','state':'known','strategy_epoch':row['strategy_epoch'],'request':_object(row['request_json']),
                'clock_sample':{'corrected_utc':row['at']},'sheet':{'pool_state':{'virtual_quote_source':row['source']},
                'creator_history':_object(row['creator_json']) if row['creator_json'] else {}},'_not_body_verified':True}
        data['receipts'].append((sheet,row['row_hash']))
    tools=r.read(o,'optional.broker_receipts',"SELECT * FROM receipts INDEXED BY receipts_near_pass_tool WHERE "
        "json_extract(payload_json,'$.tool') IN ('desk.near_pass_list','desk.near_pass_sheet') AND seq<=? "
        "AND json_extract(payload_json,'$.cut.receipt_cut')<=? ORDER BY seq",(cut,now),required=False)
    data['receipts'].extend((_receipt(row),row['row_hash']) for row in tools)
    rounds=r.read(o,'optional.rounds',"SELECT * FROM receipts WHERE cache_key=? AND seq<=? "
        "AND json_extract(payload_json,'$.known_at')<=? AND json_extract(payload_json,'$.epoch_id')=? ORDER BY seq",
        ('judge.round:latest',cut,now,epoch_id),required=False)
    data['rounds']=[_receipt(row) for row in rounds]
    keys=set()
    for p,_ in data['receipts']:
        if p.get('kind')=='judge.decide':
            keys.add('judge.settle:'+p['decision_id'])
            if p.get('decision')=='EXIT':keys.add('judge.settle:'+p['decision_id']+':'+p['slot'])
    for p in data['rounds']:keys.update(('judge.round.assigned:'+p['candidate'],'judge.round.end:'+p['round_id']))
    data['receipts'].extend(_optional_receipts(r,o,keys,cut,now,'optional.linked_receipts'))
    pairs=defaultdict(list)
    for p,_ in data['receipts']:
        if p.get('kind')=='judge.decide':pairs[p['decision_id']].append(p)
    memo_keys=set()
    for rows in pairs.values():
        pub=J.published(rows)
        if pub and pub['decision']=='WAIT':
            memo_keys.update(action_key(p['turn_decision_id'],'memo.commit') for p in pub['rows'])
    memo_keys=sorted(memo_keys)
    for start in range(0,len(memo_keys),200):
        chunk=memo_keys[start:start+200];marks=','.join('?' for _ in chunk)
        rows=r.read(o,'optional.memos:'+str(start),'SELECT id,payload_json FROM messages WHERE id IN ('+marks+')',chunk,required=False)
        data['memos'].update((row['id'],_object(row['payload_json'])) for row in rows)
    rows=r.read(data['l'],'optional.decision_receipts','SELECT * FROM decision_receipts WHERE book_id=? AND created_at<=?',
                (data['book'],now),required=False)
    data['decision_receipts']={row['action_key']:row for row in rows}
    data['calls']=r.read(o,'optional.model_calls','SELECT call_id,started_at,ended_at,outcome,epoch_id FROM model_calls '
        'INDEXED BY model_calls_ended_at_call_id WHERE ended_at<=? AND epoch_id=? ORDER BY ended_at,call_id',(now,epoch_id),required=False)
    data['call_retention']=r.read(o,'optional.call_retention','SELECT table_name,retained_from,pruned_n,dropped_n '
        'FROM runtime_observation_state WHERE table_name=?',('model_calls',),required=False)
    data['heartbeats']=r.read(o,'optional.heartbeats',"SELECT * FROM heartbeats WHERE id='supervisor'",required=False)
    if d is None or cohort is None:r.optional.append('desk.desk_tickets');return
    policies=r.read(d,'optional.desk_cohort','SELECT * FROM desk_cohorts WHERE cohort_id=?',(cohort,),required=False)
    if not policies or digest(_object(policies[0]['policy_json']))!=data['cohort'].get('policy_hash'):
        r.optional.append('desk.cohort_integrity');return
    args=('hard_filter',datetime.fromtimestamp(data['epoch']['opened_at'],timezone.utc).isoformat(),
          datetime.fromtimestamp(now,timezone.utc).isoformat(),cohort)
    data['ticket_aggregates']=r.read(d,'optional.ticket_aggregates',
        "SELECT verdict,CASE WHEN reason LIKE 'unknown:%' THEN 1 ELSE 0 END AS refused_unknown,"
        "CASE WHEN json_extract(inputs_json,'$.evidence.checks.dump_group.status')='pass' THEN 0 ELSE 1 END AS dg,"
        "CASE WHEN json_extract(inputs_json,'$.evidence.checks.cluster.status')='pass' THEN 0 ELSE 1 END AS cluster,"
        "COUNT(*) AS n,COUNT(DISTINCT json_array(network_id,mint)) AS coins FROM desk_tickets INDEXED BY idx_desk_tickets_stage_ts "
        'WHERE stage=? AND ts>=? AND ts<=? AND cohort_id=? GROUP BY verdict,refused_unknown,dg,cluster',args,required=False)
    tickets=r.read(d,'optional.desk_tickets','SELECT * FROM desk_tickets INDEXED BY idx_desk_tickets_stage_ts '
        'WHERE stage=? AND ts>=? AND ts<=? AND cohort_id=? ORDER BY ts,id',args,required=False)
    for ticket in tickets:
        if ticket['verdict']!='PASS' and not (ticket['verdict']=='SKIP' and ticket['reason'].startswith('unknown:')):continue
        body=_object(ticket['inputs_json'])
        if digest(body)!=ticket['inputs_hash']:r.optional.append('desk.ticket_integrity');continue
        (data['tickets'] if ticket['verdict']=='PASS' else data['refused_unknown']).append((ticket,body))

def _summary(r, data, epoch_id, now):
    orders=data['orders']; epoch=data['epoch']
    filled=[x for x in orders if x['filled_at'] is not None and x['filled_at']<=now]
    opened=[x for x in filled if x['closed_at'] is None or x['closed_at']>now]
    coins={(x['network_id'],x['mint']) for x in filled}
    first=min((x['requested_at'] for x in orders),default=None)
    age=0 if first is None else now-first
    ceiling=age>=28*86400
    eligibility={'eligible_as_of':bool(first is not None and (ceiling or (len(coins)>=30 and not opened and age>=14*86400))),
                 'filled_coins':len(coins),'open_positions':len(opened),'first_order_at':first,
                 'ceiling_reached':ceiling,'underpowered':len(coins)<30}
    settlement_map=defaultdict(list)
    for i,(receipt,_) in enumerate(data['receipts']):
        if i%128==0:r.check()
        if receipt.get('kind')=='judge.settle':settlement_map[receipt.get('decision_id')].append(receipt)
    order_bundles={order['decision_bundle_hash'] for order in orders}
    labels=Counter(); wait=Counter(); published=[]; grouped=defaultdict(list); sheets=dict(data['own_sheets']); attempts=Counter()
    end=None
    for p,h in data['receipts']:
        if p.get('kind')=='judge.decide' and p.get('epoch_id')==epoch_id:
            if p.get('slot') not in J.JUDGE_SLOTS or p.get('decision') not in J.DECISIONS:
                raise ValueError('judge_shape')
            args={k:p[k] for k in ('decision','candidate','order_id','public_reason')}
            J.validate(args,candidates={p['candidate']},open_orders={p['order_id']})
            if J.decision_id(epoch_id,args)!=p['decision_id']:raise ValueError('judge_identity')
            grouped[p['decision_id']].append(p)
        elif p.get('kind')=='sheet' and p.get('state')=='known' and p.get('strategy_epoch')==data['cohort'].get('id'):
            if not p.get('_not_body_verified') and (p.get('contract')!=SH.OFFICE_SHEET_CONTRACT['version'] or digest(p['request'])!=p.get('cache_key')):
                raise ValueError('sheet_identity')
            if not p.get('_not_body_verified') and (digest(p['sheet'])!=p.get('sheet_hash') or digest(p['lenses'])!=p.get('lenses_hash')):
                raise ValueError('sheet_integrity')
            sheets[h]=p
        elif p.get('tool')=='desk.near_pass_sheet' and p.get('result',{}).get('state')=='unknown':
            attempts[str(p['result'].get('reason','unknown'))]+=1
        elif p.get('kind')=='epoch.end' and p.get('epoch_id')==epoch_id: end=p
    price={key:Counter() for key in ('tape','exact','unknown')}
    not_ordered=Counter()
    for did,rows in grouped.items():
        pub=J.published(rows)
        if pub is None: continue
        label=pub['decision']; labels[label]+=1; published.append(pub)
        candidate=pub['rows'][0].get('candidate'); sheet=sheets.get(candidate)
        source=None if sheet is None else sheet.get('sheet',{}).get('pool_state',{}).get('virtual_quote_source')
        source='exact' if source=='pumpswap_rpc:pool_state' else 'tape' if source in ('tape_tip','ps_tape','pumpswap_grpc:buy_event_virtual_quote') else 'unknown'
        price[source][label]+=1
        if label=='WAIT':
            blockers=[]
            for row in pub['rows']:
                memo=data['memos'].get(action_key(row['turn_decision_id'],'memo.commit'),{})
                text=memo.get('request',{}).get('memo','')
                match=re.search(r'\bblocker=(missing_input|policy|merit)\b',text)
                blockers.append(match.group(1) if match else 'unknown')
            wait[blockers[0] if len(set(blockers))==1 else 'unknown']+=1
        if label=='ENTER':
            settled=settlement_map.get(did,[])
            result=settled[0].get('result',{}) if settled else {}
            # A ledger order is proof of an order even if the settle receipt was lost.
            key=digest([epoch_id,data['book'],did])
            ledger_receipt=[data['decision_receipts'][key]] if key in data['decision_receipts'] else []
            if not result.get('order_id') and not order_bundles.intersection(v.get('bundle_hash') for v in ledger_receipt):
                reason=(ledger_receipt[0].get('reason') if ledger_receipt else result.get('reason'))
                not_ordered[reason or 'settlement_receipt_absent']+=1
    core={k:{'admitted_coins':0} for k in ('all_known','unknown_dump_group','unknown_cluster','both_unknown')}
    listed_ids={item['ticket_id'] for p,_ in data['receipts'] if p.get('tool')=='desk.near_pass_list'
        and p.get('result',{}).get('state')=='known' for item in p['result'].get('items',[])
        if isinstance(item,dict) and isinstance(item.get('ticket_id'),str)}
    seen=set(); sheetcoins=set(); hours=defaultdict(lambda:{'admitted_tickets':0,'seen_tickets':0})
    corecoins=defaultdict(set)
    sheet_index=defaultdict(list)
    for sh in sheets.values():
        r.check()
        req=sh.get('request',{})
        sheet_index[(req.get('network_id'),req.get('mint'),req.get('pool'),req.get('judged_snapshot_hash'))].append(sh)
    for p in sheets.values():
        req=p.get('request',{});sheetcoins.add((req.get('network_id'),req.get('mint')))
    for ticket,inputs in data['tickets']:
        checks=inputs.get('evidence',{}).get('checks',{})
        dg=checks.get('dump_group',{}).get('status')!='pass';cl=checks.get('cluster',{}).get('status')!='pass'
        k='both_unknown' if dg and cl else 'unknown_dump_group' if dg else 'unknown_cluster' if cl else 'all_known'
        corecoins[k].add((ticket['network_id'],ticket['mint']))
        emission=inputs.get('emission_candidate',{}); snapshot=emission.get('snapshot',{}).get('hash')
        r.check()
        observed=ticket['ticket_id'] in listed_ids or bool(sheet_index.get((ticket['network_id'],ticket['mint'],ticket['pool'],snapshot)))
        if observed:seen.add(ticket['ticket_id'])
        hour=ticket['ts'][:13]+':00:00+00:00';hours[hour]['admitted_tickets']+=1
        if observed:hours[hour]['seen_tickets']+=1
    for k in core:core[k]['admitted_coins']=len(corecoins[k])
    # Link strata through the exact stored snapshot identity, never timestamp proximity.
    coin_core={}
    for ticket,inputs in data['tickets']:
        checks=inputs.get('evidence',{}).get('checks',{})
        dg=checks.get('dump_group',{}).get('status')!='pass';cl=checks.get('cluster',{}).get('status')!='pass'
        key='both_unknown' if dg and cl else 'unknown_dump_group' if dg else 'unknown_cluster' if cl else 'all_known'
        coin=(ticket['network_id'],ticket['mint'])
        coin_core.setdefault(coin,set()).add(key)
    data['coin_core'].update({coin:next(iter(keys)) if len(keys)==1 else 'unattributed' for coin,keys in coin_core.items()})
    core['unattributed']={'admitted_coins':sum(len(keys)!=1 for keys in coin_core.values())}
    for row in core.values():row.update(ENTER=0,WAIT=0,SKIP=0,split=0,filled=0,censored=0)
    for pub in published:
        sheet=sheets.get(pub['rows'][0].get('candidate'));req={} if sheet is None else sheet.get('request',{})
        key=data['coin_core'].get((req.get('network_id'),req.get('mint')),'unattributed')
        label=pub['decision']
        if label in ('ENTER','WAIT','SKIP','split'):core[key][label]+=1
    for order in orders:
        key=data['coin_core'].get((order['network_id'],order['mint']),'unattributed')
        if order['filled_at'] is not None and order['filled_at']<=now:core[key]['filled']+=1
        if order['state']=='censored':core[key]['censored']+=1
    by_day=defaultdict(lambda:Counter())
    for ticket,inputs in data['tickets']:
        c=inputs.get('evidence',{}).get('checks',{});day=ticket['ts'][:10]
        by_day[day]['admitted']+=1
        for key in ('dump_group','cluster'):
            if c.get(key,{}).get('status')!='pass':by_day[day][key]+=1
    degraded=[{'day':day,'fact_source_degraded':any(c[k]>c['admitted']/2 for k in ('dump_group','cluster')),
               'admitted_tickets':c['admitted'],'unknown_dump_group':c['dump_group'],'unknown_cluster':c['cluster']}
              for day,c in sorted(by_day.items())]
    data['sheets']=sheets
    data['published']=published
    creator=Counter()
    for ticket,inputs in data['tickets']:
        r.check()
        matches=sheet_index.get((ticket['network_id'],ticket['mint'],ticket['pool'],inputs.get('emission_candidate',{}).get('snapshot',{}).get('hash')),[])
        if not matches:creator['not_observed_by_office']+=1;continue
        state=matches[-1].get('sheet',{}).get('creator_history',{})
        creator['known' if state.get('state')=='known' else str(state.get('reason','unknown'))]+=1
    causes=Counter(x.get('exit_by') or 'unknown_cause' for x in filled if x['closed_at'] is not None and x['closed_at']<=now)
    exit_refused=Counter(p.get('result',{}).get('reason','unknown') for p,_ in data['receipts']
        if p.get('kind')=='judge.settle' and p.get('label')=='EXIT' and p.get('result',{}).get('state')=='refused')
    asker_counts=Counter();slots_by_order=defaultdict(set)
    for row in data['askers']:slots_by_order[row['order_id']].add(row['slot'])
    for slots in slots_by_order.values():asker_counts['one_judge' if len(slots)==1 else 'two_judges']+=1
    gaps=[];calls=sorted(data['calls'],key=lambda x:x['started_at'])
    through=epoch.get('opened_at',now)
    for c in calls:
        if c['started_at']-through>120:gaps.append({'from_at':through,'to_at':c['started_at'],'basis':'recorded_call_silence'})
        through=max(through,c['ended_at'])
    if now-through>120:gaps.append({'from_at':through,'to_at':now,'basis':'recorded_call_silence'})
    retained=data['call_retention'][0] if data['call_retention'] else {}
    call_history_complete=(bool(retained) and not retained.get('pruned_n') and not retained.get('dropped_n')
        and 'optional.model_calls' not in r.optional)
    if 'optional.model_calls' in r.optional:gaps=[]
    safety={k:v for k,v in (L._public_safety(data['safety'][0]) if data['safety'] else
        {'state':'unknown','reason':'epoch_safety_unavailable'}).items() if k in
        {'state','reason','halt_kind','halted_at','unpriced_count','unpriced_order_ids','demoted_order_ids',
         'demoted_seconds','demoted_never_priced_order_ids','repromotions','last_at'}}
    if end is not None:status=end.get('status','unknown')
    elif safety.get('halted_at') is not None:status='BROKEN' if safety.get('halt_kind')=='config_drift' else 'FAILED'
    else:status='open'
    feeds,unlisted=_feeds(data,published,sheets,now,safety)
    try:rounds=_round_summary(data,sheets,grouped,now)
    except (ValueError,KeyError,TypeError):
        r.optional.append('optional.round_details');rounds={'state':'unknown','reason':'round_inputs_incomplete'}
    for ticket,_ in data['refused_unknown']:
        hour=ticket['ts'][:13]+':00:00+00:00';hours[hour]['refused_unknown_tickets']=hours[hour].get('refused_unknown_tickets',0)+1
    for counts in hours.values():counts.setdefault('refused_unknown_tickets',0)
    complete=not r.missing and not r.optional
    return {'version':'office-eval-report-v1','clock_contract':{'clock_skew_tolerance_s':0.125,'leg_stale_demote_s':1800,
            'basis':'configured; 1075 supervisor offset samples vs one retained watcher sample; p99 0.0168s, max 0.0919s; not paired dispatch jitter; demotion unmeasured'},
        'mode':'health','epoch_id':epoch_id,'as_of':now,'epoch_status':status,
        'state':'known' if complete else 'partial','operator_waived_gates':epoch.get('operator_waived_gates'),
        'orders':{'filled_coins':len(coins),'open_positions':len(opened),'post_halt_rows':sum(bool(x.get('post_halt')) for x in orders),
            'censored_unfilled':None if any(x['filled_at'] is None for x in orders) else 0,
            'unfilled_terminal_state':'unknown; censor timestamp not persisted' if any(x['filled_at'] is None for x in orders) else 'known',
            'halt_provenance':[{'order_id':x['order_id'],'halted_at':x.get('epoch_halted_at')} for x in orders if x.get('post_halt')]},
        'eligibility':eligibility,'economics':{'state':'withheld','reason':'health_mode'},'valuation':safety,
        'admissions':{'state':'unknown' if any('desk' in x for x in r.optional) else 'known',
            'denominator':DENOMINATOR,'admitted_tickets':None if any('desk' in x for x in r.optional) else len(data['tickets']),
            'refused_unknown_tickets':None if any('desk' in x for x in r.optional) else len(data['refused_unknown']),
            'pass_never_listed':{'total':sum(unlisted.values()),'by_reason':unlisted},
            'pass_unlisted_snapshots':data['list_snapshots']},
        'seen':{'sheet_coins':len(sheetcoins),'seen_tickets':len(seen),'judged_targets':len(published)},
        'feeds':feeds,'rounds':rounds,'aggregate_scope':'whole_book_counts; use feed tables for candidate comparisons',
        'strata':{'core_list':core,'price_source':{**{k:dict(v) for k,v in price.items()},
            'unpriced_attempts':{'by_reason':dict(attempts),'coin_state':'coins_unknown','distinct_coins':None}}},
        'decisions':{'published_targets':len(published),'by_label':dict(labels),'wait_by_blocker':dict(wait)},
        'enter_not_ordered':{'by_reason':dict(not_ordered)},
        'exits':{'state':'unknown' if 'ledger.exit_askers' in r.missing else 'known','by_cause':dict(causes),
                 'refused_by_reason':dict(exit_refused),'asker_count_split':{'one_judge':asker_counts['one_judge'],'two_judges':asker_counts['two_judges']}},
        'coverage':{'complete':complete,'required_complete':not r.missing,'required_missing':sorted(set(r.missing)),'optional_missing':sorted(set(r.optional)),
            'by_hour':[dict(admission_hour=k,**v) for k,v in sorted(hours.items())],
            'fact_source_days':degraded,'creator_history_by_reason':dict(creator),
            'planned_intervals':{'state':'unknown','reason':'no_authoritative_planned_interval_history'},
            'call_silent_intervals':gaps,
            'call_history':{'state':'known' if call_history_complete else 'unknown','retention':retained,
                'basis':'recorded call silence; not independently verified service downtime'},'heartbeat_history':{'state':'unknown','reason':'latest_snapshot_only'},
            'daily_series':{'state':'unknown','reason':'history_not_recorded'},'gap_policy':'reported_not_censored',
            'outage_failure_condition_observed':any(x['to_at']-x['from_at']>86400 for x in gaps) if call_history_complete else None}}


def _feed(sheet):
    if (not isinstance(sheet,dict) or sheet.get('kind')!='sheet' or sheet.get('state')!='known'
        or not isinstance(sheet.get('request'),dict)):return 'unknown'
    feed=sheet['request'].get('feed','pass')      # SH.sheet writes request.feed only for near_pass; absent is the pass feed
    return feed if feed in ('pass','near_pass') else 'unknown'


def _feeds(data,published,sheets,now,safety):
    """Only the verified sheet request attributes a candidate/order to a feed."""
    feeds={f:{'feed':f,'source_feed_values':[], 'decisions':{},'filled':0,'closes':0,'post_halt_rows':0,
              'listed_tickets':0,'sheet_coins':0,'judged_targets':0,
              'core_list':{},'price_source':{},'exit_by':{},'valuation':{'unpriced_count':0,'demoted_count':0}}
           for f in ('pass','near_pass','unknown')}
    coin_sets=defaultdict(set)
    for sh in sheets.values():
        f=_feed(sh);req=sh['request'];coin_sets[f].add((req.get('network_id'),req.get('mint')))
        raw=req.get('feed');raw='missing' if raw is None else str(raw)
        if raw not in feeds[f]['source_feed_values']:feeds[f]['source_feed_values'].append(raw)
    for f in feeds:feeds[f]['sheet_coins']=len(coin_sets[f]);feeds[f]['source_feed_values'].sort()
    ticket_sheets=defaultdict(list)
    for sh in sheets.values():
        tid=sh.get('request',{}).get('ticket_id')
        if tid:ticket_sheets[tid].append(sh)
    order_feeds={o['order_id']:_feed(sheets.get(data['order_sheets'].get(o['order_id']))) for o in data['orders']}
    for pub in published:
        sh=sheets.get(pub['rows'][0].get('candidate'));f=_feed(sh)
        if pub['decision']=='EXIT':f=order_feeds.get(pub['rows'][0].get('order_id'),'unknown')
        row=feeds[f]
        label=pub['decision'];row['decisions'][label]=row['decisions'].get(label,0)+1;row['judged_targets']+=1
        req=(sh or {}).get('request',{});k=data['coin_core'].get((req.get('network_id'),req.get('mint')),'unattributed')
        core=row['core_list'].setdefault(k,{})
        core[label]=core.get(label,0)+1
        source=(sh or {}).get('sheet',{}).get('pool_state',{}).get('virtual_quote_source')
        source='exact' if source=='pumpswap_rpc:pool_state' else 'tape' if source in ('tape_tip','ps_tape','pumpswap_grpc:buy_event_virtual_quote') else 'unknown'
        counts=row['price_source'].setdefault(source,{})
        counts[label]=counts.get(label,0)+1
    data['order_feed']={}
    unpriced_ids=set(safety.get('unpriced_order_ids') or []);demoted_ids=set(safety.get('demoted_order_ids') or [])
    for order in data['orders']:
        oid=order['order_id'];f=_feed(sheets.get(data['order_sheets'].get(oid)));data['order_feed'][oid]=f
        row=feeds[f]
        if order['filled_at'] is not None and order['filled_at']<=now:row['filled']+=1
        if order['closed_at'] is not None and order['closed_at']<=now:
            row['closes']+=1;cause=order.get('exit_by') or 'unknown_cause'
            row['exit_by'][cause]=row['exit_by'].get(cause,0)+1
        if order.get('post_halt'):row['post_halt_rows']+=1
        if oid in unpriced_ids:row['valuation']['unpriced_count']+=1
        if oid in demoted_ids:row['valuation']['demoted_count']+=1
    listed={}
    data['list_snapshots']=[]
    for p,_ in data['receipts']:
        if p.get('tool')!='desk.near_pass_list' or p.get('result',{}).get('state')!='known':continue
        result=p.get('result',{})
        omission=result.get('pass_unlisted')
        if isinstance(omission,dict):
            data['list_snapshots'].append({'at':p.get('cut',{}).get('receipt_cut'),
                'item_limit':omission.get('item_limit'),'age_bound':omission.get('age_bound'),
                'scope':'inside_host_200_row_window; not distinct tickets over epoch'})
        for item in p.get('result',{}).get('items',[]):
            if isinstance(item,dict) and isinstance(item.get('ticket_id'),str):listed[item['ticket_id']]=item
    unlisted=Counter()
    for ticket,inputs in data['tickets']:
        if ticket['ticket_id'] not in listed:unlisted['list_evidence_unavailable']+=1
    # Listing alone may not carry feed; no guess from PASS/SKIP is substituted.
    for tid in listed:
        matches=ticket_sheets.get(tid,[])
        raw=listed[tid].get('feed')
        f=_feed(matches[0]) if len(matches)==1 else raw if raw in ('pass','near_pass') else 'unknown'
        feeds[f]['listed_tickets']+=1
    if not isinstance(safety.get('unpriced_order_ids'),list) or not isinstance(safety.get('demoted_order_ids'),list):
        for row in feeds.values():row['valuation']={'state':'unknown','unpriced_count':None,'demoted_count':None}
    else:
        for row in feeds.values():row['valuation']['state']='known'
    return feeds,dict(unlisted)


def _round_rule_ages(data,sheets,grouped):
    """Recorded rule only; Judge age comes from view_cut, never commit time."""
    def age_cell():return {'values':[],'unknown':Counter()}
    def cell():return {'rounds':0,'published_by_label':Counter(),
        'assignment_sheet_age_s':age_cell(),'judge_sheet_age_s_at_cut':{slot:age_cell() for slot in J.JUDGE_SLOTS}}
    def observe(target,value,reason):
        if reason:target['unknown'][reason]+=1
        else:target['values'].append(value)
    def finish_age(raw):
        vals=raw['values'];missing=sum(raw['unknown'].values())
        return {'state':'partial' if vals and missing else 'known' if vals else 'unknown',
            'count':len(vals),'unknown_count':missing,'unknown_by_reason':dict(raw['unknown']),
            'min':min(vals) if vals else None,'max':max(vals) if vals else None,
            'mean':sum(vals)/len(vals) if vals else None}
    rules={};seen=set()
    for row in data['rounds']:
        if row['round_id'] in seen:continue
        seen.add(row['round_id'])
        rule=row.get('rule') if row.get('rule') in ('am11','am14') else 'unknown'
        root=rules.setdefault(rule,dict(cell(),by_feed={f:cell() for f in ('pass','near_pass','unknown')}))
        sheet=sheets.get(row['candidate']);feed=_feed(sheet);targets=(root,root['by_feed'][feed])
        stamp=(sheet or {}).get('clock_sample',{}).get('corrected_utc');cut=row.get('round_cut')
        valid=_number(stamp) and _number(cut) and 0<=stamp<=cut
        age=cut-stamp if valid else None
        reason=None if valid else 'assignment_cut_unknown'
        if valid and 'sheet_age_s' in row and (not _number(row['sheet_age_s']) or abs(row['sheet_age_s']-age)>1e-6):
            reason='assignment_age_conflict'
        did=J.decision_id(row['epoch_id'],{'decision':'ENTER','candidate':row['candidate']})
        first={}
        for answer in grouped.get(did,[]):first.setdefault(answer['slot'],answer)
        pub=J.published(list(first.values())) if first else None
        for target in targets:
            target['rounds']+=1
            if pub:target['published_by_label'][pub['decision']]+=1
            observe(target['assignment_sheet_age_s'],age,reason)
            for slot in J.JUDGE_SLOTS:
                answer=first.get(slot);view=(answer or {}).get('view_cut');commit=(answer or {}).get('known_at')
                valid=(_number(stamp) and _number(view) and _number(commit) and 0<=stamp<=view<=commit)
                observe(target['judge_sheet_age_s_at_cut'][slot],view-stamp if valid else None,
                        None if valid else 'view_cut_unknown' if answer else 'judge_commit_absent')
    def finish(raw):
        return {'rounds':raw['rounds'],'published_by_label':dict(raw['published_by_label']),
            'assignment_sheet_age_s':finish_age(raw['assignment_sheet_age_s']),
            'judge_sheet_age_s_at_cut':{slot:finish_age(raw['judge_sheet_age_s_at_cut'][slot]) for slot in J.JUDGE_SLOTS}}
    return {rule:dict(finish(raw),by_feed={feed:finish(v) for feed,v in raw['by_feed'].items()}) for rule,raw in rules.items()}


def _round_summary(data,sheets,grouped,now):
    """Am11 producer contract 7a2397c87; cache-key reads, no timestamp join."""
    assigned=Counter();incomplete=Counter();latencies=[];assigned_ids=set()
    assignment_by_key=defaultdict(list);ends_by_round=defaultdict(list)
    for p,_ in data['receipts']:
        if p.get('kind')=='judge.round.assigned':assignment_by_key[p.get('cache_key')].append(p)
        elif p.get('kind')=='judge.round.end':ends_by_round[p.get('round_id')].append(p)
    for round_row in data['rounds']:
        candidate=round_row['candidate'];sheet=sheets.get(candidate)
        if sheet is None:raise ValueError('round_sheet_missing')
        feed=_feed(sheet)
        if round_row.get('feed')!=feed:raise ValueError('round_feed_mismatch')
        confirmations=[p for p in assignment_by_key.get('judge.round.assigned:'+candidate,[]) if p.get('round_id')==round_row['round_id']]
        if not confirmations:raise ValueError('round_assignment_missing')
        assigned[feed]+=1;assigned_ids.add(candidate)
        latency=round_row['round_cut']-sheet['clock_sample']['corrected_utc']
        if not _number(latency) or latency<0:raise ValueError('round_clock')
        latencies.append(latency)
        ends=ends_by_round.get(round_row['round_id'],[])
        if ends and ends[0].get('outcome')=='round_incomplete':incomplete[str(ends[0].get('reason') or 'unknown')]+=1
        elif not ends and now-round_row['round_cut']>300:incomplete['end_receipt_missing_after_round_max']+=1
    never=Counter()
    for h,sheet in sheets.items():
        if h in assigned_ids:continue
        # This describes the current exclusion, not an invented historical causal reason.
        reason='as_of_age_bound' if now-sheet['clock_sample']['corrected_utc']>1800 else 'reason_unknown'
        never[reason]+=1
    published_labels=Counter()
    for rows in grouped.values():
        if rows[0].get('candidate') in assigned_ids:
            pub=J.published(rows)
            if pub:published_labels[pub['decision']]+=1
    single=sum(len({r['slot'] for r in rows})==1 for rows in grouped.values() if rows[0].get('candidate') is not None)
    return {'state':'known','rounds':len(data['rounds']),'by_rule':_round_rule_ages(data,sheets,grouped),
        'assigned_by_feed':{f:assigned[f] for f in ('pass','near_pass','unknown')},
        'incomplete_by_reason':dict(incomplete),'single_judge_commits':single,'published_by_label':dict(published_labels),
        'sheeted_never_assigned':dict(never),
        'assignment_latency_s':{'state':'known' if latencies else 'unknown','count':len(latencies),
            'min':min(latencies) if latencies else None,'max':max(latencies) if latencies else None,
            'mean':sum(latencies)/len(latencies) if latencies else None},
        'selection':'newest first, pass before near_pass, one candidate; Scout sheeting is part of the measured Office',
        'unrecorded_omission_reason':'unknown; no causal supersession or never-sheeted claim from time proximity'}


def _legs(data,now,sol_usd,sol_at):
    """Pure valuation of the already budgeted/pinned rows. Never writes a safety sample."""
    legs=[]
    fx_ok=_number(sol_usd) and sol_usd>0 and _number(sol_at) and 0<=now-sol_at<=120
    config=json.loads(data['config'][0]['note'])['paper_eval']
    mark_age=config['mark_max_age_s'];demote=config['leg_stale_demote_s']
    marks_by_order={m['order_id']:m for m in data['marks']}
    for order in data['orders']:
        if order['filled_at'] is None or order['filled_at']>now or (order['closed_at'] is not None and order['closed_at']<=now):continue
        oid=order['order_id'];m=marks_by_order.get(oid)
        cause='known';value=None
        if not fx_ok:cause='book_unknown'
        elif m and m['lamports'] is None and m['reason'] in L.BOOK_UNKNOWN_MARK_REASONS:cause='book_unknown'
        elif m and m['lamports'] is not None and not L._proven_mark(m,order['pool']):cause='book_unknown'
        elif m is None or now-m['at']>mark_age:
            since=order['filled_at'] if m is None else m['at']+mark_age
            cause='demoted_stale' if now-since>=demote else 'leg_pause'
        elif m['lamports'] is None:
            cause='quote_failure' if m['reason'] in L.ZERO_QUOTE_REASONS else 'leg_pause'
            if cause=='leg_pause' and now-m['at']>=demote:cause='demoted_stale'
        else:value=(int(m['lamports'])/L.LAMPORTS-L.EXEC_COST_SOL['exit'])*sol_usd
        if cause=='leg_pause':
            prior=next((x for x in data['leg_safety'] if x['order_id']==oid and x['last_at']<=now),None)
            if prior and _number(prior['pause_since']) and now-prior['pause_since']>=demote:cause='demoted_stale'
        zero=-L.EXEC_COST_SOL['exit']*sol_usd if fx_ok else None
        legs.append({'order_id':oid,'cause':cause,'value_usd':value,'zero_bound_usd':zero})
    return legs


def _last_mark(data,legs,trading,cost,fx):
    orders={o['order_id']:o for o in data['orders']}
    unpriced=[leg for leg in legs if leg['cause']!='known']
    if not unpriced:return {'state':'known','label':'unverified','net_usd':trading-cost}
    change=0
    for leg in unpriced:
        m=data['priced_marks'].get(leg['order_id'])
        order=orders[leg['order_id']]
        if not m or not L._proven_mark(m,order['pool']):
            return {'state':'unknown','label':'unverified','reason':'last_proven_mark_unavailable'}
        change+=(int(m['lamports'])/L.LAMPORTS-L.EXEC_COST_SOL['exit'])*fx-leg['zero_bound_usd']
    return {'state':'partial','label':'unverified','net_usd':trading+change-cost,
            'basis':'last_proven_token_marks_at_current_pinned_fx; not an executable current value'}


def _coin_stats(data,legs,*,rng_seed,now,check):
    values={};by_order=defaultdict(float)
    for c in data['cashflows']:by_order[c['order_id']]+=c['usd']
    legmap={l['order_id']:l for l in legs}
    for o in data['orders']:
        if o['filled_at'] is None or o['filled_at']>now:continue
        leg=legmap.get(o['order_id'])
        v=by_order[o['order_id']]
        if leg:v+=leg['value_usd'] if leg['cause']=='known' else leg['zero_bound_usd']
        values[o['order_id']]=v
    sheet_strata=defaultdict(list);strata=defaultdict(list);exits=defaultdict(list);feedvalues=defaultdict(list);feed_core=defaultdict(lambda:defaultdict(list));feed_price=defaultdict(lambda:defaultdict(list))
    for o in data['orders']:
        if o['order_id'] not in values:continue
        feed=data['order_feed'].get(o['order_id'],'unknown');value=values[o['order_id']]
        feedvalues[feed].append(value)
        core=data['coin_core'].get((o['network_id'],o['mint']),'unknown');feed_core[feed][core].append(value)
        sh=data['own_sheets'].get(data['order_sheets'].get(o['order_id']),{})
        source=sh.get('sheet',{}).get('pool_state',{}).get('virtual_quote_source')
        price='exact' if source=='pumpswap_rpc:pool_state' else 'tape' if source in ('tape_tip','ps_tape','pumpswap_grpc:buy_event_virtual_quote') else 'unknown'
        feed_price[feed][price].append(value)
        strata[data['coin_core'].get((o['network_id'],o['mint']),'unattributed')].append(values[o['order_id']])
        sheet_strata['%s/%s'%(o.get('sheet_state') or 'unstamped',o.get('sells_basis') or 'unstamped')].append(values[o['order_id']])
        if o['closed_at'] is not None and o['closed_at']<=now:
            exits[o.get('exit_by') or 'unknown_cause'].append(values[o['order_id']])
    samples=list(values.values());n=len(samples)
    rng=random.Random(rng_seed)
    draws=[]
    for i in range(1000 if n else 0):
        if i%32==0:check()
        draws.append(sum(rng.choice(samples) for _ in samples)/n)
    draws.sort()
    # Partial-sheet orders have weaker entry evidence: their strata are reported, one figure over both is withheld (Alpha 2026-10-31).
    pooled=any(o.get('sheet_state')=='partial' and o['order_id'] in values for o in data['orders']) and len(sheet_strata)>1
    profits=sum(max(v,0) for v in samples)
    return {'per_feed':{f:{'feed':f,'label':'descriptive','filled':len(feedvalues[f]),'trading_net_usd':sum(feedvalues[f]),
            'core_strata':{k:{'filled':len(v),'mean_trading_net_per_coin_usd':sum(v)/len(v)} for k,v in feed_core[f].items()},
            'price_source':{k:{'filled':len(v),'mean_trading_net_per_coin_usd':sum(v)/len(v)} for k,v in feed_price[f].items()}} for f in ('pass','near_pass','unknown')},
        'stats_basis':'modeled trading net; censored legs use zero liquidation minus modeled exit costs; descriptive bounds',
        'per_core_stratum':{k:{'filled':len(v),'mean_trading_net_per_coin_usd':sum(v)/len(v)} for k,v in sorted(strata.items())},
        'per_sheet_and_sells':{k:{'filled':len(v),'mean_trading_net_per_coin_usd':sum(v)/len(v)} for k,v in sorted(sheet_strata.items())},
        'per_exit_cause':{k:{'filled':len(v),'mean_trading_net_per_coin_usd':sum(v)/len(v)} for k,v in sorted(exits.items())},
        'per_coin':values,'mean_trading_net_per_coin_usd':sum(samples)/n if n and not pooled else None,'blended_mean_withheld':pooled,
        'win_count':sum(v>0 for v in samples),'median_coin_usd':statistics.median(samples) if n and not pooled else None,
        'worst_coin_usd':min(samples) if n else None,
        'top3_profit_share':sum(sorted((max(v,0) for v in samples),reverse=True)[:3])/profits if profits else None,
        'coin_bootstrap_interval':{'state':'withheld_mixed_sheet_strata' if pooled else 'known' if n else 'unknown','kind':'descriptive percentile, coins as clusters',
            'iterations':1000,'mean_trading_net_per_coin_usd':[draws[24],draws[974]] if n and not pooled else None},
        'positive_utc_days':{'state':'unknown','reason':'complete_daily_mark_history_unavailable'}}


def _calendar_cost(opened_at,look_at):
    """Readiness cost rule without importing its NumPy/SciPy inference engine.

    Every touched UTC day, exact-midnight end exclusive, 800/month / calendar days.
    Cross-checked against costs.epoch_cost in focused tests; same frozen RULE_ID.
    """
    if not _number(opened_at) or not _number(look_at) or look_at<=opened_at:raise ValueError('cost_interval')
    day=datetime.fromtimestamp(opened_at,timezone.utc).date()
    last=datetime.fromtimestamp(look_at,timezone.utc).date()
    if look_at%86400==0:last-=timedelta(days=1)
    amount=0.;days=[]
    while day<=last:
        days.append(day.isoformat());amount+=800./calendar.monthrange(day.year,day.month)[1]
        day+=timedelta(days=1)
        if len(days)>366:raise ReadLimit('cost_calendar_limit')
    return {'amount_usd':amount,'days':days,'allocation_rule_id':costs.RULE_ID}


def _look(out, data, now, sol_usd, sol_at,check):
    """Called only after exact seal validation and required collection completeness."""
    legs=_legs(data,now,sol_usd,sol_at)
    unpriced=[leg['order_id'] for leg in legs if leg['cause']!='known']
    count=out['orders']['filled_coins']
    if len(unpriced)>count*.20:return {'state':'withheld','reason':'censored_share_exceeds_20pct'}
    if any(leg['cause']=='book_unknown' for leg in legs):return {'state':'withheld','reason':'valuation_unknown'}
    liq=sum(leg['value_usd'] if leg['cause']=='known' else leg['zero_bound_usd'] for leg in legs)
    trading=sum(x['usd'] for x in data['cashflows'])+liq
    cost=_calendar_cost(data['epoch']['opened_at'],now)
    coin_stats=_coin_stats(data,legs,rng_seed=digest(out['input_heads']),now=now,check=check)
    return {'state':'partial' if unpriced else 'known','value_kind':'lower_bound' if unpriced else 'point',
        'trading_net_usd':trading,'net_usd':trading-cost['amount_usd'],'cost_usd':cost['amount_usd'],'cost_days':cost['days'],'cost_rule_id':cost['allocation_rule_id'],
        'unpriced_count':len(unpriced),'unpriced_order_ids':unpriced,
        'mark_label_scope':'current mark alone cannot reconstruct continuously refreshed pause vs demotion; zero NET bound is the same; demotion counters are from the sealed optional latch','underpowered':out['eligibility']['underpowered'],
        'last_mark_reading':_last_mark(data,legs,trading,cost['amount_usd'],sol_usd),
        **coin_stats,
        'interpretation':'exploratory; no pass, power, nomination or causal attribution; b_unmeasured',
        'learning':{'state':'unknown','reason':'complete_lesson_injection_history_not_collected','no_loop_off_arm':True},
        'daily_block_intervals':{'state':'unknown','reason':'complete_daily_history_unavailable'}}


def quarantine(value, path=(), *, check=None):
    """Same structural gate for preparation and the externally sealed snapshot."""
    if check is not None:check()
    if len(path)>32:raise ValueError('snapshot_depth')
    forbidden=re.compile(r'pnl|net_usd|equity_usd|realized|profit|win|median|worst|top3|per_coin|closed.*(?:up|down|flat)',re.I)
    amount=re.compile(r'\$\s*[+-]?\d|[+-]?\d+(?:\.\d+)?\s*USD',re.I)
    metadata={'network_id','equity_snapshots','ledger.equity_snapshots'}
    if isinstance(value,dict):
        for key,item in value.items():
            if not isinstance(key,str) or (key not in metadata and forbidden.search(key)):raise ValueError('snapshot_financial_key')
            if _number(item) and key.endswith('_usd') and key not in ('clip_usd','capital_usd'):raise ValueError('snapshot_financial_amount')
            quarantine(item,path+(key,),check=check)
    elif isinstance(value,list):
        for item in value:quarantine(item,path+('[]',),check=check)
    elif isinstance(value,str):
        if amount.search(value):raise ValueError('snapshot_financial_text')
    elif value is not None and type(value) not in (bool,int,float):raise ValueError('snapshot_shape')
    elif type(value) is float and not math.isfinite(value):raise ValueError('snapshot_nonfinite')


def _safety_gate(data,now):
    snap=data['safety_snapshot'];current=data['safety'][0] if data['safety'] else None
    if current is None:return 'safety_latch_unreadable'
    cfg=_object(data['config'][0]['note'])['paper_eval'] if data['config'] else {}
    expected=digest(cfg)
    if (snap.get('state') not in ('known','partial_lower_bound') or snap.get('config_hash')!=expected or
        current.get('config_hash')!=expected or not _number(snap.get('last_equity_read_at')) or
        not 0<=now-snap['last_equity_read_at']<=cfg['unknown_max_s'] or
        not _number(current.get('last_equity_read_at')) or current['last_equity_read_at']<snap['last_equity_read_at']):
        return 'safety_latch_unreadable'
    for row in (snap,current):
        if row.get('halted_at') is not None and row['halted_at']<=now:return 'epoch_not_efficacy_eligible'
    return None


def _pair_matrix(data,check):
    """Only complete, published candidate rounds; a missing pair is unattributed."""
    def empty():return {c:{g:0 for g in J.DECISIONS} for c in J.DECISIONS}
    total=empty();feeds={f:empty() for f in ('pass','near_pass','unknown')}
    decisions=defaultdict(list);ends={};sheets=dict(data['own_sheets']);unattributed=Counter()
    missing_by_feed={f:Counter() for f in feeds}
    for p,h in data['receipts']:
        check()
        if p.get('kind')=='judge.decide':decisions[p['decision_id']].append(p)
        elif p.get('kind')=='judge.round.end':ends.setdefault(p['round_id'],p)
        elif p.get('kind')=='sheet' and p.get('state')=='known':sheets[h]=p
    seen=set()
    for row in data['rounds']:
        check();rid=row['round_id']
        if rid in seen:continue
        seen.add(rid);end=ends.get(rid)
        if end is None or end.get('outcome')!='complete':continue
        feed=_feed(sheets.get(row['candidate']))
        if rid!=digest([row['epoch_id'],row['candidate']]):
            unattributed['round_identity_unknown']+=1;missing_by_feed[feed]['round_identity_unknown']+=1;continue
        if not isinstance(end.get('committed_slots'),list) or set(end['committed_slots'])!=set(J.JUDGE_SLOTS):
            unattributed['complete_round_slots_unknown']+=1;missing_by_feed[feed]['complete_round_slots_unknown']+=1;continue
        did=J.decision_id(row['epoch_id'],{'decision':'ENTER','candidate':row['candidate']})
        first={}
        for answer in decisions.get(did,[]):first.setdefault(answer['slot'],answer)
        if (set(first)!=set(J.JUDGE_SLOTS) or J.published(list(first.values())) is None or
            any(answer.get('decision') not in J.DECISIONS for answer in first.values())):
            unattributed['complete_round_pair_missing']+=1;missing_by_feed[feed]['complete_round_pair_missing']+=1;continue
        c=first['judge_claude']['decision'];g=first['judge_gpt']['decision']
        feed=_feed(sheets.get(row['candidate']));total[c][g]+=1;feeds[feed][c][g]+=1
    return total,feeds,dict(unattributed),{f:dict(v) for f,v in missing_by_feed.items()}


def _descriptive_counts(r,data,out):
    """Aggregates are explicitly not body verified. Truncated details stay unknown."""
    aggregate_state=r.optional_heads.get('optional.ticket_aggregates',{}).get('state')!='incomplete'
    if 'optional.ticket_aggregates' in r.optional_heads and aggregate_state:
        admitted=sum(x['n'] for x in data['ticket_aggregates'] if x['verdict']=='PASS')
        refused=sum(x['n'] for x in data['ticket_aggregates'] if x['verdict']=='SKIP' and x['refused_unknown'])
        out['admissions'].update(state='known',admitted_tickets=admitted,refused_unknown_tickets=refused,
                                  verification='sql_aggregate_not_body_verified')
        if 'optional.desk_tickets' in r.optional:
            out['admissions']['pass_never_listed']={'state':'unknown','reason':'ticket_identity_window_incomplete'}
            out['seen'].update(state='unknown',reason='ticket_identity_window_incomplete',seen_tickets=None)
            for cell in out['strata']['core_list'].values():
                cell.update(state='unknown',reason='ticket_identity_window_incomplete')
                for key in ('ENTER','WAIT','SKIP','split','filled','censored'):cell[key]=None
        for cell in out['strata']['core_list'].values():cell['admitted_coins']=0
        for row in data['ticket_aggregates']:
            if row['verdict']!='PASS':continue
            key='both_unknown' if row['dg'] and row['cluster'] else 'unknown_dump_group' if row['dg'] else 'unknown_cluster' if row['cluster'] else 'all_known'
            out['strata']['core_list'][key]['admitted_coins']+=row['coins']
    if 'optional.sheet_metadata' in r.optional:
        out['seen'].update(state='unknown',sheet_coins=None,reason='sheet_metadata_incomplete')
    for row in data['sheet_aggregates']:
        f=row['feed'] if row['feed'] in ('pass','near_pass') else 'unknown'
        out['feeds'][f].setdefault('sheet_receipt_aggregates',[]).append({
            'source':row['source'],'receipts':row['n'],'coins':row['coins'],'verification':'sql_aggregate_not_body_verified'})
    if 'optional.judge_receipts' in r.optional:
        if 'optional.decision_aggregates' in r.optional_heads and 'optional.decision_aggregates' not in r.optional:
            counts={str(row['label']):row['n'] for row in data['decision_aggregates']}
            out['decisions'].update(state='partial',published_targets=sum(v for k,v in counts.items() if k!='unpublished'),
                by_label={k:v for k,v in counts.items() if k!='unpublished'},unpublished_targets=counts.get('unpublished',0),
                wait_by_blocker=None,verification='sql_aggregate_not_body_verified',reason='memo_and_feed_details_unknown')
        else:
            out['decisions'].update(state='unknown',published_targets=None,by_label=None,wait_by_blocker=None,
                                    reason='judge_receipt_window_incomplete')
    if any(label=='optional.rounds' or label.startswith('optional.linked_receipts:') for label in r.optional):
        out['rounds']={'state':'unknown','rounds':None,'reason':'round_receipt_window_incomplete'}
    if 'optional.broker_receipts' in r.optional:
        out['strata']['price_source']['unpriced_attempts'].update(state='unknown',by_reason=None,reason='broker_receipt_window_incomplete')
        out['admissions']['pass_unlisted_snapshots_state']='unknown'
        out['admissions']['pass_never_listed']={'state':'unknown','reason':'broker_receipt_window_incomplete'}
    else:
        out['strata']['price_source']['unpriced_attempts']['state']='known'
        out['admissions']['pass_unlisted_snapshots_state']='known'
    if 'optional.desk_tickets' in r.optional:
        out['coverage']['by_hour_state']='unknown';out['coverage']['creator_history_state']='unknown'
    else:
        out['coverage']['by_hour_state']='known';out['coverage']['creator_history_state']='known'
    if any(label=='optional.judge_receipts' or label.startswith('optional.linked_receipts:') or
           label=='optional.decision_receipts' for label in r.optional):
        out['enter_not_ordered']={'state':'unknown','by_reason':None,'reason':'decision_window_incomplete'}
    for row in out['feeds'].values():
        row['state']='known'
        if 'optional.judge_receipts' in r.optional or 'optional.sheet_metadata' in r.optional:
            row.update(state='partial',decisions=None,judged_targets=None,reason='candidate_feed_window_incomplete')
    def basis_counts():
        return {seat:{basis:0 for basis in ('echoed','requested_pinned','unknown')} for seat in J.JUDGE_SLOTS}
    if 'optional.decide_basis' in r.optional_heads and 'optional.decide_basis' not in r.optional:
        total=basis_counts()
        for feed in out['feeds'].values():
            feed['by_seat_and_basis']=basis_counts();feed['basis_count_state']='known'
        for row in data['basis_aggregates']:
            total[row['seat']][row['basis']]+=row['n']
            out['feeds'][row['feed']]['by_seat_and_basis'][row['seat']][row['basis']]+=row['n']
        out['decisions'].update(by_seat_and_basis=total,basis_count_state='known',
            basis_count_unit='committed judge.decide receipts, including unpublished; no memo content displayed',
            basis_evidence='same turn_decision_id memo.commit identity; SQL metadata not_body_verified; declared basis, never echo inferred from model name')
    else:
        out['decisions'].update(by_seat_and_basis=None,basis_count_state='unknown',basis_count_reason='memo_join_read_unavailable')
        for feed in out['feeds'].values():feed.update(by_seat_and_basis=None,basis_count_state='unknown')
    failures={'state':'known','by_reason':{},'attributed_attempts':0,'attribution_unknown_attempts':0,
        'coin_state':'coins_unknown','distinct_coins':None,
        'unit':'judge.round.sheet_failed receipts; assignment/freshness failures, not proof of unavailable market prices'}
    details=Counter();feed_details={f:Counter() for f in out['feeds']}
    for feed in out['feeds'].values():feed['sheet_failed_attempts']={'state':'known','by_reason':{},'attempts':0}
    if 'optional.sheet_failed_aggregates' in r.optional:
        failures.update(state='unknown',by_reason=None,attributed_attempts=None,attribution_unknown_attempts=None,
                        reason='sheet_failed_read_incomplete',reason_details=None)
        for feed in out['feeds'].values():feed['sheet_failed_attempts']={'state':'unknown','by_reason':None,'attempts':None,'reason_details':None}
    else:
        for row in data.get('sheet_failed_aggregates',[]):
            if row['epoch_id']!=out['epoch_id']:
                failures['attribution_unknown_attempts']+=row['n'];continue
            name=row['feed'] if row['feed'] in ('pass','near_pass') else 'unknown'
            reason=row['reason'] if isinstance(row['reason'],str) else 'reason_unknown'
            failures['by_reason'][reason]=failures['by_reason'].get(reason,0)+row['n']
            failures['attributed_attempts']+=row['n']
            cell=out['feeds'][name]['sheet_failed_attempts'];cell['attempts']+=row['n']
            cell['by_reason'][reason]=cell['by_reason'].get(reason,0)+row['n']
            detail=_object(row['detail_json']) if row['detail_json'] else {}
            state=detail.get('state');why=detail.get('reason')
            valid=(isinstance(state,str) and 0<len(state)<=64 and isinstance(why,str) and 0<len(why)<=200)
            key=('known',state,why) if valid else ('unknown',None,'reason_detail_unavailable')
            details[key]+=row['n'];feed_details[name][key]+=row['n']
        def detail_rows(counts):
            return [{'state':state,'tool_state':tool_state,'reason':why,'attempts':n}
                    for (state,tool_state,why),n in sorted(counts.items())]
        failures['reason_details']=detail_rows(details)
        for name,feed in out['feeds'].items():feed['sheet_failed_attempts']['reason_details']=detail_rows(feed_details[name])
    out['strata']['price_source']['unpriced_attempts']['round_sheet_failed']=failures
    matrix_incomplete=any(label in ('optional.rounds','optional.judge_receipts') or
                          label.startswith('optional.linked_receipts:') for label in r.optional)
    if matrix_incomplete:
        out['decisions'].update(pair_matrix=None,pair_matrix_state='unknown',pair_matrix_reason='round_receipt_window_incomplete')
        for feed in out['feeds'].values():feed.update(pair_matrix=None,pair_matrix_state='unknown')
    else:
        total,matrices,missing,missing_feeds=_pair_matrix(data,r.check)
        state='partial' if missing else 'known'
        out['decisions'].update(pair_matrix=total,pair_matrix_state=state,pair_matrix_unattributed=missing,
            pair_matrix_unit='distinct published round_ids with round.end complete; Claude rows x GPT columns')
        for name,feed in out['feeds'].items():feed.update(pair_matrix=matrices[name],
            pair_matrix_state='partial' if missing_feeds[name] else 'known',pair_matrix_unattributed=missing_feeds[name])
    out['descriptive_verification']='SQL aggregates and metadata are not_body_verified; own-order attribution is reverified at look'
    out['coverage']['table_states']={key:('unknown' if value.get('state')=='incomplete' else 'known')
                                    for key,value in r.optional_heads.items()}


def _minimal_summary(r,data,epoch_id,now):
    """Malformed descriptive input cannot turn into a required financial failure."""
    fresh=dict(data,receipts=[pair for pair in data['receipts'] if pair[0].get('kind')=='epoch.end'],
               tickets=[],refused_unknown=[],rounds=[],calls=[],call_retention=[],memos={},decision_receipts={})
    return _summary(r,fresh,epoch_id,now)


def build_report(office_path, ledger_path, *, now, desk_path=None, epoch_id='office-paper-eval-1',
                 mode='health', limits=None, look_record=None, look_sha256=None, sol_usd=None, sol_at=None):
    if not _number(now) or now<0 or mode not in ('health','look'):raise ValueError('report_arguments')
    lim=dict(DEFAULT_LIMITS,**(limits or {}));ceiling=LOOK_CEILINGS if mode=='look' else DEFAULT_LIMITS
    if (set(lim)!=set(DEFAULT_LIMITS) or any(not _number(v) or v<=0 for v in lim.values()) or
        any(type(lim[k]) is not int for k in ('max_rows','max_queries','max_bytes')) or any(lim[k]>ceiling[k] for k in lim)):
        raise ValueError('report_limits')
    r=Reader(lim)
    record=None;record_error=None
    if mode=='look' and look_record is not None and look_sha256 is not None:
        try:
            if not isinstance(look_record,bytes) or len(look_record)>4*1024*1024 or hashlib.sha256(look_record).hexdigest()!=look_sha256:
                raise ValueError('look_hash')
            record=_object(look_record)
            if record.get('version')!='office-eval-look-v2':raise ValueError('look_version')
            quarantine(record['descriptive_snapshot'],check=r.check)
            if set(record['safety_snapshot'])-{'state','config_hash','last_equity_read_at','halt_kind','halted_at'}:
                raise ValueError('safety_snapshot_shape')
            quarantine(record['safety_snapshot'],check=r.check)
            if digest(record['descriptive_snapshot'])!=record['descriptive_snapshot_sha256']:raise ValueError('snapshot_hash')
        except (ValueError,TypeError,KeyError,RecursionError,UnicodeError):record=None;record_error='look_record_invalid'
    if record_error is not None:
        return {'state':'unknown','epoch_id':epoch_id,'as_of':now,'limits':lim,
                'economics':{'state':'withheld','reason':record_error}}
    try:
        o=r.open(office_path);l=r.open(ledger_path)
        try:d=r.open(desk_path) if desk_path is not None else None
        except (sqlite3.Error,TypeError):d=None
        data=_load_required(r,o,l,epoch_id,now,record)
        # Optional reads cannot consume the whole required calculation deadline.
        r.optional_until=min(r.until-0.1,time.monotonic()+min(.4,lim['deadline_s']/2))
        _attribution(r,data,d,epoch_id,now)
        if record is None:
            try:_descriptive(r,data,d,epoch_id,now)
            except (ValueError,TypeError,KeyError,AttributeError,RecursionError):r.optional.append('optional.descriptive_integrity')
            try:out=_summary(r,data,epoch_id,now)
            except (ValueError,TypeError,KeyError,AttributeError):
                r.optional.append('optional.descriptive_integrity');out=_minimal_summary(r,data,epoch_id,now)
            _descriptive_counts(r,data,out)
        else:
            out=json.loads(S.canonical_json(record['descriptive_snapshot']['report']))
            if out.get('epoch_id')!=epoch_id or out.get('as_of')!=now:raise ValueError('snapshot_cut')
            # Eligibility comes from the current causal ledger projection, not frozen counts.
            filled=[x for x in data['orders'] if x['filled_at'] is not None]
            first=min((x['requested_at'] for x in data['orders']),default=None)
            age=0 if first is None else now-first;opened=sum(x['closed_at'] is None for x in filled)
            n=len({(x['network_id'],x['mint']) for x in filled})
            out['eligibility']={'eligible_as_of':bool(first is not None and (age>=28*86400 or (n>=30 and opened==0 and age>=14*86400))),
                'filled_coins':n,'open_positions':opened,'first_order_at':first,'ceiling_reached':age>=28*86400,'underpowered':n<30}
            out['orders']['filled_coins']=n;out['orders']['open_positions']=opened
        out['input_heads']=r.heads;out['limits']=lim;out['receipt_seq_cut']=data['receipt_seq_cut']
        out['safety_snapshot']=data['safety_snapshot']
        out['coverage']['required_complete']=not r.missing
        out['coverage']['required_missing']=sorted(set(r.missing))
        if record is None:
            snapshot={'computed_by':script_sha256(),'as_of':now,'optional_input_heads':r.optional_heads,
                'table_states':out['coverage'].get('table_states',{}),
                'report':{key:value for key,value in out.items() if key not in
                          ('economics','input_heads','limits','receipt_seq_cut','safety_snapshot')}}
            quarantine(snapshot,check=r.check)
            out['descriptive_snapshot']=snapshot;out['descriptive_snapshot_sha256']=digest(snapshot)
        if mode=='look':
            out['mode']='look'
            why=record_error or ('look_record_required' if record is None else None)
            if record is not None:
                snap=record['descriptive_snapshot']
                expected={'version':'office-eval-look-v2','epoch_id':epoch_id,'look_at':now,'report_script_sha256':script_sha256(),
                    'input_heads':r.heads,'eligible_counts':out['eligibility'],'limits':lim,
                    'fx':{'sol_usd':sol_usd,'sol_at':sol_at},'receipt_seq_cut':data['receipt_seq_cut'],
                    'safety_snapshot':data['safety_snapshot'],'descriptive_snapshot':snap,'descriptive_snapshot_sha256':digest(snap)}
                why=_safety_gate(data,now)
                if why is None and (record!=expected or snap.get('computed_by')!=script_sha256() or snap.get('as_of')!=now):why='look_record_mismatch'
                if why is None and r.missing:why='required_inputs_incomplete'
                if why is None and any(p.get('status') in ('FAILED','BROKEN') for p,_ in data['receipts'] if p.get('kind')=='epoch.end'):
                    why='epoch_not_efficacy_eligible'
                if why is None and not out['eligibility']['eligible_as_of']:why='look_not_due'
                out['look_record_sha256']=look_sha256
                out['descriptive_snapshot_sealed']={'not_reverified_at_look':True,'sha256':digest(snap),
                    'computed_by':snap['computed_by'],'as_of':snap['as_of'],'optional_input_heads':snap['optional_input_heads'],
                    'table_states':snap['table_states']}
                current=data['safety'][0] if data['safety'] else {}
                if current.get('halted_at') is not None and current['halted_at']<=now:
                    out['epoch_status']='BROKEN' if current.get('halt_kind')=='config_drift' else 'FAILED'
                out['post_look_halt']=bool(current.get('halted_at') is not None and current['halted_at']>now)
            r.check()
            out['economics']={'state':'withheld','reason':why} if why else _look(out,data,now,sol_usd,sol_at,r.check)
            r.check()
        return out
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,AttributeError,IndexError,RecursionError,UnicodeError) as exc:
        return {'state':'unknown','epoch_id':epoch_id,'as_of':now,'reason':type(exc).__name__,
            'economics':{'state':'withheld','reason':'report_inputs_unknown'},'input_heads':r.heads,'limits':lim,
            'coverage':{'complete':False,'required_complete':False,'required_missing':sorted(set(r.missing+['report_integrity']))}}
    finally:r.close()
