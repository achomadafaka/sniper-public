"""Owned research ledger; no funded database handles or authority.

we_* tables are new and private to data/wallet_execution_research.db.
"""
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path
from sniper.research.wallet_execution.evidence import digest, episode_gate, PROTOCOL

DDL = '''
CREATE TABLE IF NOT EXISTS we_manifests(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS we_origins(origin_id TEXT PRIMARY KEY, wallet TEXT, chain TEXT,
 mint TEXT, observed_at TEXT, payload TEXT NOT NULL, raw_digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS we_origin_cell ON we_origins(wallet,chain,observed_at);
CREATE TABLE IF NOT EXISTS we_episodes(origin_id TEXT PRIMARY KEY, acquired_atomic TEXT,
 payload TEXT NOT NULL, raw_digest TEXT NOT NULL, eligibility TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS we_results(result_id TEXT PRIMARY KEY, origin_id TEXT,
 policy_id TEXT, payload TEXT NOT NULL, raw_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS we_raw(source TEXT, raw_id TEXT, payload TEXT NOT NULL,
 raw_digest TEXT NOT NULL, PRIMARY KEY(source,raw_id));
CREATE TABLE IF NOT EXISTS we_cursors(seq INTEGER PRIMARY KEY, source TEXT NOT NULL, offset INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS we_cursor_source ON we_cursors(source,seq DESC);
CREATE TABLE IF NOT EXISTS we_studies(study_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS we_study_reads(study_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS we_cells(wallet TEXT,chain TEXT,origins INTEGER NOT NULL DEFAULT 0,episodes INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(wallet,chain));
CREATE TABLE IF NOT EXISTS we_checkpoints(seq INTEGER PRIMARY KEY,source TEXT,offset INTEGER,identity TEXT,anchor TEXT,epoch TEXT);
CREATE INDEX IF NOT EXISTS we_checkpoint_source ON we_checkpoints(source,seq DESC);
CREATE TABLE IF NOT EXISTS we_source_gaps(seq INTEGER PRIMARY KEY,source TEXT,at TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS we_runs(id INTEGER PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
'''


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def open_store(path):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(p, timeout=2)
    c.row_factory=sqlite3.Row
    try:
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row[0].startswith('sqlite_')}
        if tables - set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', DDL)):
            raise ValueError('foreign research database refused')
        c.executescript(DDL); c.commit()
    except Exception:
        c.close(); raise
    return c


def freeze_manifest(db, manifest):
    key=digest(manifest)
    db.execute('INSERT OR IGNORE INTO we_manifests VALUES (?,?)', (key, encoded(manifest)))
    return key


def coverage_counts(db):
    """Archive counts by wallet/chain; naive or missing clocks cannot add UTC days."""
    return [dict(r) for r in db.execute('''
        SELECT c.wallet,c.chain,c.origins,c.episodes,count(DISTINCT o.mint) AS distinct_coins,
        count(DISTINCT CASE WHEN o.observed_at LIKE '%Z' OR o.observed_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]'
                           THEN date(o.observed_at) END) AS observed_days,
        sum(CASE WHEN o.origin_id IS NOT NULL AND (date(o.observed_at) IS NULL OR
            NOT (o.observed_at LIKE '%Z' OR o.observed_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]'))
            THEN 1 ELSE 0 END) AS unknown_clock_buys
        FROM we_cells c LEFT JOIN we_origins o ON o.wallet=c.wallet AND o.chain=c.chain
        GROUP BY c.wallet,c.chain
    ''')]


def record_origin(db, source):
    # A repeated platform buy remains in the denominator; grouping into buy
    # episodes requires separate verified source transactions, never UID/mint alone.
    key=digest({'feed_id': source['upstream_event_id']})
    raw=encoded(source); h=digest(source)
    old=db.execute('SELECT raw_digest FROM we_origins WHERE origin_id=?',(key,)).fetchone()
    if old and old[0]!=h: raise ValueError('immutable origin changed')
    from sniper.services.memecoin_chains import canonical_chain_id
    chain=canonical_chain_id(source.get('network_id')) or str(source.get('network_id') or 'unknown')
    db.execute('INSERT OR IGNORE INTO we_origins VALUES (?,?,?,?,?,?,?)',
               (key, source.get('source_user_id'), chain,
                source.get('mint'), source.get('observed_at'), raw,h))
    if not old:
        db.execute('INSERT INTO we_cells(wallet,chain,origins) VALUES (?,?,1) ON CONFLICT(wallet,chain) DO UPDATE SET origins=origins+1',(source.get('source_user_id'),chain))
    return key


def ingest_episode(db, episode):
    origin=episode['origin_id']; h=digest(episode)
    old=db.execute('SELECT raw_digest FROM we_episodes WHERE origin_id=?',(origin,)).fetchone()
    if old and old[0]!=h: raise ValueError('immutable episode changed; use a new versioned origin')
    gate=episode_gate(episode)
    db.execute('INSERT OR IGNORE INTO we_episodes VALUES (?,?,?,?,?)',
               (origin, str((episode.get('source') or {}).get('acquired_atomic')) if isinstance(episode.get('source'),dict) else None, encoded(episode), h, encoded(gate.as_dict())))
    if not old and gate.eligible:
        db.execute('INSERT INTO we_cells(wallet,chain,episodes) VALUES (?,?,1) ON CONFLICT(wallet,chain) DO UPDATE SET episodes=episodes+1',(episode['wallet'],episode['chain']))
    return gate


def record_result(db, result):
    h=digest(result)
    db.execute('INSERT OR IGNORE INTO we_results VALUES (?,?,?,?,?)',
               (h,result['origin_id'],result['policy_id'],encoded(result),h))
    return h


def record_raw(db, source, raw_id, row):
    h=digest(row)
    db.execute('INSERT OR IGNORE INTO we_raw VALUES (?,?,?,?)',(source,str(raw_id),encoded(row),h))
    old=db.execute('SELECT raw_digest FROM we_raw WHERE source=? AND raw_id=?',(source,str(raw_id))).fetchone()
    if old and old[0]!=h: raise ValueError('immutable archived source changed')


def cursor(db, source):
    row=db.execute('SELECT offset FROM we_cursors WHERE source=? ORDER BY seq DESC LIMIT 1',(source,)).fetchone()
    return row[0] if row else None


def advance_cursor(db, source, offset):
    prior=cursor(db,source)
    if prior is not None and offset < prior: raise ValueError('cursor cannot rewind')
    if offset!=prior:
        db.execute('INSERT INTO we_cursors(source,offset) VALUES (?,?)',(source,int(offset)))


def checkpoint(db,source):
    row=db.execute('SELECT * FROM we_checkpoints WHERE source=? ORDER BY seq DESC LIMIT 1',(source,)).fetchone()
    return dict(row) if row else None


def record_checkpoint(db,source,offset,identity,anchor,epoch):
    db.execute('INSERT INTO we_checkpoints(source,offset,identity,anchor,epoch) VALUES (?,?,?,?,?)',(source,offset,identity,anchor,epoch))


def dossier_gate_inventory():
    from sniper.persistence.db_memecoin import EXEMPTIBLE_GATE_FAMILIES
    return EXEMPTIBLE_GATE_FAMILIES | {'route', 'safety', 'exposure', 'loss', 'concurrency',
                                     'duplicate', 'sizing', 'execution_premium', 'route_over_mid'}


def freeze_dossier_baseline(db, baseline, *, now):
    """Explicit paper baseline. This never reads or changes funded settings."""
    from sniper.research.wallet_execution.evidence import number
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(baseline.get('chain'))
    defaults = baseline.get('defaults')
    gates = baseline.get('required_gates')
    policy = baseline.get('policy')
    allowed = {'policy_id', 'entry', 'clip_usd', 'max_hold_s', 'tp_pct', 'sl_pct',
               'trail_pct', 'trail_arm_pct', 'mirror', 'max_wait_s', 'premium_cap_pct'}
    if (chain is None or chain != baseline.get('chain') or not isinstance(defaults, dict) or not defaults
        or any(value is None for value in defaults.values()) or not isinstance(gates, list)
        or not all(isinstance(k, str) and k in defaults for k in gates)
        or len(set(gates)) != len(gates) or not dossier_gate_inventory() <= set(gates)
        or set(defaults) != set(gates)
        or not isinstance(policy, dict) or set(policy) - allowed
        or not isinstance(policy.get('mirror', False), bool)
        or not policy.get('policy_id') or policy.get('entry') != 'immediate'
        or not number(policy.get('clip_usd'), positive=True)
        or not number(policy.get('max_hold_s'), positive=True)
        or any(not number(policy.get(k, 0)) for k in ('tp_pct', 'sl_pct', 'trail_pct', 'trail_arm_pct'))):
        raise ValueError('complete defaults and supported whole-position policy required')
    from datetime import datetime
    if datetime.fromisoformat(now).tzinfo is None: raise ValueError('aware seal time required')
    key = digest(baseline)
    payload = {**baseline, 'id': key, 'sealed_at': now, 'defaults_digest': digest(defaults),
               'protocol': 'wallet-fast-move-v1', 'funded_authority': False}
    record_raw(db, 'wallet_dossier_baseline:'+chain, key, payload)
    return key


def append_dossier_note(db, *, wallet, chain, text, now):
    from uuid import uuid4
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('note must contain 1 to 4000 characters')
    note = {'id': uuid4().hex, 'wallet': wallet, 'chain': chain, 'at': now, 'text': text.strip()}
    record_raw(db, f'wallet_dossier_notes:{chain}:{wallet}', note['id'], note)
    return note


def record_dossier_admission(db, receipt, *, now):
    """Own-store input receipt, immutable per origin; preserve failed diagnostics."""
    from datetime import datetime
    from sniper.research.wallet_execution.evidence import dossier_admission_gate
    if len(encoded(receipt).encode()) > 65536: raise ValueError('admission receipt exceeds 64 KiB')
    body = receipt.get('evidence') if isinstance(receipt, dict) else None
    if not isinstance(body, dict): raise ValueError('admission evidence required')
    origin = db.execute('SELECT * FROM we_origins WHERE origin_id=?', (body.get('origin_id'),)).fetchone()
    if origin is None or any(body.get(k) != origin[k] for k in ('wallet', 'chain', 'mint')):
        raise ValueError('admission origin identity mismatch')
    baseline = db.execute('SELECT payload FROM we_raw WHERE source=? ORDER BY rowid DESC LIMIT 1',
                          ('wallet_dossier_baseline:'+origin['chain'],)).fetchone()
    stamp = datetime.fromisoformat(now)
    if stamp.tzinfo is None: raise ValueError('aware admission clock required')
    gate = dossier_admission_gate(receipt, json.loads(baseline[0]) if baseline else None, dict(origin), now=stamp.timestamp())
    record_raw(db, 'wallet_dossier_admission_v2', origin['origin_id'], receipt)
    return gate


def dossier_rows(db, *, wallet, chain, start, end):
    """Bounded own-ledger read. Episode bodies are fetched only after receipt checks."""
    baseline = db.execute('SELECT payload FROM we_raw WHERE source=? ORDER BY rowid DESC LIMIT 1',
                          ('wallet_dossier_baseline:'+chain,)).fetchone()
    notes = [json.loads(r[0]) for r in db.execute('SELECT payload FROM we_raw WHERE source=? ORDER BY rowid DESC LIMIT 50',
             (f'wallet_dossier_notes:{chain}:{wallet}',))]
    scope = (wallet, chain, start, end)
    where = 'o.wallet=? AND o.chain=? AND unixepoch(o.observed_at)>=? AND unixepoch(o.observed_at)<=?'
    counts = db.execute('SELECT count(*),count(DISTINCT mint),min(unixepoch(observed_at)),max(unixepoch(observed_at)) FROM we_origins o WHERE ' + where, scope).fetchone()
    rows = [dict(r) for r in db.execute('SELECT o.origin_id,o.mint,o.observed_at,e.eligibility,'
            "CASE WHEN length(a.payload)<=65536 THEN a.payload WHEN a.payload IS NULL THEN json_extract(e.payload,'$.dossier') END AS receipt,"
            "json_extract(e.payload,'$.source.received_at') AS received_at,"
            "json_extract(e.payload,'$.wallet') AS episode_wallet,json_extract(e.payload,'$.chain') AS episode_chain,"
            "json_extract(e.payload,'$.mint') AS episode_mint,length(e.payload) AS episode_bytes "
            "FROM we_origins o LEFT JOIN we_episodes e ON e.origin_id=o.origin_id LEFT JOIN we_raw a ON a.source='wallet_dossier_admission_v2' AND a.raw_id=o.origin_id WHERE " + where + ' ORDER BY o.observed_at DESC,o.origin_id LIMIT 200', scope)]
    run = db.execute('SELECT at,payload FROM we_runs ORDER BY id DESC LIMIT 1').fetchone()
    first = db.execute('SELECT min(unixepoch(at)) FROM we_runs').fetchone()[0]
    clock_unknown = db.execute('SELECT count(*) FROM we_origins WHERE wallet=? AND chain=? AND unixepoch(observed_at) IS NULL',
                               (wallet, chain)).fetchone()[0]
    return {'baseline': json.loads(baseline[0]) if baseline else None, 'notes': notes,
            'rows': rows, 'counts': tuple(counts), 'first_run': first, 'clock_unknown': clock_unknown,
            'run': dict(run) if run else None}


def dossier_episode(db, origin_id):
    row = db.execute('SELECT payload FROM we_episodes WHERE origin_id=? AND length(payload)<=5000000', (origin_id,)).fetchone()
    return json.loads(row[0]) if row else None
