"""Host-only cross-process search cache and proxy admission. No credentials or network I/O here."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

PROVIDERS=('tavily.com','duckduckgo.com','bing.com','search.brave.com')
TTL=300
FAILURE_TTL=5
LEASE=120
RETENTION=7*86400
MAX_ENTRIES=1000
STOP_S=900   # the IP is shared with the funded runner


def provider(host):
    host=host.lower().rstrip('.')
    return next((d for d in PROVIDERS if host==d or host.endswith('.'+d)),None)


def from_config(cfg):
    return Broker(Path(cfg['office_db']).parent/'v2/search.db',cfg.get('search_policy'))


class Broker:
    def __init__(self,path,policy,*,clock=time.time):
        self.path,self.policy,self.clock=Path(path),policy,clock

    def _budget(self):
        try:
            value=json.loads(Path(self.policy).read_text())['daily_budget']
            if type(value) is not int or not 0<=value<=10000:raise ValueError()
            return value
        except (OSError,ValueError,KeyError,TypeError):raise ValueError('search_config_unknown') from None

    def _now(self):
        now=self.clock()
        if not isinstance(now,(int,float)) or not math.isfinite(now) or now<0:raise ValueError('search_clock_unknown')
        return now

    @contextmanager
    def _conn(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError:pass
        c=sqlite3.connect(self.path,timeout=.5)
        try:
            c.executescript('''
          CREATE TABLE IF NOT EXISTS budget(id INTEGER PRIMARY KEY CHECK(id=1), day INTEGER, used INTEGER, last_at REAL);
          CREATE TABLE IF NOT EXISTS pace(host TEXT PRIMARY KEY, at REAL);
          CREATE TABLE IF NOT EXISTS stop(id INTEGER PRIMARY KEY CHECK(id=1), until REAL, reason TEXT);
          CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, token TEXT, at REAL, answer TEXT);
          CREATE INDEX IF NOT EXISTS cache_at ON cache(at);
        ''')
            with c:yield c
        finally:c.close()

    def admit(self,host):
        """Reserve before the proxy dials. Ambiguous/failed attempts are never refunded."""
        family=provider(host)
        if family is None:return None
        try:
            cap=self._budget()
            with self._conn() as c:
                c.execute('BEGIN IMMEDIATE')
                now=self._now();day=int(now//86400)
                row=c.execute('SELECT day,used,last_at FROM budget WHERE id=1').fetchone()
                if row and (type(row[0]) is not int or row[0]<0 or type(row[1]) is not int or row[1]<0
                            or not isinstance(row[2],(int,float)) or not math.isfinite(row[2]) or row[2]<0):return 'search_store_unknown'
                if row and now<row[2]:return 'search_clock_unknown'
                used=row[1] if row and row[0]==day else 0
                if used>=cap:return 'budget_exhausted'
                stop=c.execute('SELECT until FROM stop WHERE id=1').fetchone()
                if stop and now<stop[0]:return 'search_stopped'
                prior=c.execute('SELECT at FROM pace WHERE host=?',(family,)).fetchone()
                if prior and now-prior[0]<5:return 'search_paced'
                c.execute('INSERT OR REPLACE INTO budget VALUES(1,?,?,?)',(day,used+1,now))
                c.execute('INSERT OR REPLACE INTO pace VALUES(?,?)',(family,now))
            return None
        except ValueError as exc:return str(exc)
        except (OSError,sqlite3.Error,TypeError,KeyError):return 'search_store_unknown'

    def prune(self,now=None):
        now=self._now() if now is None else now
        with self._conn() as c:
            c.execute('DELETE FROM cache WHERE at<?',(now-RETENTION,))
            c.execute('DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY at DESC LIMIT -1 OFFSET ?)',(MAX_ENTRIES,))

    def search(self,query,fetch):
        """Exact UTF-8 query; only the lease owner fetches, outside the database transaction."""
        # Bump this namespace when the fetcher's result contract changes.
        key=hashlib.sha256(b'keyless-v1\0'+query.encode()).hexdigest();token=uuid.uuid4().hex
        deadline=time.monotonic()+2
        try:
            self._budget();self.prune()
            while True:
                owner=False
                with self._conn() as c:
                    c.execute('BEGIN IMMEDIATE')
                    now=self._now()
                    stop=c.execute('SELECT until FROM stop WHERE id=1').fetchone()
                    row=c.execute('SELECT token,at,answer FROM cache WHERE key=?',(key,)).fetchone()
                    if row and row[1]>now:return dict(state='unknown',reason='search_clock_unknown')
                    if row and row[2] is not None:
                        answer=json.loads(row[2]);ttl=TTL if answer['state']=='known' else FAILURE_TTL
                        if now-row[1]<=ttl:return self._answer(answer,True,row[1],now-row[1])
                    if stop and now<stop[0]:return dict(state='unknown',reason='search_stopped',retry_after_s=stop[0]-now)
                    if row is None or row[2] is not None or now-row[1]>=LEASE:
                        if row is None:
                            count=c.execute('SELECT count(*) FROM cache').fetchone()[0]
                            if count>=MAX_ENTRIES:
                                removed=c.execute('DELETE FROM cache WHERE key IN (SELECT key FROM cache WHERE answer IS NOT NULL ORDER BY at LIMIT 1)').rowcount
                                if not removed:return dict(state='unknown',reason='search_cache_busy')
                        c.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,NULL)',(key,token,now));owner=True
                if owner:break
                if time.monotonic()>=deadline:return dict(state='unknown',reason='search_inflight')
                time.sleep(.05)
            try:answer=fetch()
            except Exception:answer=dict(state='unknown',reason='search_fetch_unknown')
            if not isinstance(answer,dict) or answer.get('state') not in ('known','unknown'):
                answer=dict(state='unknown',reason='search_response_unknown')
            if answer['state']=='known' and not isinstance(answer.get('results'),list):
                answer=dict(state='unknown',reason='search_response_unknown')
            if answer['state']=='unknown':answer={k:v for k,v in answer.items() if k!='results'}
            raw=json.dumps(answer)
            if len(raw.encode())>14000:answer=dict(state='unknown',reason='search_response_too_large');raw=json.dumps(answer)
            at=self._now()
            reasons={answer.get('reason')}|set(answer.get('failed').values() if isinstance(answer.get('failed'),dict) else ())
            with self._conn() as c:
                # Every engine failed and one said challenge or 429: all seats stop, not only this query.
                if answer['state']=='unknown' and reasons&{'challenge','rate_limited'}:c.execute('INSERT OR REPLACE INTO stop VALUES(1,?,?)',(at+STOP_S,'provider_refused'))
                changed=c.execute('UPDATE cache SET at=?,answer=? WHERE key=? AND token=?',(at,raw,key,token)).rowcount
            if not changed:return dict(state='unknown',reason='search_lease_lost')
            return self._answer(answer,False,at,0)
        except ValueError as exc:return dict(state='unknown',reason=str(exc))
        except (OSError,sqlite3.Error,TypeError,KeyError):return dict(state='unknown',reason='search_store_unknown')

    @staticmethod
    def _answer(answer,cached,at,age):
        out=dict(cached=cached,fetched_at=at,age_s=age)
        out.update({k:v for k,v in answer.items() if k not in out})
        return out
