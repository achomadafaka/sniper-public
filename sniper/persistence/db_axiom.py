"""Axiom observation tables (spec 2026-09-16-axiom-observation-lanes-design.md, section 4.4).

Raw capture first, parsed tables second. Every row carries the LOCAL receipt clock
(`received_mono_ns` from time.monotonic_ns) so an arrival comparison never reads a provider's
own timestamp as a clock. Nothing in this module is read by the funded entry path.
"""
from __future__ import annotations

import sqlite3
import json
import hashlib
from datetime import datetime, timedelta, timezone

RAW_TABLE = "axiom_raw_messages"
RAW_SCHEMA = """
    CREATE TABLE IF NOT EXISTS {s}axiom_raw_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_mono_ns INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        transport TEXT NOT NULL,            -- 'ws' | 'rest'
        source TEXT NOT NULL,               -- socket URL or REST URL (query stripped)
        page TEXT,                          -- which capture page produced it
        payload TEXT NOT NULL,
        byte_len INTEGER NOT NULL,
        body_sha TEXT                       -- REST only: dedupes a catalogue that repeats per reload
    );
    CREATE INDEX IF NOT EXISTS {s}idx_axiom_raw_received ON axiom_raw_messages(received_at);
"""
RAW_SHA_INDEX = "CREATE INDEX IF NOT EXISTS {s}idx_axiom_raw_sha ON axiom_raw_messages(source, body_sha)"


def _raw_schema(conn, schema):
    """CREATE, then add `body_sha` to a table created before the column existed (first live run,
    2026-09-16), then the sha index. Order matters: the index needs the column."""
    conn.executescript(RAW_SCHEMA.format(s=schema))
    cols = {r[1] for r in conn.execute(f"PRAGMA {schema}table_info(axiom_raw_messages)")}
    if "body_sha" not in cols:
        conn.execute(f"ALTER TABLE {schema}axiom_raw_messages ADD COLUMN body_sha TEXT")
    conn.execute(RAW_SHA_INDEX.format(s=schema))


def attach_raw_store(conn: sqlite3.Connection, path) -> None:
    """Raw frames go to their OWN file (schema `axraw`), never the funded ledger: a reload can
    add megabytes, and the ledger is 11 GB with a 7-day retention on this table."""
    conn.execute("ATTACH DATABASE ? AS axraw", (str(path),))
    _raw_schema(conn, "axraw.")


def _raw_table(conn):
    names = {r[1] for r in conn.execute("PRAGMA database_list")}
    return ("axraw." + RAW_TABLE) if "axraw" in names else RAW_TABLE


def ensure_axiom_tables(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("ensure_axiom_tables must run outside a caller transaction")
    _raw_schema(conn, "")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS axiom_trader_directory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        x_handle TEXT NOT NULL,
        display TEXT,
        address TEXT NOT NULL,
        chain TEXT NOT NULL,
        pnl_24h REAL, trades INTEGER, volume REAL, followers INTEGER,
        seen_at TEXT NOT NULL,
        UNIQUE(x_handle, chain, address)
    );
    CREATE TABLE IF NOT EXISTS axiom_verified_addresses (
        source_user_id TEXT NOT NULL,
        handle TEXT,
        x_handle TEXT,
        network_id TEXT NOT NULL,
        address TEXT NOT NULL,
        verify_status TEXT NOT NULL,        -- 'axiom_confirmed' | 'REFUTED' | 'unverified'
        matches INTEGER, contradictions INTEGER, no_data INTEGER, checked INTEGER,
        verified_at TEXT NOT NULL,
        detail TEXT,
        UNIQUE(source_user_id, network_id)
    );
    CREATE INDEX IF NOT EXISTS idx_axiom_verified_status
        ON axiom_verified_addresses(verify_status);
    CREATE TABLE IF NOT EXISTS j7_receipts (
        fomo_event_id TEXT PRIMARY KEY,
        received_mono_ns INTEGER NOT NULL,
        received_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS axiom_wallet_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_mono_ns INTEGER NOT NULL,
        received_at TEXT NOT NULL,
        address TEXT NOT NULL, chain TEXT NOT NULL, mint TEXT NOT NULL,
        side TEXT NOT NULL, amount_usd REAL, tx_sig TEXT, axiom_ts TEXT,
        raw_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_axiom_events_sig ON axiom_wallet_events(tx_sig);
    CREATE TABLE IF NOT EXISTS axiom_discovery_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seen_at TEXT NOT NULL, chain TEXT NOT NULL, mint TEXT NOT NULL, pair TEXT,
        mcap REAL, liquidity REAL, volume_m5 REAL, age_s REAL, filter_set TEXT
    );
    CREATE TABLE IF NOT EXISTS axiom_kol_callouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seen_at TEXT NOT NULL, kol_handle TEXT, mint TEXT, chain TEXT,
        called_mcap REAL, text TEXT, raw_id INTEGER
    );
    """)
    with conn:
        from sniper.persistence.db_axiom_research import ensure_research_tables
        ensure_research_tables(conn)
        additions = {
            'axiom_trader_directory': {'source': 'TEXT', 'metrics_seen_at': 'TEXT'},
            'axiom_wallet_events': {
                'transport': 'TEXT', 'observation_kind': "TEXT NOT NULL DEFAULT 'unknown'",
                'clock_domain': 'TEXT', 'capture_run_id': 'TEXT', 'parser_version': 'TEXT'},
            'j7_receipts': {'clock_domain': 'TEXT', 'capture_run_id': 'TEXT', 'identity_json': 'TEXT'},
            'axiom_discovery_tokens': {'mcap_unit': 'TEXT', 'liquidity_unit': 'TEXT',
                'volume_value': 'REAL', 'volume_unit': 'TEXT', 'volume_window_s': 'INTEGER',
                'parser_version': 'TEXT'},
            'axiom_kol_callouts': {'event_key': 'TEXT', 'last_seen_at': 'TEXT', 'parser_version': 'TEXT',
                'source_id': 'TEXT', 'source_kind': 'TEXT', 'called_at': 'TEXT', 'identity_kind': 'TEXT'},
        }
        for table, fields in additions.items():
            existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name, declaration in fields.items():
                if name not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        # Legacy history has no reliable transport or clock identity.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_axiom_events_identity_v2'").fetchone():
            conn.execute('DROP INDEX IF EXISTS idx_axiom_events_uniq')
            conn.execute("DELETE FROM axiom_wallet_events WHERE tx_sig IS NOT NULL AND id NOT IN "
                         "(SELECT MIN(id) FROM axiom_wallet_events WHERE tx_sig IS NOT NULL "
                         "GROUP BY chain,tx_sig,address,side,mint,observation_kind)")
            conn.execute('CREATE UNIQUE INDEX idx_axiom_events_identity_v2 ON axiom_wallet_events '
                         '(chain,tx_sig,address,side,mint,observation_kind) WHERE tx_sig IS NOT NULL')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_axiom_kol_event_key '
                     'ON axiom_kol_callouts(event_key) WHERE event_key IS NOT NULL')


def insert_raw(conn, *, received_mono_ns, received_at, transport, source, page, payload, body_sha=None):
    """Returns the new row id, or None when a REST body with this sha already sits under this source
    (catalogues repeat on every reload; the first copy is enough)."""
    t = _raw_table(conn)
    if body_sha and conn.execute(
            f"SELECT 1 FROM {t} WHERE source=? AND body_sha=? LIMIT 1", (source, body_sha)).fetchone():
        return None
    cur = conn.execute(
        f"INSERT INTO {t}(received_mono_ns, received_at, transport, source, page, payload, byte_len, "
        "body_sha) VALUES (?,?,?,?,?,?,?,?)",
        (int(received_mono_ns), received_at, transport, source, page, payload, len(payload.encode("utf-8", "replace")), body_sha))
    return cur.lastrowid


def upsert_directory(conn, rows, seen_at):
    """Rows from parse_trader_directory; a row without a full address is skipped, not stored."""
    n = 0
    for r in rows:
        if not r.get("address") or r.get("truncated") or not r.get("x_handle"):
            continue
        # Axiom keeps ONE generic EVM address per trader, not one per EVM chain (Beta, from the
        # smart-money-traders payload). Such a row lands as chain 'evm'; the verifier then tests
        # that address on 4663, 56 and 8453 separately.
        chain = r.get("chain") or "evm"
        conn.execute(
            "INSERT INTO axiom_trader_directory(x_handle, display, address, chain, pnl_24h, trades, "
            "volume, followers, seen_at, source, metrics_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(x_handle, chain, address) DO UPDATE SET display=COALESCE(excluded.display,axiom_trader_directory.display), "
            "pnl_24h=COALESCE(excluded.pnl_24h,axiom_trader_directory.pnl_24h), trades=COALESCE(excluded.trades,axiom_trader_directory.trades), volume=COALESCE(excluded.volume,axiom_trader_directory.volume), "
            "followers=COALESCE(excluded.followers,axiom_trader_directory.followers), seen_at=excluded.seen_at, source=excluded.source, "
            "metrics_seen_at=COALESCE(excluded.metrics_seen_at,axiom_trader_directory.metrics_seen_at)",
            (r["x_handle"], r.get("display"), r["address"], chain, r.get("pnl_24h"),
             r.get("trades"), r.get("volume"), r.get("followers"), seen_at, r.get("source", "smart-money-traders"),
             seen_at if any(r.get(k) is not None for k in ("pnl_24h","trades","volume","followers")) else None))
        n += 1
    return n


def record_j7_receipts(conn, rows):
    """Store each relay's LOCAL monotonic arrival. Returns how many were new.

    A replay of the same relay keeps the FIRST stamp: the arrival we are measuring happened once.
    """
    stored = 0
    for row in rows or []:
        cur = conn.execute(
            "INSERT OR IGNORE INTO j7_receipts(fomo_event_id,received_mono_ns,received_at,"
            "clock_domain,capture_run_id,identity_json) VALUES (?,?,?,?,?,?)",
            (str(row["fomo_event_id"]), int(row["received_mono_ns"]), str(row["received_at"]),
             row.get("clock_domain"), row.get("capture_run_id"),
             json.dumps(row["identity_json"], sort_keys=True) if isinstance(row.get("identity_json"), dict)
             else row.get("identity_json")))
        stored += cur.rowcount
    conn.commit()
    return stored


def insert_wallet_event(conn, ev, *, received_mono_ns, received_at, raw_id=None,
                        transport=None, observation_kind='unknown', clock_domain=None,
                        capture_run_id=None, parser_version=None):
    """Preserve the first observation in each chain and observation class."""
    if observation_kind not in {'unknown', 'history', 'live'}:
        raise ValueError('Invalid observation kind')
    if observation_kind == 'live' and transport != 'ws':
        raise ValueError('Live Axiom events require WebSocket provenance')
    cur = conn.execute(
        "INSERT OR IGNORE INTO axiom_wallet_events(received_mono_ns,received_at,address,chain,mint,side,"
        "amount_usd,tx_sig,axiom_ts,raw_id,transport,observation_kind,clock_domain,capture_run_id,parser_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(received_mono_ns),received_at,ev['address'],ev['chain'],ev['mint'],ev['side'],
         ev.get('amount_usd'),ev.get('tx_sig'),ev.get('axiom_ts'),raw_id,transport,observation_kind,
         clock_domain,capture_run_id,parser_version))
    return cur.rowcount


def insert_discovery(conn, rows, seen_at):
    rows = [r for r in rows if r.get('chain') and r.get('mint')]
    fields = ('chain','mint','pair','mcap','liquidity','volume_m5','age_s','filters',
              'mcap_unit','liquidity_unit','volume_value','volume_unit','volume_window_s','parser_version')
    conn.executemany(
        'INSERT INTO axiom_discovery_tokens(seen_at,chain,mint,pair,mcap,liquidity,volume_m5,'
        'age_s,filter_set,mcap_unit,liquidity_unit,volume_value,volume_unit,volume_window_s,parser_version) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(seen_at, *(r.get(k) for k in fields)) for r in rows])
    return len(rows)


def insert_kol_callouts(conn, rows, seen_at, raw_id=None):
    stored = 0
    for row in rows:
        if row.get('source_id') is not None:
            identity = [row.get('source_kind'), str(row['source_id'])]
            identity_kind = 'source_id'
        elif row.get('called_at'):
            identity = [row.get(k) for k in ('source_kind','kol_handle','mint','chain','called_at')]
            identity_kind = 'source_time'
        else:
            identity = row.get('payload_sha') or {k: row.get(k) for k in
                       ('source_kind','kol_handle','mint','chain','called_mcap','text')}
            identity_kind = 'unique_payload'
        key = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        cur = conn.execute(
            'INSERT INTO axiom_kol_callouts(seen_at,kol_handle,mint,chain,called_mcap,text,raw_id,'
            'event_key,last_seen_at,parser_version,source_id,source_kind,called_at,identity_kind) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(event_key) WHERE event_key IS NOT NULL DO UPDATE SET last_seen_at=excluded.last_seen_at',
            (seen_at,row.get('kol_handle'),row.get('mint'),row.get('chain'),row.get('called_mcap'),
             row.get('text'),raw_id,key,seen_at,'axiom-callout-v2',row.get('source_id'),
             row.get('source_kind'),row.get('called_at'),identity_kind))
        stored += cur.rowcount
    return stored


def raw_id_for_body(conn, source, body_sha):
    row=conn.execute(f'SELECT id FROM {_raw_table(conn)} WHERE source=? AND body_sha=? ORDER BY id LIMIT 1',
                     (source,body_sha)).fetchone()
    return row[0] if row else None


def prune_raw(conn, *, now_iso, retention_days, max_bytes=2_000_000_000):
    """Bound retained payload bytes. SQLite reuses free pages; this does not run VACUUM."""
    if max_bytes <= 0:
        raise ValueError('Raw byte limit must be positive')
    table = _raw_table(conn)
    cutoff = (datetime.fromisoformat(now_iso.replace('Z','+00:00'))
              - timedelta(days=float(retention_days))).isoformat()
    deleted = conn.execute(f'DELETE FROM {table} WHERE received_at < ?', (cutoff,)).rowcount
    total = conn.execute(f'SELECT COALESCE(SUM(byte_len),0) FROM {table}').fetchone()[0]
    if total > max_bytes:
        # Keep the newest complete payloads within the limit.
        boundary = conn.execute(f'SELECT MAX(id) FROM (SELECT id,SUM(byte_len) OVER '
                                f'(ORDER BY id DESC) AS retained FROM {table}) WHERE retained>?',
                                (max_bytes,)).fetchone()[0]
        if boundary is not None:
            deleted += conn.execute(f'DELETE FROM {table} WHERE id<=?', (boundary,)).rowcount
    return deleted
