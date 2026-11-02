"""The Office wallet network (operator order 2026-10-31): wallets, developers, traders and funders as edges with source, time and proof. Research only.

Nothing here can arm a wallet. This is a separate file under data/office; the funded runner, the Wallet Map and trial arming never open it (tests/office/test_wallet_network.py fails if one does), and it feeds no sheet or gate.
An edge is evidence, not control: an exchange can fund a wallet. proof_state is verified only when the proof names a stored chain fetch whose result contains both addresses; a seat's word stays claimed. Null observed_at means unknown.
A missing edge is not "no link": coverage rows say looked_nothing or unknown.
The operator removed default research admission/storage ceilings on 2026-11-01; usage is still accounted. Explicit finite limits remain supported."""
import json
import os
import math
import re
import sqlite3
from pathlib import Path

from sniper.office.chain_data import CREDITS_ESTIMATE, MAX_ROWS, TRACE_TXS

EDGE_TYPES = ('funded_by', 'created_mint', 'traded_with', 'co_buy_slot', 'slot_follower', 'follows', 'dumps_on', 'shared_funder', 'holds')
SOURCES = ('host', 'seat', 'import')
MAX_BYTES = math.inf
MAX_ROWS_PER_DAY = math.inf
CREDITS_PER_DAY = None
CLAIMED_DAYS = OTHER_DAYS = math.inf
MAX_HOPS = 2
EXPAND_MAX_HOPS = 3    # chain_data.TRACE_HOPS; the credit reservation scales with it
MAX_LIMIT = 500
MAX_TEXT = 200
ADDRESS = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
PRUNE_EVERY_S = 3600.0
READ_STEP = 1000
READ_MAX_VM_OPS = 5_000_000   # a web poll cannot run away on a full store: past this the read is a typed unknown
EXPAND_MAX_ROWS = max(2 * EXPAND_MAX_HOPS, MAX_ROWS + 1)    # a trace writes an edge and a coverage row per hop; holders write up to chain_data.MAX_ROWS edges, clamped below, and one coverage row
HOLDERS_CALLS = 2
SCHEMA = '''
CREATE TABLE IF NOT EXISTS edges(edge_id INTEGER PRIMARY KEY,network_id TEXT NOT NULL,src TEXT NOT NULL,dst TEXT NOT NULL,type TEXT NOT NULL,source TEXT NOT NULL,actor_id TEXT,
  observed_at REAL,known_at REAL NOT NULL,proof_kind TEXT,proof_ref TEXT,proof_state TEXT NOT NULL,method_version TEXT NOT NULL,stale INTEGER NOT NULL DEFAULT 0,
  CHECK(proof_state IN ('verified','claimed','none')));
CREATE UNIQUE INDEX IF NOT EXISTS edges_identity ON edges(network_id,src,dst,type,source,IFNULL(proof_ref,''),method_version);
CREATE INDEX IF NOT EXISTS edges_src ON edges(network_id,src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(network_id,dst);
CREATE INDEX IF NOT EXISTS edges_verified_fanout ON edges(network_id,dst,src) WHERE type='funded_by' AND proof_state='verified';
CREATE INDEX IF NOT EXISTS edges_hub_campaign ON edges(network_id,dst,observed_at,known_at,src) WHERE type='funded_by' AND proof_state='verified' AND source='host';
CREATE INDEX IF NOT EXISTS edges_known ON edges(known_at);
CREATE INDEX IF NOT EXISTS edges_overview ON edges(network_id,(source='import'),edge_id DESC);
CREATE TABLE IF NOT EXISTS labels(network_id TEXT NOT NULL,address TEXT NOT NULL,label TEXT NOT NULL,source TEXT NOT NULL,actor_id TEXT,known_at REAL NOT NULL,PRIMARY KEY(network_id,address,label,source));
CREATE TABLE IF NOT EXISTS coverage(network_id TEXT NOT NULL,address TEXT NOT NULL,check_name TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,at REAL NOT NULL,fetch_id TEXT,PRIMARY KEY(network_id,address,check_name,at),
  CHECK(state IN ('found','looked_nothing','unknown')));
CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY,rows_added INTEGER NOT NULL DEFAULT 0,credits INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);
'''


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _day(at):
    return str(int(at // 86400))


class WalletNetwork:
    def __init__(self, path, *, chain_db=None, chain=None, clock=None, max_bytes=MAX_BYTES, busy_timeout=2):
        import time
        self.path, self.chain_db, self.chain = Path(path), chain_db, chain
        self.clock, self.max_bytes = clock or time.time, max_bytes
        self.busy_timeout=busy_timeout

    def _conn(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
            return conn
        except BaseException:
            conn.close()
            raise

    def _size(self):
        return sum(os.path.getsize(p) for p in (self.path, Path(str(self.path) + '-wal')) if p.exists())

    def _refusal(self, conn, now, *, rows=0):
        if self._size() >= self.max_bytes:
            return 'disk_cap'
        if rows:
            got = conn.execute('SELECT rows_added FROM daily WHERE day=?', (_day(now),)).fetchone()
            if (got[0] if got else 0) + rows > MAX_ROWS_PER_DAY:
                return 'daily_row_cap'
        return None

    def _count(self, conn, now, rows=0, credits=0):
        conn.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET rows_added=rows_added+excluded.rows_added,credits=credits+excluded.credits', (_day(now), rows, credits))

    def _fetch_proves(self, fetch_id, *addresses):
        """True only when the stored chain fetch exists, was answered, and its result names every address. Unreadable is False: claimed, never verified."""
        if not self.chain_db or not isinstance(fetch_id, str):
            return False
        try:
            conn = sqlite3.connect(Path(self.chain_db).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
            try:
                row = conn.execute('SELECT state,result FROM fetches WHERE fetch_id=?', (fetch_id,)).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return False
        if row is None or row[0] not in ('known', 'partial') or not isinstance(row[1], str):
            return False
        try:
            tokens = set(_strings(json.loads(row[1])))      # exact tokens: a longer string containing an address proves nothing
        except ValueError:
            return False
        return all(a in tokens for a in addresses)

    def _insert(self, conn, *, src, dst, edge_type, source, actor, observed_at, known_at, proof_kind, proof_ref, proof_state, method, stale=0, network='solana'):
        got = conn.execute('INSERT OR IGNORE INTO edges(network_id,src,dst,type,source,actor_id,observed_at,known_at,proof_kind,proof_ref,proof_state,method_version,stale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (network, src, dst, edge_type, source, actor, observed_at, known_at, proof_kind, proof_ref, proof_state, method, stale))
        return got.rowcount

    def add_edge(self, *, src, dst, edge_type, actor, observed_at=None, fetch_id=None, note_ref=None):
        """A seat's edge: labelled seat. Verified needs a matching stored fetch; anything else is claimed."""
        ok_time = observed_at is None or (type(observed_at) in (int, float) and 0 < observed_at <= self.clock())
        if not (isinstance(src, str) and ADDRESS.match(src) and isinstance(dst, str) and ADDRESS.match(dst)) or edge_type not in EDGE_TYPES or not ok_time or src == dst:
            return dict(state='refused', reason='bad_arguments')
        if note_ref is not None and (not isinstance(note_ref, str) or len(note_ref) > MAX_TEXT):
            return dict(state='refused', reason='bad_arguments')
        verified = self._fetch_proves(fetch_id, src, dst)
        now = self.clock()
        conn = self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            why = self._refusal(conn, now, rows=1)
            if why:
                return dict(state='refused', reason=why)
            kind, ref = ('chain_fetch', fetch_id) if fetch_id else ('seat_note', note_ref)
            with conn:
                n = self._insert(conn, src=src, dst=dst, edge_type=edge_type, source='seat', actor=actor, observed_at=observed_at, known_at=now, proof_kind=kind, proof_ref=ref,
                                 proof_state='verified' if verified else 'claimed' if ref else 'none', method='seat-v1')
                self._count(conn, now, rows=n)
            self._prune_due(conn, now)
            return dict(state='added' if n else 'duplicate', proof_state='verified' if verified else 'claimed' if ref else 'none')
        finally:
            conn.close()

    def add_wallet(self, *, address, label, actor):
        if not (isinstance(address, str) and ADDRESS.match(address)) or not isinstance(label, str) or not label.strip() or len(label) > MAX_TEXT:
            return dict(state='refused', reason='bad_arguments')
        now = self.clock()
        conn = self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            why = self._refusal(conn, now, rows=1)
            if why:
                return dict(state='refused', reason=why)
            with conn:
                n = conn.execute('INSERT OR IGNORE INTO labels VALUES(?,?,?,?,?,?)', ('solana', address, label, 'seat', actor, now)).rowcount
                self._count(conn, now, rows=n)
            return dict(state='added' if n else 'duplicate', source='seat')
        finally:
            conn.close()

    def _credits_since(self, start):
        conn = self.chain._conn()
        try:
            return conn.execute('SELECT COALESCE(SUM(credits_charged),0) FROM calls WHERE at>=?', (start,)).fetchone()[0]
        finally:
            conn.close()

    def expand(self, *, seed, kind='wallet', hops=1, actor=None):
        """Host read from a seed: a wallet's funding trace or a mint's largest holders. Stops typed at the credit cap. Credits are the chain tool's own estimates, counted across any concurrent chain call in the same second: over, not under."""
        if self.chain is None:
            return dict(state='unknown', reason='chain_config_unknown')
        if not (isinstance(seed, str) and ADDRESS.match(seed)) or kind not in ('wallet', 'mint') or type(hops) is not int or not 1 <= hops <= EXPAND_MAX_HOPS:
            return dict(state='refused', reason='bad_arguments')
        now = self.clock()
        # Reserve the worst case before any call, atomically, so neither cap can be passed; the real figures replace it below. A crash leaves the reservation counted: over, not under.
        hold_credits = (1 + TRACE_TXS) * hops * CREDITS_ESTIMATE if kind == 'wallet' else HOLDERS_CALLS * CREDITS_ESTIMATE
        conn = self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            got = conn.execute('SELECT credits FROM daily WHERE day=?', (_day(now),)).fetchone()
            if CREDITS_PER_DAY is not None and (got[0] if got else 0) + hold_credits > CREDITS_PER_DAY:
                return dict(state='refused', reason='credit_cap', credits_per_day=CREDITS_PER_DAY, credits_needed=hold_credits)
            why = self._refusal(conn, now, rows=EXPAND_MAX_ROWS)
            if why:
                return dict(state='refused', reason=why)
            self._count(conn, now, rows=EXPAND_MAX_ROWS, credits=hold_credits)
            conn.commit()
        finally:
            conn.close()
        out = self.chain.funding_trace(seed, hops=hops) if kind == 'wallet' else self.chain.token_holders(seed)
        spent = self._credits_since(now)
        fetch_id = out.get('fetch_id')
        at = self.clock()
        edges, cover = [], []
        if kind == 'wallet':
            for step in out.get('hops') or out.get('steps') or []:
                wallet = step.get('wallet')
                if step.get('state') == 'known' and step.get('funder'):
                    edges.append((wallet, step['funder'], 'funded_by', step.get('block_time_s')))
                    cover.append((wallet, 'funder', 'found', None))
                elif wallet:
                    cover.append((wallet, 'funder', 'unknown', step.get('reason')))
            if not cover:
                cover.append((seed, 'funder', 'unknown', out.get('reason')))
        else:
            for item in (out.get('items') or [])[:MAX_ROWS]:
                if item.get('owner'):
                    edges.append((item['owner'], seed, 'holds', out.get('fetched_at')))
            cover.append((seed, 'holders_top20', 'found' if edges else 'unknown', out.get('reason')))
        conn = self._conn()
        try:
            added, covered = 0, 0
            with conn:
                for src, dst, edge_type, observed in edges:
                    verified = self._fetch_proves(fetch_id, src, dst)
                    added += self._insert(conn, src=src, dst=dst, edge_type=edge_type, source='host', actor=actor, observed_at=observed, known_at=at, proof_kind='chain_fetch', proof_ref=fetch_id,
                                          proof_state='verified' if verified else 'none', method='expand-v1')
                for address, check, state, reason in cover:
                    covered += conn.execute('INSERT OR IGNORE INTO coverage VALUES(?,?,?,?,?,?,?)', ('solana', address, check, state, reason, at, fetch_id)).rowcount
                self._count(conn, now, rows=added + covered - EXPAND_MAX_ROWS, credits=spent - hold_credits)      # the reservation's own day, even across midnight
            self._prune_due(conn, at)
        finally:
            conn.close()
        return dict(state=out.get('state', 'unknown'), reason=out.get('reason'), edges_added=added, fetch_id=fetch_id, credits_estimated=spent, source='host')

    def read(self, *, seed=None, hops=1, limit=200, types=None, as_of=None, verified_only=False):
        """Around a seed, or with no seed the newest edges, seat and host before import. An overview is a bounded sample: truncated says more exist, and it says nothing about the coins not shown."""
        if not (seed is None or (isinstance(seed, str) and ADDRESS.match(seed))) or type(hops) is not int or not 1 <= hops <= MAX_HOPS or type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return dict(state='refused', reason='bad_arguments')
        if types is not None and (not isinstance(types, (list, tuple)) or any(t not in EDGE_TYPES for t in types)):
            return dict(state='refused', reason='bad_arguments')
        clock_now = self.clock()
        now = clock_now if as_of is None else as_of
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 < now <= clock_now or type(verified_only) is not bool:
            return dict(state='refused', reason='bad_arguments')
        predicate = "known_at<=? AND (observed_at IS NULL OR observed_at<=?)" + (" AND proof_state='verified'" if verified_only else '')
        causal = (now, now)
        if not self.path.exists():
            return dict(state='unknown', reason='store_missing', as_of=now, nodes=[], edges=[], coverage=[], truncated=False)
        try:
            conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
        except (sqlite3.Error, OSError):
            return dict(state='unknown', reason='store_unreadable', as_of=now, nodes=[], edges=[], coverage=[], truncated=False)
        conn.row_factory = sqlite3.Row
        budget = [READ_MAX_VM_OPS // READ_STEP]
        def tick():
            budget[0] -= 1
            return budget[0] < 0                # nonzero aborts the statement
        conn.set_progress_handler(tick, READ_STEP)
        try:
            seen, frontier, edges, truncated = ({seed}, [seed], {}, False) if seed else (set(), [], {}, False)
            if seed is None:
                marks = ','.join('?' * len(types)) if types else None
                rows = conn.execute("SELECT * FROM edges WHERE network_id='solana' AND " + predicate + "%s ORDER BY source='import',edge_id DESC LIMIT ?" % (' AND type IN (%s)' % marks if types else ''), (*causal, *(types or ()), limit + 1)).fetchall()
                truncated = len(rows) > limit
                for r in rows[:limit]:
                    edges[r['edge_id']] = dict(r)
                    seen.update((r['src'], r['dst']))
            for _ in range(hops if seed else 0):
                nxt = []
                for address in frontier:
                    rows = conn.execute('SELECT * FROM edges WHERE network_id=? AND (src=? OR dst=?) AND ' + predicate + ' ORDER BY known_at DESC LIMIT ?', ('solana', address, address, *causal, limit + 1)).fetchall()
                    for r in rows:
                        if types and r['type'] not in types:
                            continue
                        if r['edge_id'] not in edges:
                            if len(edges) >= limit:
                                truncated = True
                                continue
                            edges[r['edge_id']] = dict(r)
                        for other in (r['src'], r['dst']):
                            if other not in seen:
                                seen.add(other)
                                nxt.append(other)
                frontier = nxt
            marks = ','.join('?' * len(seen))
            labels = conn.execute('SELECT address,label,source,known_at FROM labels WHERE address IN (%s) AND known_at<=?' % marks, (*seen, now)).fetchall()
            cover = conn.execute('SELECT address,check_name,state,reason,at,fetch_id FROM coverage WHERE address IN (%s) AND at<=? ORDER BY at DESC LIMIT 200' % marks, (*seen, now)).fetchall()
        except sqlite3.Error:
            return dict(state='unknown', reason='store_read_budget' if budget[0] < 0 else 'store_unreadable', as_of=now, nodes=[], edges=[], coverage=[], truncated=False)
        finally:
            conn.close()
        by = {}
        for r in labels:
            by.setdefault(r['address'], []).append(dict(label=r['label'], source=r['source'], known_at=r['known_at']))
        return dict(state='partial' if truncated else 'known', as_of=now, truncated=truncated, note='Evidence, not control. No edge is not no link; read coverage.',
                    nodes=[dict(address=a, labels=by.get(a, [])) for a in sorted(seen)], edges=list(edges.values()), coverage=[dict(r) for r in cover])

    def _prune_due(self, conn, now):
        got = conn.execute("SELECT value FROM meta WHERE key='pruned_at'").fetchone()
        if got is None or now - got[0] >= PRUNE_EVERY_S:
            self.prune(conn, now)

    def prune(self, conn, now):
        with conn:
            conn.execute("DELETE FROM edges WHERE (proof_state!='verified' AND source='seat' AND known_at<?) OR known_at<?", (now - CLAIMED_DAYS * 86400, now - OTHER_DAYS * 86400))
            conn.execute('DELETE FROM coverage WHERE at<?', (now - OTHER_DAYS * 86400,))
            conn.execute("DELETE FROM labels WHERE source='seat' AND known_at<?", (now - CLAIMED_DAYS * 86400,))
            conn.execute("INSERT INTO meta VALUES('pruned_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
