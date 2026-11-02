"""Host-side display market data for seats: cache first, then one paced provider read shared by every seat. Provider claims only, never a fill price or sheet evidence."""
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

HOST='api.dexscreener.com'
TTL=60
PACE=10
DAILY=500
STOP_429=900
TIMEOUT=5
ADDRESS=re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}\Z')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None


_OPENER=urllib.request.build_opener(_NoRedirect)   # a redirect is not this provider's answer; it reads as unavailable


def _fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'sniper-office/1'})
    try:
        with _OPENER.open(req,timeout=TIMEOUT) as r:return r.status,json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as exc:return exc.code,None
    except (OSError,ValueError):return None,None


def _dict(value):
    return value if isinstance(value,dict) else {}


def _text(value):
    return value if isinstance(value,str) else None


def _number(value):
    try:value=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return value if math.isfinite(value) and value>=0 else None


def _pick(doc,*,mint,pool):
    """The asked pool, or the deepest Solana pair whose base token is the asked mint. None when no pair proves that identity."""
    pairs=doc.get('pairs') or []
    if not isinstance(pairs,list):raise ValueError('provider_malformed')
    pairs=[p for p in pairs if isinstance(p,dict) and p.get('chainId')=='solana']
    if pool:pairs=[p for p in pairs if p.get('pairAddress')==pool]
    else:pairs=[p for p in pairs if _dict(p.get('baseToken')).get('address')==mint]
    return max(pairs,key=lambda p:_number(_dict(p.get('liquidity')).get('usd')) or 0.,default=None)


def _facts(pair,fetched_at):
    created=_number(pair.get('pairCreatedAt'))
    fields=dict(price_usd=_number(pair.get('priceUsd')),liquidity_usd=_number(_dict(pair.get('liquidity')).get('usd')),
                volume_24h_usd=_number(_dict(pair.get('volume')).get('h24')),market_cap_usd=_number(pair.get('marketCap')),
                pair_age_s=max(0.,fetched_at-created/1000.) if created else None)
    return dict(state='known',mint=_text(_dict(pair.get('baseToken')).get('address')),pool=_text(pair.get('pairAddress')),dex=_text(pair.get('dexId')),**fields,
                field_unknown=sorted(k for k,v in fields.items() if v is None))


class MarketData:
    def __init__(self,path,*,clock=time.time,fetch=_fetch):
        self.path,self.clock,self.fetch=Path(path),clock,fetch

    def _conn(self):
        conn=sqlite3.connect(self.path,timeout=2,isolation_level=None)
        conn.executescript('CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,at REAL,doc TEXT);'
                           'CREATE TABLE IF NOT EXISTS fetches(fetch_id TEXT PRIMARY KEY,asked TEXT,network_id TEXT,fetched_at REAL,age_s REAL,cached INTEGER,state TEXT,reason TEXT,price_usd REAL,liquidity_usd REAL,volume_24h_usd REAL);'
                           'CREATE TABLE IF NOT EXISTS calls(at REAL,outcome TEXT);CREATE INDEX IF NOT EXISTS calls_at ON calls(at);'
                           'CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK(id=1),day INTEGER,used INTEGER,last_at REAL,stop_until REAL);')
        if 'shown_at' not in {r[1] for r in conn.execute('PRAGMA table_info(fetches)')}:conn.execute('ALTER TABLE fetches ADD COLUMN shown_at REAL')
        for column in ({'dial','stop_until'}-{r[1] for r in conn.execute('PRAGMA table_info(calls)')}):
            conn.execute('ALTER TABLE calls ADD COLUMN %s %s'%(column,'INTEGER' if column=='dial' else 'REAL'))
        return conn

    def _admit(self,conn):
        """Reserved before the dial and never refunded, so a failed call still spends pace. The clock is read under the write lock, so a delayed caller cannot reserve with an old time."""
        conn.execute('BEGIN IMMEDIATE')
        try:
            now=self.clock()
            day,used,last,stop=conn.execute('SELECT day,used,last_at,stop_until FROM budget WHERE id=1').fetchone() or (int(now//86400),0,0.,0.)
            if now<last:return 'clock_unknown'
            if now<stop:return 'provider_backoff'
            used=used if day==int(now//86400) else 0
            if used>=DAILY:return 'budget_exhausted'
            if now-last<PACE:return 'paced'
            conn.execute('INSERT OR REPLACE INTO budget VALUES(1,?,?,?,?)',(int(now//86400),used+1,now,stop))
            return conn.execute('INSERT INTO calls(at,dial) VALUES(?,1)',(now,)).lastrowid
        finally:conn.execute('COMMIT')

    def read(self,*,mint=None,pool=None):
        out,reservation=self._read(mint=mint,pool=pool)
        stop_until=out.pop('_stop_until',None)
        try:
            conn=self._conn()
            try:
                if 'fetched_at' in out:
                    # What the seat is about to see, kept until the epoch is sealed plus 30 days; pruning is manual, never here.
                    out=dict(out,fetch_id=uuid.uuid4().hex)
                    conn.execute('INSERT INTO fetches(shown_at,fetch_id,asked,network_id,fetched_at,age_s,cached,state,reason,price_usd,liquidity_usd,volume_24h_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(self.clock(),out['fetch_id'],out['asked'],out['network_id'],out['fetched_at'],out['age_s'],int(out['cached']),out['state'],out.get('reason'),out.get('price_usd'),out.get('liquidity_usd'),out.get('volume_24h_usd')))
                outcome='cache_hit' if out.get('cached') else out.get('reason') or 'fetched'
                try:
                    if reservation is None:conn.execute('INSERT INTO calls(at,outcome,dial) VALUES(?,?,0)',(self.clock(),outcome))
                    else:conn.execute('UPDATE calls SET outcome=?,stop_until=? WHERE rowid=?',(outcome,stop_until,reservation))
                except sqlite3.Error:pass   # the reservation row stays, outcome NULL, and the report shows it as outcome_unknown
            finally:conn.close()
        except (OSError,sqlite3.Error):return dict(state='unknown',reason='market_store_unknown')   # nothing reaches a seat unrecorded
        return out

    def counts(self,start,end):
        """Read-only, for the tool and the report: never creates the store. Every dial has a reservation row, so total is not an undercount; an outcome never written reads outcome_unknown."""
        try:
            conn=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)
        except sqlite3.Error:return dict(state='unknown',reason='market_db_missing')
        try:
            rows=conn.execute('SELECT dial,outcome,count(*) FROM calls WHERE at>=? AND at<? GROUP BY dial,outcome',(start,end)).fetchall()
            stops=conn.execute('SELECT stop_until FROM calls WHERE stop_until IS NOT NULL AND at>=? AND at<? ORDER BY at LIMIT 101',(start,end)).fetchall()
            return dict(state='known',dials=sum(r[2] for r in rows if r[0]),dials_by_outcome={r[1] or 'outcome_unknown':r[2] for r in rows if r[0]},
                        other_by_outcome={r[1]:r[2] for r in rows if not r[0]},stop_until=[r[0] for r in stops[:100]],stop_windows_truncated=len(stops)>100)
        except sqlite3.Error:return dict(state='unknown',reason='market_db_unreadable')
        finally:conn.close()

    def _read(self,*,mint=None,pool=None):
        address=pool or mint
        if bool(mint)==bool(pool) or not isinstance(address,str) or not ADDRESS.match(address):return dict(state='unknown',reason='bad_arguments'),None
        key=('pool:' if pool else 'mint:')+address
        now=self.clock()
        try:
            conn=self._conn()
            try:
                hit=conn.execute('SELECT at,doc FROM cache WHERE key=?',(key,)).fetchone()
                if hit and 0<=now-hit[0]<TTL:return self._answer(json.loads(hit[1]),hit[0],now,True,key),None
                reservation=self._admit(conn)
                if isinstance(reservation,str):return dict(state='unknown',reason=reservation),None
                url='https://%s/latest/dex/pairs/solana/%s'%(HOST,pool) if pool else 'https://%s/latest/dex/tokens/%s'%(HOST,mint)
                status,doc=self.fetch(url)
                now=self.clock()
                if status==429:
                    conn.execute('UPDATE budget SET stop_until=? WHERE id=1',(now+STOP_429,))
                    return dict(state='unknown',reason='provider_rate_limited',_stop_until=now+STOP_429),reservation
                if status!=200 or not isinstance(doc,dict):return dict(state='unknown',reason='provider_unavailable'),reservation
                try:pair=_pick(doc,mint=mint,pool=pool)
                except ValueError:return dict(state='unknown',reason='provider_malformed'),reservation
                # A 200 with no matching pair is an answer about that address, not an outage.
                facts=_facts(pair,now) if pair else dict(state='unknown',reason='no_matching_pair')
                conn.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(key,now,json.dumps(facts)))
                return self._answer(facts,now,now,False,key),reservation
            finally:conn.close()
        except (OSError,sqlite3.Error,ValueError,TypeError):return dict(state='unknown',reason='market_store_unknown'),None

    @staticmethod
    def _answer(facts,at,now,cached,key):
        return dict(facts,network_id='solana',asked=key,provenance=HOST,label='provider_claim',note='display data; not a fill price',fetched_at=at,age_s=now-at,cached=cached)
