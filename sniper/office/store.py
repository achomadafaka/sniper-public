"""Canonical Office records, atomic appends and independently verifiable chains.

Domain modules validate their JSON fields. Private grades/outcomes use a separate
file opened with evaluation=True; the Office supervisor must never open that file.
A trusted external chain head is required to detect a removed tail.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

PRIVATE_TABLES=frozenset({'grades','outcome_captures'})
APPEND_ONLY_TABLES=frozenset({'claims','decisions','org_changes','grades','receipts',
    'decision_bundles','trial_registry','opportunity_log','outcome_captures',
    'cost_allocations','fee_attestations','sealed_views','opportunity_gaps','memory_entries','memory_snapshots','opportunity_coverage'})
MUTABLE_TABLES=frozenset({'agents','turn_runs','action_requests','messages','tickets','heartbeats','spend','epochs'})
_COLUMNS={
    'memory_entries':{'known_at':"REAL NOT NULL CHECK(json_type(payload_json,'$.known_at') IN ('integer','real'))", 'namespace':'TEXT NOT NULL', 'content':'TEXT NOT NULL', 'decision_id':'TEXT', 'supersedes':'TEXT'},
    'memory_snapshots':{'snapshot_id':'TEXT NOT NULL','epoch_id':'TEXT','cut':"REAL NOT NULL CHECK(json_type(payload_json,'$.cut') IN ('integer','real'))"},
    'receipts':{'cache_key':'TEXT'},
    'claims':{'claim_hash':'TEXT'},
    'decisions':{'candidate_seq':'INTEGER','decision_id':'TEXT','claim_hash':'TEXT REFERENCES claims(claim_hash)'},
    'grades':{'decision_seq':'INTEGER','candidate_seq':'INTEGER','epoch_id':'TEXT','kind':'TEXT'},
    'decision_bundles':{'decision_id':'TEXT'},
    'opportunity_coverage':{'epoch_id':'TEXT','source':'TEXT','from_at':'REAL','to_at':'REAL','proof_hash':'TEXT NOT NULL REFERENCES receipts(row_hash)'},
    'opportunity_gaps':{'epoch_id':'TEXT','source':'TEXT'},
    'opportunity_log':{'epoch_id':'TEXT','network_id':'TEXT','mint':'TEXT'},
    'outcome_captures':{'candidate_seq':'INTEGER'},
    'cost_allocations':{'epoch_id':'TEXT'},
    'trial_registry':{'k':'INTEGER'},
    'fee_attestations':{'account':'TEXT','cycle':'TEXT'},
    'sealed_views':{'epoch_id':'TEXT'},
}


def canonical_json(obj):
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


@contextmanager
def write_transaction(conn):
    """Serialize a write, or join an existing transaction without committing it."""
    nested=conn.in_transaction
    name='office_'+uuid.uuid4().hex
    conn.execute('SAVEPOINT '+name if nested else 'BEGIN IMMEDIATE')
    try:
        yield conn
        if nested:conn.execute('RELEASE SAVEPOINT '+name)
        else:conn.commit()
    except BaseException:
        if nested:
            conn.execute('ROLLBACK TO SAVEPOINT '+name)
            conn.execute('RELEASE SAVEPOINT '+name)
        else:conn.rollback()
        raise


def open_store(path: Path, *, evaluation=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(path),timeout=1.)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        purpose='evaluation' if evaluation else 'office'
        tables=PRIVATE_TABLES if evaluation else APPEND_ONLY_TABLES-PRIVATE_TABLES
        with write_transaction(conn):
            conn.execute('CREATE TABLE IF NOT EXISTS _store_info(id INTEGER PRIMARY KEY CHECK(id=1),purpose TEXT NOT NULL)')
            prior=conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
            if prior is not None and prior[0]!=purpose:
                raise ValueError('store_purpose_mismatch')
            conn.execute('INSERT OR IGNORE INTO _store_info VALUES(1,?)',(purpose,))
            for table in sorted(tables):
                columns={key:key+' '+kind+" GENERATED ALWAYS AS (json_extract(payload_json,'$."+key+"')) VIRTUAL"
                         for key,kind in _COLUMNS.get(table,{}).items()}
                extra=''.join(', '+definition for definition in columns.values())
                conn.execute('CREATE TABLE IF NOT EXISTS '+table+'('
                    "seq INTEGER PRIMARY KEY,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),"
                    "prev_hash TEXT NOT NULL UNIQUE CHECK(prev_hash='' OR length(prev_hash)=64),"
                    'row_hash TEXT NOT NULL UNIQUE CHECK(length(row_hash)=64)'+extra+')')
                present={row[1] for row in conn.execute('PRAGMA table_xinfo('+table+')')}
                for key,definition in columns.items():
                    if key not in present:
                        conn.execute('ALTER TABLE '+table+' ADD COLUMN '+definition)
                for operation in ('UPDATE','DELETE'):
                    conn.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_'+operation.lower()+' BEFORE '+operation+
                                 " ON "+table+" BEGIN SELECT RAISE(ABORT,'append_only'); END")
                for key in _COLUMNS.get(table,{}):
                    if table=='memory_entries' and key in {'content','namespace'}:continue
                    if (table,key) in {('claims','claim_hash'),('decisions','decision_id'),('memory_snapshots','snapshot_id')}:
                        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS '+table+'_'+key+'_identity ON '+table+'('+key+')')
                    else:
                        conn.execute('CREATE INDEX IF NOT EXISTS '+table+'_'+key+'_seq ON '+table+'('+key+',seq)')
            if not evaluation:
                from .runtime_observations import ensure_schema
                ensure_schema(conn)
                from .work import ensure_schema as ensure_work_schema
                ensure_work_schema(conn)
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS receipts_launcher_nonce ON receipts(json_extract(payload_json,'$.launcher_policy_nonce')) WHERE json_extract(payload_json,'$.kind')='launcher_policy_use'")
                conn.execute("CREATE INDEX IF NOT EXISTS receipts_judge_decide_seq ON receipts(seq) WHERE json_extract(payload_json,'$.kind')='judge.decide'")
                conn.execute('CREATE INDEX IF NOT EXISTS opportunity_coverage_epoch_source_from ON opportunity_coverage(epoch_id,source,from_at,seq)')
                # near-pass funnel (Gamma prereg 3): (c) Scout deliveries are broker receipts of the near-pass tools;
                # (d) sheets by feed and blocker set. Expression indexes only: no column, so an older writer is unaffected.
                conn.execute("CREATE INDEX IF NOT EXISTS receipts_near_pass_tool ON receipts(json_extract(payload_json,'$.tool'),seq) "
                             "WHERE json_extract(payload_json,'$.tool') IN ('desk.near_pass_list','desk.near_pass_sheet')")
                conn.execute("CREATE INDEX IF NOT EXISTS receipts_sheet_feed ON receipts(json_extract(payload_json,'$.request.feed'),"
                             "json_extract(payload_json,'$.request.blocker_set_id'),seq) WHERE json_extract(payload_json,'$.kind')='sheet'")
                # reporting funnel: day counts are indexed timestamp ranges (rowid order is not time order, late receipts append)
                conn.execute("CREATE INDEX IF NOT EXISTS receipts_sheet_at ON receipts(json_extract(payload_json,'$.clock_sample.corrected_utc')) WHERE json_extract(payload_json,'$.kind')='sheet'")
                conn.execute("CREATE INDEX IF NOT EXISTS receipts_broker_at ON receipts(json_extract(payload_json,'$.cut.receipt_cut')) WHERE json_extract(payload_json,'$.tool')='desk.near_pass_list' AND json_extract(payload_json,'$.cut.basis')='corrected_utc'")
                conn.execute('CREATE INDEX IF NOT EXISTS memory_entries_namespace_known_at ON memory_entries(namespace,known_at,seq)')
                fts_exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_entries_fts'").fetchone() is not None
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts USING fts5(content,content='memory_entries',content_rowid='seq')")
                conn.execute("CREATE TRIGGER IF NOT EXISTS memory_entries_fts_insert AFTER INSERT ON memory_entries BEGIN INSERT INTO memory_entries_fts(rowid,content) VALUES(new.seq,new.content); END")
                if not fts_exists:
                    conn.execute("INSERT INTO memory_entries_fts(memory_entries_fts) VALUES('rebuild')")
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS opportunity_log_coin ON opportunity_log(epoch_id,network_id,mint)')
                conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS decision_bundles_decision ON decision_bundles(decision_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS fee_attestations_account_cycle ON fee_attestations(account,cycle,seq)')
                for table in sorted(MUTABLE_TABLES):
                    conn.execute('CREATE TABLE IF NOT EXISTS '+table+"(id TEXT NOT NULL PRIMARY KEY,payload_json TEXT NOT NULL "
                                 "CHECK(json_valid(payload_json) AND json_type(payload_json)='object'))")
                conn.execute("CREATE INDEX IF NOT EXISTS turn_runs_created_at ON turn_runs(json_extract(payload_json,'$.created_at'))")
                conn.execute("CREATE INDEX IF NOT EXISTS turn_runs_deferred_deadline ON turn_runs(json_extract(payload_json,'$.deadline_at'),id) WHERE json_extract(payload_json,'$.state')='deferred'")
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise sqlite3.IntegrityError('foreign_key_violation')
        return conn
    except BaseException:
        conn.close()
        raise


def _table(table):
    if table not in APPEND_ONLY_TABLES:raise ValueError('unknown_append_table')
    return table


def append(conn, table: str, row: dict) -> str:
    table=_table(table)
    if not conn.execute('PRAGMA foreign_keys').fetchone()[0]:raise ValueError('foreign_keys_required')
    if not isinstance(row,dict):raise TypeError('record_must_be_object')
    if {'seq','row_hash','prev_hash'}&row.keys():raise ValueError('reserved_record_fields')
    payload=canonical_json(row)
    with write_transaction(conn):
        prior=conn.execute('SELECT row_hash FROM '+table+' ORDER BY seq DESC LIMIT 1').fetchone()
        prev='' if prior is None else prior[0]
        digest=hashlib.sha256(payload+prev.encode('ascii')).hexdigest()
        conn.execute('INSERT INTO '+table+'(payload_json,prev_hash,row_hash) VALUES(?,?,?)',
                     (payload.decode('utf-8'),prev,digest))
    return digest


def verify_chain(conn, table: str) -> tuple[bool,int|None]:
    table=_table(table);previous='';expected_seq=1
    for seq,payload,prev,digest in conn.execute('SELECT seq,payload_json,prev_hash,row_hash FROM '+table+' ORDER BY seq'):
        try:
            if not isinstance(payload,str):return False,seq
            row=json.loads(payload);canonical=canonical_json(row)
            expected=hashlib.sha256(canonical+previous.encode('ascii')).hexdigest()
            if (not isinstance(row,dict) or canonical.decode('utf-8')!=payload or seq!=expected_seq
                    or prev!=previous or digest!=expected):return False,seq
        except (ValueError,TypeError,UnicodeError):return False,seq
        previous=digest;expected_seq+=1
    return True,None


def latest(conn, table: str, key_col: str, key):
    """Privileged, indexed latest-record lookup; never expose raw stores to agents."""
    table=_table(table)
    if key_col not in {'row_hash'}|_COLUMNS.get(table,{}).keys():raise ValueError('unindexed_lookup_key')
    row=conn.execute('SELECT seq,payload_json,prev_hash,row_hash FROM '+table+' WHERE '+key_col+'=? ORDER BY seq DESC LIMIT 1',(key,)).fetchone()
    return None if row is None else dict(zip(('seq','payload_json','prev_hash','row_hash'),row))
