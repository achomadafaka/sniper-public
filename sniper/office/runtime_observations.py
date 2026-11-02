"""Public runtime observations, flushed off dispatch; never a source of action authority."""
from collections import deque
import hashlib
import json
import math
import sqlite3
import time

from . import store as S
from .launcher_diagnostics import redact_text

RETENTION_S = 30 * 86400
TABLES = {'model_calls':'ended_at', 'handoff_events':'at'}
REF_KINDS = {'memo.commit':'memo', 'org.propose':'proposal', 'ticket.propose':'proposal', 'decision.bundle':'decision'}
SAFE_REASONS = frozenset({'tool_event','structured_output_missing','claude_sdk_vendor_missing','claude_sdk_vendor_mismatch','claude_sdk_wheel_invalid','claude_sdk_manifest_mismatch','claude_sdk_import_conflict','claude_sdk_missing','claude_sdk_version_mismatch','claude_cli_hash_mismatch','claude_cli_version_unknown','claude_cli_too_old','timeout','provider_timeout','auth_revoked','auth_expired','rate_limit','rate_limited',
    'provider_busy','provider_paused','codex_busy','provider_error','transport_error','invalid_result',
    'launcher_identity','launcher_refused','existing_unknown','configuration','schema_configuration',
    'model_mismatch','served_model_unknown','clock_unknown','clock_regressed','deadline','no_codex_login','no_isolation',
    'claude_auth_near_expiry','claude_auth_unknown','codex_cleanup_unproven','codex_auth_writeback_refused',
    'codex_auth_snapshot_missing','trusted_auth_bindings_required','auth_private_state','auth_snapshot_path',
    'cli_missing','output_budget_unsupported','refusal','refused','stale','partial','schema_error'})


def _number(value):
    try:
        return type(value) in (int,float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def ensure_schema(conn):
    """Called only by Office store bootstrap, inside its existing transaction."""
    conn.execute('CREATE TABLE IF NOT EXISTS model_calls('
        'seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,'
        'generation INTEGER NOT NULL, turn_id TEXT NOT NULL, decision_id TEXT,'
        'requested_provider TEXT NOT NULL, requested_model TEXT NOT NULL, served_provider TEXT, served_model TEXT,'
        'started_at REAL, ended_at REAL, latency_s REAL, outcome TEXT NOT NULL, reason TEXT,'
        'input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, cache_creation_tokens INTEGER,'
        'cost_usd REAL, usage_state TEXT NOT NULL, cost_basis TEXT NOT NULL, invalid_metadata_n INTEGER)')
    if 'invalid_metadata_n' not in {r[1] for r in conn.execute('PRAGMA table_info(model_calls)')}:
        conn.execute('ALTER TABLE model_calls ADD COLUMN invalid_metadata_n INTEGER')
    if 'served_model_basis' not in {r[1] for r in conn.execute('PRAGMA table_info(model_calls)')}:
        conn.execute('ALTER TABLE model_calls ADD COLUMN served_model_basis TEXT')
    conn.execute('CREATE TABLE IF NOT EXISTS handoff_events('
        'seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, at REAL NOT NULL,'
        'from_actor_id TEXT, to_actor_id TEXT, from_record_kind TEXT NOT NULL, from_record_id TEXT NOT NULL,'
        'to_record_kind TEXT NOT NULL, to_record_id TEXT NOT NULL, decision_id TEXT, order_id TEXT,'
        'kind TEXT NOT NULL, public_summary TEXT NOT NULL)')
    for table, columns in {
        'model_calls': [('actor_id, ended_at, call_id'), ('ended_at, call_id')],
        'handoff_events': [('at, event_id'), ('from_actor_id, at'), ('to_actor_id, at')],
    }.items():
        for fields in columns:
            conn.execute('CREATE INDEX IF NOT EXISTS '+table+'_'+fields.replace(', ','_')+' ON '+table+'('+fields+')')
        conn.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_update BEFORE UPDATE ON '+table+
                     " BEGIN SELECT RAISE(ABORT,'insert_only'); END")
    for table in TABLES:
        if 'epoch_id' not in {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}:
            conn.execute('ALTER TABLE '+table+' ADD COLUMN epoch_id TEXT')
    conn.execute('CREATE TABLE IF NOT EXISTS runtime_observation_state('
        'table_name TEXT PRIMARY KEY, consumed_seq INTEGER NOT NULL DEFAULT 0, retained_from REAL,'
        'pruned_n INTEGER NOT NULL DEFAULT 0, dropped_n INTEGER NOT NULL DEFAULT 0,'
        'receipt_seq INTEGER NOT NULL DEFAULT 0, ledger_seq INTEGER NOT NULL DEFAULT 0)')
    for table in TABLES:
        conn.execute('INSERT OR IGNORE INTO runtime_observation_state(table_name) VALUES(?)',(table,))


def _public(value, depth=0):
    if depth > 16:
        return False
    if isinstance(value,dict):
        return not any(value.get(k) is True for k in ('evaluation_derived','blind','private')) and all(
            _public(v,depth+1) for v in value.values())
    if isinstance(value,list):
        return all(_public(v,depth+1) for v in value)
    return True


def _receipt(conn, key):
    if not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('handoff_reference_invalid')
    row=conn.execute('SELECT substr(payload_json,1,65537),prev_hash,row_hash FROM receipts WHERE row_hash=?',(key,)).fetchone()
    if row is None or len(row[0].encode())>65536:
        raise ValueError('handoff_reference_unknown')
    doc=json.loads(row[0])
    if (hashlib.sha256(S.canonical_json(doc)+row[1].encode()).hexdigest()!=key or not _public(doc)
            or doc.get('outcome','success')!='success'):
        raise ValueError('handoff_reference_unproven')
    return doc


def _at(doc):
    value=doc.get('known_at')
    if value is None and isinstance(doc.get('cut'),dict):
        value=doc['cut'].get('receipt_cut')
    if not _number(value):
        raise ValueError('handoff_time_unknown')
    return value


def validate_refs(conn, refs, *, before, supplied=None):
    """Typed refs are host-verified immutable receipt identities, never prose links."""
    if not _number(before) or not isinstance(refs,list) or len(refs)>32:
        raise ValueError('handoff_reference_invalid')
    checked=[]
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)!={'kind','receipt_id'}:
            raise ValueError('handoff_reference_invalid')
        if supplied is not None and ref not in supplied:
            raise ValueError('handoff_reference_not_supplied')
        doc=_receipt(conn,ref['receipt_id'])
        if REF_KINDS.get(doc.get('kind',doc.get('tool')))!=ref['kind'] or _at(doc)>before:
            raise ValueError('handoff_reference_unproven')
        if ref not in checked:
            checked.append(dict(ref))
    return checked


def _insert(conn, table, row, *, key, identity):
    prior=conn.execute('SELECT '+','.join(identity)+' FROM '+table+' WHERE '+key+'=?',(row[key],)).fetchone()
    if prior is not None:
        if tuple(prior)!=tuple(row[k] for k in identity):
            raise ValueError('observation_identity_conflict')
        return
    keys=tuple(row)
    conn.execute('INSERT INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',
                 tuple(row[k] for k in keys))


def mark_consumed(conn, table_name, seq):
    """Join the reporting writer's transaction; never commit its daily rollup."""
    if table_name not in TABLES or type(seq)is not int or seq<0:
        raise ValueError('observation_watermark_invalid')
    current=conn.execute('SELECT consumed_seq FROM runtime_observation_state WHERE table_name=?',(table_name,)).fetchone()[0]
    if seq<=current:
        return
    last=conn.execute('SELECT seq FROM '+table_name+' ORDER BY seq DESC LIMIT 1').fetchone()
    if last is None or seq>last[0]:
        raise ValueError('observation_watermark_future')
    conn.execute('UPDATE runtime_observation_state SET consumed_seq=? WHERE table_name=?',(seq,table_name))


def retention_cutoff(conn,now):
    # shortcut: refuse retention above 128 epochs; add a validated indexed epoch projection if reached.
    epochs=conn.execute('SELECT payload_json FROM epochs LIMIT 129').fetchall()
    if len(epochs)>128:
        return None
    cutoff=now-RETENTION_S
    try:
        for row in epochs:
            doc=json.loads(row[0])
            if not _number(doc.get('opened_at')) or 'closed_at' not in doc:
                return None
            if doc['closed_at'] is None:
                cutoff=min(cutoff,doc['opened_at'])
            elif not _number(doc['closed_at']) or doc['closed_at']<doc['opened_at']:
                return None
    except (ValueError,TypeError):
        return None
    return cutoff


def prune(conn, *, now, limit=200):
    """Bounded expiry outside OPEN epochs and only after the daily writer consumed a row."""
    if not _number(now) or type(limit)is not int or not 1<=limit<=200:
        raise ValueError('observation_retention_invalid')
    removed={table:0 for table in TABLES}
    with S.write_transaction(conn):
        cutoff=retention_cutoff(conn,now)
        if cutoff is None:return removed
        for table, at in TABLES.items():
            consumed=conn.execute('SELECT consumed_seq FROM runtime_observation_state WHERE table_name=?',(table,)).fetchone()[0]
            cursor=conn.execute('DELETE FROM '+table+' WHERE seq IN (SELECT seq FROM '+table+
                ' WHERE '+at+'<? AND seq<=? ORDER BY '+at+', '+('call_id' if table=='model_calls' else 'event_id')+' LIMIT ?)',
                (cutoff,consumed,limit))
            removed[table]=cursor.rowcount
            if cursor.rowcount:
                first=conn.execute('SELECT '+at+' FROM '+table+' WHERE '+at+' IS NOT NULL ORDER BY '+at+' LIMIT 1').fetchone()
                conn.execute('UPDATE runtime_observation_state SET retained_from=?,pruned_n=pruned_n+? WHERE table_name=?',
                             (first[0] if first else cutoff,cursor.rowcount,table))
    return removed


class Buffer:
    """Single supervisor owner. Capture is memory-only; SQLite and retention run after turns."""
    def __init__(self, conn, *, ledger=None, clock, max_pending=1024):
        if type(max_pending)is not int or max_pending<1:
            raise ValueError('observation_buffer_limit')
        self.conn,self.ledger,self.clock=conn,ledger,clock
        self.pending=deque()
        self.max_pending=max_pending
        self.dropped_n=conn.execute("SELECT dropped_n FROM runtime_observation_state WHERE table_name='model_calls'").fetchone()[0]
        self.last_error=None
        self.next_prune=0.

    def health(self):
        return dict(state='partial' if self.last_error or self.dropped_n else 'observed_only',
                    pending_n=len(self.pending),dropped_n=self.dropped_n,last_error=self.last_error)

    def capture_call(self, context, result, *, call_id):
        if len(self.pending)>=self.max_pending:
            self.dropped_n+=1
            return
        usage=result.usage if isinstance(result.usage,dict) else {}
        def count(name):
            v=usage.get(name)
            return v if type(v)is int and 0<=v<=9223372036854775807 else None
        invalid=getattr(result,'invalid_metadata_n',0)
        invalid=invalid if type(invalid) is int and 0<=invalid<=5 else 1
        invalid+=sum(k in usage and count(k) is None for k in ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'))
        counters=[count(k) for k in ('input_tokens','output_tokens','cache_read_input_tokens')]
        served=result.served_model
        if served is not None and (not isinstance(served,str) or not 1<=len(served)<=128 or not served.isascii()
                or not served.startswith(('claude-','gpt-')) or served in ('claude-','gpt-')
                or not all(c.isalnum() or c in '._-' for c in served) or redact_text(served)!=served):
            served=None
            invalid+=1
        cost=result.cost_usd
        if cost is not None and (not _number(cost) or type(cost) is int and cost>9223372036854775807):
            cost=None
            invalid+=1
        cost=float(cost) if cost is not None and result.cost_basis in ('billed_receipt','provider_reported') else None
        provider=('claude' if isinstance(served,str) and served.startswith('claude-') else
                  'codex' if isinstance(served,str) and served.startswith('gpt-') else None)
        reason=result.error_reason
        row=dict(call_id=call_id,actor_id=context.actor_id,generation=context.generation,turn_id=context.turn_id,
            decision_id=context.decision_id,epoch_id=getattr(context,'epoch_id',None),requested_provider=result.requested_provider,
            requested_model=result.requested_model,served_provider=provider,served_model=served,
            served_model_basis=getattr(result,'served_model_basis','unknown') if getattr(result,'served_model_basis','unknown') in ('echoed','requested_pinned') else 'unknown',
            started_at=result.started_at if _number(result.started_at) else None,
            ended_at=result.ended_at if _number(result.ended_at) else None,
            latency_s=result.latency_s if _number(result.latency_s) else None,outcome=result.outcome,
            reason=reason if reason in SAFE_REASONS else 'unclassified_error' if reason is not None else None,
            input_tokens=counters[0],output_tokens=counters[1],cached_tokens=counters[2],
            cache_creation_tokens=count('cache_creation_input_tokens'),cost_usd=cost,invalid_metadata_n=invalid,
            usage_state='known' if all(v is not None for v in counters) else 'partial' if any(v is not None for v in counters) else 'unknown',
            cost_basis=result.cost_basis if cost is not None else 'subscription_unmeasured' if result.billing_basis=='subscription' else 'unknown')
        self.pending.append(row)

    def _handoff(self, row):
        _insert(self.conn,'handoff_events',row,key='event_id',identity=tuple(row))

    def _receipts(self, now):
        cursor=self.conn.execute("SELECT receipt_seq FROM runtime_observation_state WHERE table_name='handoff_events'").fetchone()[0]
        rows=self.conn.execute('SELECT seq,row_hash,substr(payload_json,1,65537) FROM receipts WHERE seq>? ORDER BY seq LIMIT 200',(cursor,)).fetchall()
        for seq,key,raw in rows:
            doc=json.loads(raw)
            candidates=doc.get('refs',doc.get('source_refs',[]))
            refs=[r for r in candidates if isinstance(r,dict) and set(r)=={'kind','receipt_id'}] if isinstance(candidates,list) else []
            if refs and _public(doc) and doc.get('outcome','success')=='success':
                target=_receipt(self.conn,key)
                at=_at(target)
                if at>now:
                    raise ValueError('handoff_future')
                for ref in validate_refs(self.conn,refs,before=at):
                    source=_receipt(self.conn,ref['receipt_id'])
                    kind='memo_to_judge' if ref['kind']=='memo' and target.get('role')=='judge' else ref['kind']+'_response'
                    event_id=hashlib.sha256(S.canonical_json([key,ref['receipt_id'],kind])).hexdigest()
                    self._handoff(dict(event_id=event_id,at=at,from_actor_id=source.get('actor_id'),
                        to_actor_id=target.get('actor_id'),from_record_kind='receipts',from_record_id=ref['receipt_id'],
                        to_record_kind='receipts',to_record_id=key,decision_id=target.get('decision_id'),order_id=None,
                        kind=kind,epoch_id=target.get('epoch_id'),public_summary='Recorded '+ref['kind']+' response'))
            cursor=seq
        self.conn.execute("UPDATE runtime_observation_state SET receipt_seq=? WHERE table_name='handoff_events'",(cursor,))

    def _orders(self, now):
        if self.ledger is None:
            return
        cursor=self.conn.execute("SELECT ledger_seq FROM runtime_observation_state WHERE table_name='handoff_events'").fetchone()[0]
        rows=self.ledger.execute('SELECT rowid AS seq,* FROM decision_receipts WHERE rowid>? ORDER BY rowid LIMIT 200',(cursor,)).fetchall()
        for receipt in rows:
            if receipt['order_id'] is not None:
                order=self.ledger.execute('SELECT book_id,decision_bundle_hash FROM orders WHERE order_id=?',(receipt['order_id'],)).fetchone()
                if (order is None or tuple(order)!=(receipt['book_id'],receipt['bundle_hash'])
                        or receipt['outcome']!='submitted' or not _number(receipt['created_at']) or receipt['created_at']>now):
                    raise ValueError('decision_order_unproven')
                key=receipt['action_key']
                self._handoff(dict(event_id=hashlib.sha256(S.canonical_json(['decision_to_order',key])).hexdigest(),
                    at=receipt['created_at'],from_actor_id=receipt['actor_id'],to_actor_id=None,
                    from_record_kind='decision_receipts',from_record_id=key,to_record_kind='orders',
                    to_record_id=receipt['order_id'],decision_id=receipt['decision_id'],order_id=receipt['order_id'],
                    kind='decision_to_order',epoch_id=receipt['epoch_id'] if 'epoch_id' in receipt.keys() else None,public_summary='Recorded decision to paper order'))
            cursor=receipt['seq']
        self.conn.execute("UPDATE runtime_observation_state SET ledger_seq=? WHERE table_name='handoff_events'",(cursor,))

    def flush(self):
        """Bounded off-dispatch write. Failed batches stay pending and are visibly unknown."""
        try:
            now=self.clock()
            if not _number(now):
                raise ValueError('observation_clock_unknown')
            count=len(self.pending)
            with S.write_transaction(self.conn):
                for row in self.pending:
                    _insert(self.conn,'model_calls',row,key='call_id',identity=('actor_id','generation','turn_id','decision_id','epoch_id','requested_provider','requested_model'))
                self._receipts(now)
                self._orders(now)
                self.conn.execute("UPDATE runtime_observation_state SET dropped_n=? WHERE table_name='model_calls'",(self.dropped_n,))
            for _ in range(count):
                self.pending.popleft()
            self.last_error=None
            if time.monotonic()>=self.next_prune:
                prune(self.conn,now=now)
                self.next_prune=time.monotonic()+3600.
        except (sqlite3.Error,ValueError,TypeError,KeyError,RecursionError):
            self.last_error='observation_flush_failed'
        return self.health()
