"""Crew-only discovery transition. No ledger writes, model calls or service control."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import time

from . import store as S,runtime_observations as O

OLD='office-pilot-discovery-1'
NEW='office-pilot-discovery-2'
GAMMA_APPROVAL='a7bbb9bc2-era review 2026-10-29: prompt delta + refs line approved with 5 conditions'
DECISION_FIELDS=['dm_context','dm_count','dm_unread_backlog','ref_context','refs_count']
_FIELDS={'epoch','predecessor','prompt_hashes','tool_contract_hash','release','roster','host_guard',
         'evidence','blind_judge_rule','decision_fields','counters','carryover','approvals'}
_IGNORED={'agents','epochs','supervisor_lease','heartbeats','office_epoch_control','org_changes'}


def _hash(doc):return hashlib.sha256(S.canonical_json(doc)).hexdigest()


def tool_hash():
    from .runtime_steps import SCHEMA
    return _hash(SCHEMA)


def _hex(value,n):return isinstance(value,str) and len(value)==n and all(c in '0123456789abcdef' for c in value)


def _utc(value):return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def _timestamp(value):
    if value is None:return True
    try:
        return isinstance(value,str) and datetime.fromisoformat(value.replace('Z','+00:00')).strftime('%Y-%m-%dT%H:%M:%S.%fZ')==value
    except ValueError:return False


def _ident(name):return '"'+name.replace('"','""')+'"'


def capital_hash(ledger):
    book=ledger.execute("SELECT * FROM books WHERE book_id='pilot-floor'").fetchone()
    flows=ledger.execute("SELECT * FROM capital_flows WHERE book_id='pilot-floor' ORDER BY id LIMIT 1001").fetchall()
    if book is None or book['kind']!='discovery' or not flows or len(flows)>1000:
        raise ValueError('epoch_capital_unknown')
    return _hash({'book':dict(book),'flows':[dict(row) for row in flows]})


def _tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _fingerprint(conn,*,ignored=()):
    """Bounded quiescent-state proof; refuse a large DB rather than claim it is empty."""
    digest=hashlib.sha256();size=0;deadline=time.monotonic()+2
    for table in _tables(conn):
        if table in ignored:continue
        # shortcut: transition/rollback refuses above 5000 rows/table or 32MiB; use an audited indexed frontier plan if exceeded.
        definition=conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
        order='rowid'
        if 'WITHOUT ROWID' in definition.upper():
            keys=sorted((r for r in conn.execute('PRAGMA table_info('+_ident(table)+')') if r[5]),key=lambda r:r[5])
            if not keys:raise ValueError('epoch_table_identity_unknown')
            order=','.join(_ident(r[1]) for r in keys)
        rows=conn.execute('SELECT * FROM '+_ident(table)+' ORDER BY '+order+' LIMIT 5001')
        digest.update(table.encode())
        for index,row in enumerate(rows):
            if index>=5000:raise ValueError('epoch_snapshot_unbounded')
            raw=repr(tuple(row)).encode();size+=len(raw)
            if size>33554432 or time.monotonic()>deadline:raise ValueError('epoch_snapshot_unbounded')
            digest.update(len(raw).to_bytes(8,'big'));digest.update(raw)
    return digest.hexdigest()


def _frontiers(conn):
    frontiers={};last=[];unknown=False
    for table in _tables(conn):
        columns={r[1] for r in conn.execute('PRAGMA table_info('+_ident(table)+')')}
        if 'seq' not in columns:continue
        row=conn.execute('SELECT * FROM '+_ident(table)+' ORDER BY seq DESC LIMIT 1').fetchone()
        frontiers[table]=0 if row is None else row['seq']
        if row is None:continue
        doc=json.loads(row['payload_json']) if 'payload_json' in columns else dict(row)
        stamp=next((doc[k] for k in ('ended_at','known_at','at','created_at') if O._number(doc.get(k))),None)
        if stamp is None:unknown=True
        else:last.append(stamp)
    return frontiers,None if unknown or not last else _utc(max(last))


def _actors(conn):
    rows=conn.execute('SELECT id,payload_json FROM agents LIMIT 5').fetchall()
    actors={r[0]:json.loads(r[1]) for r in rows}
    if len(actors)!=4 or any(a.get('state')!='active' or type(a.get('generation'))is not int or a['generation']<1 for a in actors.values()):
        raise ValueError('epoch_roster_unknown')
    return actors


def _control(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_epoch_control'").fetchone():return None
    row=conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def _frozen(conn,control):
    row=conn.execute('SELECT payload_json,prev_hash FROM org_changes WHERE row_hash=?',(control['receipt_id'],)).fetchone()
    if row is None:raise ValueError('epoch_transition_unknown')
    doc=json.loads(row[0])
    if hashlib.sha256(S.canonical_json(doc)+row[1].encode()).hexdigest()!=control['receipt_id']:
        raise ValueError('epoch_transition_corrupt')
    return doc


def _lease(conn,now):
    row=conn.execute('SELECT expires_at FROM supervisor_lease WHERE id=1').fetchone()
    if row is None or not O._number(row[0]):raise ValueError('epoch_lease_unknown')
    if row[0]>now:raise ValueError('epoch_supervisor_lease_live')


def _validate(record,manifest,actors,ledger,guard_hash):
    try:
        if set(record)!=_FIELDS or record['epoch']!={'id':NEW,'mode':'discovery'}:raise ValueError()
        p=record['predecessor']
        if set(p)!={'id','sealed_at','last_seq','last_row_at'} or p['id']!=OLD or not _timestamp(p['sealed_at']) or not _timestamp(p['last_row_at']) or not isinstance(p['last_seq'],dict):raise ValueError()
        if any(not isinstance(k,str) or type(v)is not int or v<0 for k,v in p['last_seq'].items()):raise ValueError()
        if record['prompt_hashes']['old']!={aid:a['prompt_hash'] for aid,a in actors.items()}:raise ValueError()
        new={}
        for aid,a in actors.items():
            seats=[p for p in manifest['seats'] if p['role']==a['role'] and p['family']==a['family']]
            if len(seats)!=1:raise ValueError()
            seat=seats[0]
            if any(seat[k]!=a[k] for k in ('role','family','provider','model','policy_hash')) or not _hex(seat['prompt_hash'],64):raise ValueError()
            new[aid]=seat['prompt_hash']
        if set(record['prompt_hashes'])!={'old','new'} or record['prompt_hashes']['new']!=new:raise ValueError()
        if set(record['tool_contract_hash'])!={'old','new'} or not _hex(record['tool_contract_hash']['old'],64) or record['tool_contract_hash']['new']!=tool_hash():raise ValueError()
        release=record['release']
        if set(release)!={'sha','process_start_at','process_start_source'} or not _hex(release['sha'],40) or release['process_start_source']!='ps' or not _timestamp(release['process_start_at']):raise ValueError()
        roster={aid:{**{k:a[k] for k in ('model','provider','family')},'generation':a['generation']+1} for aid,a in actors.items()}
        if record['roster']!={'agents':roster,'unchanged_from_predecessor':True} or record['roster']['unchanged_from_predecessor'] is not True:raise ValueError()
        if record['host_guard']!={'file':'config/office/host_guard_pilot.json','sha256':guard_hash,'policy':'office-host-guard-pilot-v3'} or not _hex(guard_hash,64):raise ValueError()
        if any(type(record['evidence'].get(k))is not bool for k in ('discovery_only','evaluated','counts_as_evidence')):raise ValueError()
        if record['evidence']!={'discovery_only':True,'evaluated':False,'never_pooled_with':[OLD],'counts_as_evidence':False}:raise ValueError()
        if record['blind_judge_rule']!={'dm_to_or_from_judge_before_verdict':'flagged','flag_field':'dm_context'} or record['decision_fields']!=DECISION_FIELDS:raise ValueError()
        counters=record['counters']
        if set(counters)!={'injection_as_instruction','refused_sends_by_reason','refused_refs_by_reason'}:raise ValueError()
        if counters['injection_as_instruction'] is not None and (type(counters['injection_as_instruction'])is not int or counters['injection_as_instruction']!=0):raise ValueError()
        for key in ('refused_sends_by_reason','refused_refs_by_reason'):
            if not isinstance(counters[key],dict) or any(not isinstance(k,str) or type(v)is not int or v<0 for k,v in counters[key].items()):raise ValueError()
        if record['carryover'].get('paper_book_preserved') is not True:raise ValueError()
        if record['carryover']!={'paper_book_preserved':True,'capital_snapshot_sha256':capital_hash(ledger)}:raise ValueError()
        if set(record['approvals'])!={'alpha','gamma'} or not isinstance(record['approvals']['alpha'],str) or not record['approvals']['alpha'].strip() or record['approvals']['gamma']!=GAMMA_APPROVAL:raise ValueError()
    except (KeyError,TypeError,ValueError,OverflowError):raise ValueError('epoch_approval_binding_invalid') from None


def transition(conn,ledger,*,manifest,record,now,guard_hash,apply=False,rollback=False):
    if not O._number(now):raise ValueError('epoch_clock_unknown')
    approval=_hash({'manifest':manifest,'record':record})
    # Dry-run has no schema creation or writes; apply serializes with lease acquisition.
    if apply:
        with S.write_transaction(conn):return _transition(conn,ledger,manifest,record,now,guard_hash,approval,rollback,True)
    owned=not conn.in_transaction
    if owned:conn.execute('BEGIN')
    try:return _transition(conn,ledger,manifest,record,now,guard_hash,approval,rollback,False)
    finally:
        if owned:conn.rollback()


def _transition(conn,ledger,manifest,record,now,guard_hash,approval,rollback,apply):
    if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0]!='office':raise ValueError('epoch_office_store_required')
    if len(conn.execute('SELECT seq FROM org_changes LIMIT 5001').fetchall())>5000 or not S.verify_chain(conn,'org_changes')[0]:raise ValueError('epoch_org_chain_unknown')
    if record.get('host_guard',{}).get('sha256')!=guard_hash or record.get('tool_contract_hash',{}).get('new')!=tool_hash():raise ValueError('epoch_approval_binding_invalid')
    _lease(conn,now);control=_control(conn)
    if control:
        frozen=_frozen(conn,control)
        if frozen['approval_sha256']!=approval:raise ValueError('epoch_approval_changed')
        if rollback:
            if control['active_epoch']==OLD:return {'state':'already_rolled_back'}
            return _rollback(conn,ledger,frozen,control,now,apply)
        if control['active_epoch']==NEW:return {'state':'already_applied','record':frozen['new_epoch']['record']}
        raise ValueError('epoch_transition_rolled_back')
    if rollback:raise ValueError('epoch_transition_missing')
    actors=_actors(conn);_validate(record,manifest,actors,ledger,guard_hash)
    epochs=conn.execute('SELECT id,payload_json FROM epochs LIMIT 3').fetchall()
    if len(epochs)!=1 or epochs[0][0]!=OLD:raise ValueError('epoch_predecessor_unknown')
    old=json.loads(epochs[0][1])
    if old.get('closed_at') is not None or old.get('kind')!='discovery' or old.get('evaluation_allowed') is not False:raise ValueError('epoch_predecessor_not_open_discovery')
    frontiers,last_at=_frontiers(conn)
    if record['predecessor']['last_seq'] and record['predecessor']['last_seq']!=frontiers:raise ValueError('epoch_frontier_changed')
    measured=deepcopy(record);measured['predecessor'].update(sealed_at=_utc(now),last_seq=frontiers,last_row_at=last_at)
    digest=_hash(manifest)
    new_actors={aid:dict(a,prompt_hash=record['prompt_hashes']['new'][aid],generation=a['generation']+1,
                         seed_manifest_sha256=digest,prompt_update_epoch=NEW) for aid,a in actors.items()}
    epoch=dict(epoch_id=NEW,opened_at=now,closed_at=None,kind='discovery',pilot=True,evaluation_allowed=False,
               book_id='pilot-floor',seed_manifest_sha256=digest,transition_id=approval,record=measured,
               prompt_paragraphs={aid:['dm_task','memo_refs']+(['near_pass'] if a['role']=='Scout' else []) for aid,a in actors.items()})
    event=dict(kind='pilot_epoch_transition',by='crew',at=now,epoch_id=NEW,approval_sha256=approval,
        old_epoch=old,new_epoch=epoch,old_actors=actors,new_actors=new_actors,manifest=manifest,approved_record=deepcopy(record),
        baseline_sha256=_fingerprint(conn,ignored=_IGNORED),ledger_sha256=_fingerprint(ledger),
        post_org_seq=conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM org_changes').fetchone()[0])
    if not apply:return {'state':'dry_run','record':measured,'approval_sha256':approval}
    receipt=S.append(conn,'org_changes',event)
    sealed=dict(old,closed_at=now)
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?',(S.canonical_json(sealed).decode(),OLD))
    for aid,a in new_actors.items():conn.execute('UPDATE agents SET payload_json=? WHERE id=?',(S.canonical_json(a).decode(),aid))
    conn.execute('INSERT INTO epochs VALUES(?,?)',(NEW,S.canonical_json(epoch).decode()))
    conn.execute('CREATE TABLE IF NOT EXISTS office_epoch_control(id INTEGER PRIMARY KEY CHECK(id=1),payload_json TEXT NOT NULL)')
    conn.execute('INSERT INTO office_epoch_control VALUES(1,?)',(S.canonical_json({'active_epoch':NEW,'receipt_id':receipt}).decode(),))
    return {'state':'transitioned','record':measured,'receipt_id':receipt}


def _rollback(conn,ledger,frozen,control,now,apply):
    epoch=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(NEW,)).fetchone()
    if (epoch is None or json.loads(epoch[0])!=frozen['new_epoch'] or _actors(conn)!=frozen['new_actors']
            or conn.execute('SELECT max(seq) FROM org_changes').fetchone()[0]!=frozen['post_org_seq']
            or _fingerprint(conn,ignored=_IGNORED)!=frozen['baseline_sha256'] or _fingerprint(ledger)!=frozen['ledger_sha256']):
        raise ValueError('epoch_new_epoch_has_written_or_changed')
    if not apply:return {'state':'dry_run_rollback'}
    S.append(conn,'org_changes',{'kind':'pilot_epoch_transition_rollback','by':'crew','at':now,'epoch_id':OLD,'transition_receipt_id':control['receipt_id']})
    conn.execute('DELETE FROM epochs WHERE id=?',(NEW,))
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?',(S.canonical_json(frozen['old_epoch']).decode(),OLD))
    for aid,a in frozen['old_actors'].items():conn.execute('UPDATE agents SET payload_json=? WHERE id=?',(S.canonical_json(a).decode(),aid))
    conn.execute('UPDATE office_epoch_control SET payload_json=? WHERE id=1',(S.canonical_json(dict(control,active_epoch=OLD)).decode(),))
    return {'state':'rolled_back'}


def adopted(conn,manifest):
    """Startup recognizes only a sealed transition, never reseeds the existing paper book."""
    control=_control(conn)
    if control is None:return None
    frozen=_frozen(conn,control)
    epoch_id=control['active_epoch']
    expected=frozen['new_actors'] if epoch_id==NEW else frozen['old_actors']
    if _actors(conn)!=expected:raise ValueError('epoch_roster_changed')
    if epoch_id==NEW and manifest!=frozen['manifest']:raise ValueError('epoch_prompt_manifest_changed')
    if epoch_id==NEW and frozen['new_epoch']['record']['tool_contract_hash']['new']!=tool_hash():raise ValueError('epoch_tool_contract_changed')
    if epoch_id==OLD and _hash(manifest)!=frozen['old_epoch']['seed_manifest_sha256']:raise ValueError('epoch_predecessor_manifest_changed')
    epoch=conn.execute('SELECT payload_json FROM epochs WHERE id=?',(epoch_id,)).fetchone()
    target=frozen['new_epoch'] if epoch_id==NEW else frozen['old_epoch']
    if epoch is None or json.loads(epoch[0])!=target:raise ValueError('epoch_record_changed')
    ids={seat['slot']:next(aid for aid,a in expected.items() if a['role']==seat['role'] and a['family']==seat['family']) for seat in manifest['seats']}
    return {'epoch_id':epoch_id,'book_id':'pilot-floor','agent_ids':ids,'seed_manifest_sha256':target['seed_manifest_sha256']}


def approval_draft(conn,ledger,manifest,*,guard_hash,release_sha):
    """Read-only template. Operator must attest the predecessor contract hash and approval ID."""
    control=_control(conn)
    if control:return deepcopy(_frozen(conn,control)['approved_record'])
    actors=_actors(conn);frontiers,last_at=_frontiers(conn)
    new={aid:next(s['prompt_hash'] for s in manifest['seats'] if s['role']==a['role'] and s['family']==a['family']) for aid,a in actors.items()}
    return dict(epoch={'id':NEW,'mode':'discovery'},predecessor={'id':OLD,'sealed_at':None,'last_seq':frontiers,'last_row_at':last_at},
        prompt_hashes={'old':{aid:a['prompt_hash'] for aid,a in actors.items()},'new':new},tool_contract_hash={'old':None,'new':tool_hash()},
        release={'sha':release_sha,'process_start_at':None,'process_start_source':'ps'},
        roster={'agents':{aid:{**{k:a[k] for k in ('model','provider','family')},'generation':a['generation']+1} for aid,a in actors.items()},'unchanged_from_predecessor':True},
        host_guard={'file':'config/office/host_guard_pilot.json','sha256':guard_hash,'policy':'office-host-guard-pilot-v3'},
        evidence={'discovery_only':True,'evaluated':False,'never_pooled_with':[OLD],'counts_as_evidence':False},
        blind_judge_rule={'dm_to_or_from_judge_before_verdict':'flagged','flag_field':'dm_context'},decision_fields=DECISION_FIELDS,
        counters={'injection_as_instruction':None,'refused_sends_by_reason':{},'refused_refs_by_reason':{}},
        carryover={'paper_book_preserved':True,'capital_snapshot_sha256':capital_hash(ledger)},approvals={'alpha':None,'gamma':GAMMA_APPROVAL})
