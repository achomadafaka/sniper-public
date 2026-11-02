"""Private CPMM research tape and bounded ingress. Never opens the trading ledger."""
from __future__ import annotations

import base64
import collections
import fcntl
import json
import math
import sqlite3
from pathlib import Path

from solders.signature import Signature
from sniper.services import memecoin_desk_cpmm as C


class IntegrityLoss(ValueError):
    pass


class StorageFull(OSError):
    pass


def dump(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


class Store:
    def __init__(self,desk_dir,*,max_bytes=1_000_000_000):
        self.path=Path(desk_dir)/'cpmm'/'tape.db'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.max_bytes=max_bytes
        self.lock=(self.path.parent/'writer.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            self.lock.close();raise
        self.conn=sqlite3.connect(self.path,timeout=0)
        self.conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS swaps(event_id TEXT PRIMARY KEY,mint TEXT NOT NULL,pool TEXT NOT NULL,
          received_at REAL NOT NULL,slot INTEGER NOT NULL,token_atomic TEXT NOT NULL,quote_atomic TEXT NOT NULL,
          payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS swaps_window ON swaps(mint,pool,received_at);
        CREATE TABLE IF NOT EXISTS bindings(pool TEXT PRIMARY KEY,mint TEXT NOT NULL,known_at REAL NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gaps(cause TEXT NOT NULL,start_at REAL NOT NULL,end_at REAL NOT NULL,pool TEXT);
        CREATE INDEX IF NOT EXISTS gaps_window ON gaps(start_at,end_at);
        CREATE TABLE IF NOT EXISTS head(id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL,start_at REAL,through REAL,slot INTEGER,complete INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS rpc_backoff(id INTEGER PRIMARY KEY CHECK(id=1),failures INTEGER NOT NULL,retry_at REAL NOT NULL);
        INSERT OR IGNORE INTO rpc_backoff VALUES(1,0,0);
        CREATE TABLE IF NOT EXISTS rpc_budget(day INTEGER PRIMARY KEY,used INTEGER NOT NULL,last_at REAL NOT NULL);
        ''')
        if 'complete' not in {r[1] for r in self.conn.execute('PRAGMA table_info(head)')}:
            self.conn.execute('ALTER TABLE head ADD COLUMN complete INTEGER NOT NULL DEFAULT 0')
        self.conn.execute('INSERT OR IGNORE INTO head(id,version) VALUES(1,?)',(C.VERSION,));self.conn.commit()
        if self.conn.execute('SELECT version FROM head').fetchone()[0]!=C.VERSION:
            raise IntegrityLoss('schema_version')

    def __enter__(self):return self
    def __exit__(self,kind,*args):
        try:
            if kind is None:self.conn.commit()
            else:self.conn.rollback()
        finally:
            self.conn.close();self.lock.close()

    def check_disk(self):
        paths=[self.path,Path(str(self.path)+'-wal'),Path(str(self.path)+'-shm')]
        used=sum(p.stat().st_size for p in paths if p.exists())
        if used>=self.max_bytes:raise StorageFull('cpmm_disk_cap')

    def head(self):
        start,through,slot,complete=self.conn.execute('SELECT start_at,through,slot,complete FROM head').fetchone()
        return {'from':start,'through':through,'slot':slot,'complete':bool(complete)}

    def begin(self,at):
        if not finite(at) or at<0:raise IntegrityLoss('clock')
        self.check_disk()
        # Restart starts a new continuity interval; old rows remain historical evidence.
        self.conn.execute('UPDATE head SET start_at=?,through=NULL,slot=NULL,complete=0 WHERE id=1',(at,));self.conn.commit()

    def bind(self,binding):
        self.check_disk();body=dump(binding)
        old=self.conn.execute('SELECT payload FROM bindings WHERE pool=?',(binding['pool'],)).fetchone()
        if old and old[0]!=body:raise IntegrityLoss('binding_conflict')
        self.conn.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?,?)',(binding['pool'],binding['mint'],binding['known_at'],body));self.conn.commit()

    def bindings(self,at,pools=None):
        if pools is None:
            rows=self.conn.execute('SELECT pool,payload FROM bindings WHERE known_at<=? LIMIT 1000',(at,))
            return {p:json.loads(b) for p,b in rows}
        out={}
        for pool in dict.fromkeys(pools):
            row=self.conn.execute('SELECT payload FROM bindings WHERE pool=? AND known_at<=?',(pool,at)).fetchone()
            if row:out[pool]=json.loads(row[0])
        return out

    def record(self,row):
        self.check_disk();body=dump(row)
        for field in ('token_atomic','quote_atomic','pre_base_atomic','pre_quote_atomic'):
            if not isinstance(row[field],str) or not row[field].isdigit():raise IntegrityLoss('atomic_units')
        old=self.conn.execute('SELECT payload FROM swaps WHERE event_id=?',(row['event_id'],)).fetchone()
        if old:
            prior=json.loads(old[0])
            immutable=('signature','slot','outer_ix','inner_ix','ordinal','pool','mint','event_b64','token_atomic','quote_atomic')
            if any(prior.get(k)!=row.get(k) for k in immutable):raise IntegrityLoss('event_conflict')
            return False
        through=self.head()['through']
        if through is not None and row['received_at']<=through:
            self.gap('late_frame',row['received_at'],through,row['pool'])
        head=self.head()
        if head['slot'] is not None and row['slot']<=head['slot']:
            self.gap('late_slot',head['from'],max(row['received_at'],through),row['pool'])
        self.conn.execute('INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?)',(row['event_id'],row['mint'],row['pool'],row['received_at'],row['slot'],row['token_atomic'],row['quote_atomic'],body))
        return True

    def gap(self,cause,start,end,pool=None):
        self.check_disk()
        if not finite(start) or not finite(end) or not 0<=start<=end:raise IntegrityLoss('gap_clock')
        self.conn.execute('INSERT INTO gaps VALUES(?,?,?,?)',(cause,start,end,pool))

    def fence(self,slot,received_at,*,complete=False):
        self.check_disk();head=self.head()
        if (type(slot) is not int or slot<=0 or not finite(received_at) or head['from'] is None
                or received_at<head['from'] or (head['through'] is not None and
                (received_at<head['through'] or slot<=head['slot']))):raise IntegrityLoss('fence_order')
        # Metadata arrival is a processing checkpoint, not proof of complete filtered delivery.
        self.conn.execute('UPDATE head SET through=?,slot=?,complete=? WHERE id=1',(received_at,slot,int(complete)));self.conn.commit()

    def reserve_rpc(self,at,*,daily_cap,min_spacing):
        self.check_disk()
        day=int(at//86400)
        with self.conn:
            row=self.conn.execute('SELECT used FROM rpc_budget WHERE day=?',(day,)).fetchone()
            last=self.conn.execute('SELECT max(last_at) FROM rpc_budget').fetchone()[0]
            if (row and row[0]>=daily_cap) or (last is not None and at-last<min_spacing):return False
            self.conn.execute('INSERT INTO rpc_budget VALUES(?,1,?) ON CONFLICT(day) DO UPDATE SET used=used+1,last_at=excluded.last_at',(day,at))
        return True


class Capture:
    def __init__(self,store,*,max_bytes=4_000_000):
        self.store=store;self.max_bytes=max_bytes;self.bytes=0
        self.queue=collections.deque();self.pending_gap=None

    def offer(self,raw,received_at):
        if len(raw)+self.bytes>self.max_bytes:
            self.pending_gap=received_at if self.pending_gap is None else min(self.pending_gap,received_at)
            return False
        self.queue.append((raw,received_at));self.bytes+=len(raw);return True

    def pop(self):
        if not self.queue:return None
        raw,at=self.queue.popleft();self.bytes-=len(raw);return raw,at

    def flush_loss(self,at):
        if self.pending_gap is not None:
            self.store.gap('queue_overflow',self.pending_gap,at)
            self.pending_gap=None


def transaction_frame(update,*,received_at,clock_offset_s=None):
    t=update.transaction;i=t.transaction;m=i.transaction.message;meta=i.meta
    if not i.HasField('meta') or not i.HasField('transaction') or not i.transaction.HasField('message'):
        raise C.DecodeLoss('metadata_unknown')
    keys=list(m.account_keys)+list(meta.loaded_writable_addresses)+list(meta.loaded_readonly_addresses)
    def ix(x):return {'program_id_index':x.program_id_index,'accounts':list(x.accounts),'data_b64':base64.b64encode(x.data).decode()}
    def balances(rows):
        return [{'account_index':b.account_index,'mint':b.mint,'owner':b.owner,'program_id':b.program_id,
                 'decimals':b.ui_token_amount.decimals,'amount':b.ui_token_amount.amount} for b in rows]
    offset=clock_offset_s if finite(clock_offset_s) else None
    return {'signature':str(Signature.from_bytes(i.signature)),'slot':t.slot,'tx_index':i.index or None,
            'commitment':'confirmed','err':bytes(meta.err.err).hex() if meta.HasField('err') else None,
            'account_keys':[C.key(k) for k in keys],'signer_count':m.header.num_required_signatures,
            'instructions':[ix(x) for x in m.instructions],
            'inner_instructions':[{'index':g.index,'instructions':[ix(x) for x in g.instructions]} for g in meta.inner_instructions],
            'logs':list(meta.log_messages),'pre_token_balances':balances(meta.pre_token_balances),
            'post_token_balances':balances(meta.post_token_balances),'received_at':received_at,
            'observed_at':received_at-offset if offset is not None else None,'clock_offset_s':offset,'block_time':None}


class Processor:
    """Single writer; metadata lookup happens after raw partial facts, never on receive."""
    def __init__(self,store,*,lookup,daily_cap=120,min_spacing=10):
        self.store=store;self.lookup=lookup;self.daily_cap=daily_cap;self.min_spacing=min_spacing
        self.failures,self.retry_at=self.store.conn.execute('SELECT failures,retry_at FROM rpc_backoff WHERE id=1').fetchone()

    def decode(self,frame):
        preliminary=C.decode_transaction(frame,{})
        bindings=self.store.bindings(frame['received_at'],preliminary['unresolved_pools'])
        result=C.decode_transaction(frame,bindings) if bindings else preliminary
        for row in result['rows']:self.store.record(row)
        for gap in result['gaps']:self.store.gap(gap['cause'],frame['received_at'],frame['received_at'],gap['pool'])
        return result

    def requests(self,result,now):
        for pool in dict.fromkeys(result['unresolved_pools']):
            if now<self.retry_at or not self.store.reserve_rpc(now,daily_cap=self.daily_cap,min_spacing=self.min_spacing):break
            row=next(r for r in result['rows'] if r['pool']==pool)
            yield pool,[row['event']['input_mint'],row['event']['output_mint']]

    def lookup_failed(self,exc,at,now,pool):
        self.failures+=1;self.retry_at=now+min(300,10*2**min(self.failures,5))
        code=getattr(exc,'code',None)
        reason=('rpc_rate_limit' if code==429 else 'rpc_forbidden' if code in (401,403) else
                'rpc_rejected' if type(code) is int else 'rpc_timeout' if isinstance(exc,TimeoutError) else
                str(exc) if isinstance(exc,C.DecodeLoss) and str(exc) in ('rpc_partial','rpc_response_error','rpc_response_size') else
                'binding_lookup_failed')
        self.store.gap(reason,at,max(now,at),pool)
        self.store.conn.execute('UPDATE rpc_backoff SET failures=?,retry_at=? WHERE id=1',(self.failures,self.retry_at));self.store.conn.commit()

    def lookup_succeeded(self,binding):
        self.store.bind(binding)
        self.failures=self.retry_at=0
        self.store.conn.execute('UPDATE rpc_backoff SET failures=0,retry_at=0 WHERE id=1');self.store.conn.commit()

    def transaction(self,frame,*,now):
        result=self.decode(frame)
        for pool,mints in self.requests(result,now):
            try:
                b=self.lookup(pool,mints,now)
                if b['known_at']<now:raise C.DecodeLoss('binding_backdated')
                self.lookup_succeeded(b)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.lookup_failed(exc,frame['received_at'],now,pool)
        return result
