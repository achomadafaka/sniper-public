"""Separate research ledger. Never opens or migrates the funded ledger."""
import json
import sqlite3
from pathlib import Path

FIELDS = ('origin','uid','chain','address','signature','event_index','mint','action',
          'quantity_atomic','quote_atomic','source_at','received_at','state','setup_json','payload_json')


def open_observation_db(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path)
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0,1):
        conn.close()
        raise ValueError('unsupported research schema')
    allowed={'observations','cursors','collection_status','backlog','paging','quote_policies','budget_usage','manifest'}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables-allowed or (tables and 'observations' not in tables):
        conn.close()
        raise ValueError('foreign ledger cannot be migrated as research')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS observations (
            origin TEXT PRIMARY KEY, uid TEXT NOT NULL, chain TEXT NOT NULL, address TEXT NOT NULL,
            signature TEXT NOT NULL, event_index TEXT NOT NULL, mint TEXT, action TEXT NOT NULL,
            quantity_atomic TEXT, quote_atomic TEXT, source_at TEXT, received_at TEXT NOT NULL,
            state TEXT NOT NULL, setup_json TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cursors(owner TEXT PRIMARY KEY,signature TEXT,observed_at TEXT);
        CREATE TABLE IF NOT EXISTS collection_status(owner TEXT PRIMARY KEY,state TEXT,detail TEXT,as_of TEXT);
        CREATE TABLE IF NOT EXISTS backlog(owner TEXT,signature TEXT,slot INTEGER,block_time INTEGER,
            PRIMARY KEY(owner,signature));
        CREATE TABLE IF NOT EXISTS paging(owner TEXT PRIMARY KEY,frontier TEXT,head TEXT);
        CREATE TABLE IF NOT EXISTS quote_policies(origin TEXT,arm TEXT,policy_id TEXT,policy_version INTEGER,
            policy_json TEXT, PRIMARY KEY(origin,arm));
        PRAGMA user_version=1;
    ''')
    return conn


def record_observation(conn,event):
    for field in ('quantity_atomic','quote_atomic'):
        raw=event.get(field)
        if raw is not None and (not isinstance(raw,str) or not raw.isascii() or not raw.isdigit()):
            raise ValueError('atomic amount must be decimal TEXT')
    for field in ('setup_json','payload_json'):
        json.loads(event[field])
    values=tuple(event.get(f) for f in FIELDS)
    old=conn.execute('SELECT '+','.join(FIELDS)+' FROM observations WHERE origin=?',(event['origin'],)).fetchone()
    if old is not None:
        # Retry receipt clocks do not revise the immutable first observation.
        compare=[i for i,f in enumerate(FIELDS) if f!='received_at']
        if any(old[i]!=values[i] for i in compare):
            raise ValueError('conflicting observation identity/payload')
        return False
    conn.execute('INSERT INTO observations ('+','.join(FIELDS)+') VALUES ('+','.join('?' for _ in FIELDS)+')',values)
    return True


def advance_cursor(conn,*,owner,signature,observed_at):
    conn.execute('INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET '
                 'signature=excluded.signature,observed_at=excluded.observed_at',(owner,signature,observed_at))


def freeze_manifest(conn,manifest):
    """One immutable selection/window/policy set per research ledger."""
    fields={k:manifest.get(k) for k in ('chain','selection_at','forward_start','policies')}
    fields['owners']=sorted((r['uid'],r['address']) for r in manifest.get('owners',[]))
    payload=json.dumps(fields,sort_keys=True)
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS manifest(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
        old=conn.execute('SELECT payload FROM manifest WHERE id=1').fetchone()
        if old and old[0]!=payload: raise ValueError('frozen research manifest mismatch; use a new research ledger')
        conn.execute('INSERT OR IGNORE INTO manifest VALUES (1,?)',(payload,))


def open_research_quote_db(path):
    """Refuse existing unowned stores before the shared quote helper performs any migration."""
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as check:
            tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            allowed={'copy_quote_schedule','copy_quote_result','research_owner','research_policies',
                     'research_quote_envelopes','research_events'}
            if tables-allowed or 'research_owner' not in tables:
                raise ValueError('unowned quote database cannot be migrated')
            if check.execute('SELECT identity FROM research_owner').fetchall()!=[('wallet-universe-v1',)]:
                raise ValueError('unowned quote database')
    conn=open_measurement_db(path)
    conn.execute('CREATE TABLE IF NOT EXISTS research_owner(identity TEXT PRIMARY KEY)')
    conn.execute("INSERT OR IGNORE INTO research_owner VALUES ('wallet-universe-v1')")
    return conn
