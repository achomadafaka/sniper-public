"""Desk news capture (docs/superpowers/specs/2026-10-27-desk-news-capture-design.md). One-shot poller: polls every source whose last attempt is older than its cadence.

Every attempt writes a `polls` row. An error is a typed `error` row (UNKNOWN), never an empty result; `ok` with items_seen=0 is an empty feed. `items` keeps the FIRST sighting only,
never updated, with the raw parsed entry. Times are corrected UTC (sniper.util.clock.real_utc_now)."""
import hashlib
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sniper.feeds.news import DESK_NEWS_SOURCES, _canonicalize_url
from sniper.util.clock import real_utc_now

TIMEOUT_S = 15.0
UA = "Mozilla/5.0 (sniper-research news capture)"
DDL = """
create table if not exists polls(id integer primary key, source text not null, started_at real, received_at real not null, status text not null check(status in ('ok','error')),
  http_status integer, bytes integer, items_seen integer, new_items integer, error_class text, error_detail text);
create table if not exists items(source text not null, item_id text not null, first_received_at real not null, published_at text, title text, summary text, raw_json text not null,
  unique(source, item_id));
create index if not exists polls_src on polls(source, received_at);
"""


class NewsError(Exception):
    def __init__(self, error_class, detail="", http_status=None):
        super().__init__("%s: %s" % (error_class, detail))
        self.error_class, self.detail, self.http_status = error_class, str(detail)[:300], http_status


def fetch(url, timeout=TIMEOUT_S, opener=urllib.request.urlopen):
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
            body = r.read()
            status = getattr(r, "status", 200)
    except urllib.error.HTTPError as exc:
        raise NewsError("http_status", exc.reason, exc.code)
    except (socket.timeout, TimeoutError) as exc:
        raise NewsError("timeout", exc)
    except urllib.error.URLError as exc:
        raise NewsError("timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "transport", exc.reason)
    except OSError as exc:
        raise NewsError("transport", exc)
    if not body:
        raise NewsError("empty_body", "zero bytes", status)
    return status, body


def parse_rss(body):
    import feedparser
    f = feedparser.parse(body)
    if not f.get("version"):                                         # not RSS/Atom at all (an HTML block page parses to zero entries and no error): UNKNOWN, never an empty feed
        raise NewsError("parse_error", "not an rss/atom document: %s" % (getattr(f, "bozo_exception", None) or "no feed version"))
    out = []
    for e in f.entries:
        title, link, pub = str(e.get("title") or ""), str(e.get("link") or ""), str(e.get("published") or e.get("updated") or "")
        # a feed may reuse ONE link for every entry (Google Trends): the title is part of the identity unless the feed gives a real id
        iid = str(e.get("id") or "") or hashlib.sha256(("%s|%s" % (_canonicalize_url(link), title)).encode()).hexdigest()[:32]
        out.append(dict(item_id=iid, published_at=pub, title=title, summary=str(e.get("summary") or ""), raw=json.dumps(dict(e), default=str, sort_keys=True)))
    note = None
    if f.bozo and not out and not isinstance(getattr(f, "bozo_exception", None), feedparser.CharacterEncodingOverride):
        raise NewsError("parse_error", "malformed feed with zero entries: %s" % str(getattr(f, "bozo_exception", "malformed"))[:200])        # truncated body: UNKNOWN, not an empty feed
    if f.bozo and not isinstance(getattr(f, "bozo_exception", None), feedparser.CharacterEncodingOverride):
        note = "partial: %s" % str(getattr(f, "bozo_exception", "malformed"))[:200]      # parsed with errors: kept, but the poll row says so
    return out, note


def parse_wiki_top(body):
    try:
        j = json.loads(body)
        it = j["items"][0]
        date = "%s-%s-%s" % (it["year"], it["month"], it["day"])
        arts = it["articles"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise NewsError("parse_error", exc)
    return [dict(item_id="%s:%s" % (date, a["article"]), published_at=date, title=str(a["article"]).replace("_", " "), summary=json.dumps({"rank": a.get("rank"), "views": a.get("views")}),
                raw=json.dumps(a, sort_keys=True)) for a in arts], None


PARSERS = {"rss": parse_rss, "wiki_top": parse_wiki_top}


class Poller:
    def __init__(self, state_dir, *, clock=real_utc_now, fetcher=fetch, sources=DESK_NEWS_SOURCES, pause_s=2.0):
        self.pause_s = pause_s
        self.dir, self.clock, self.fetcher, self.sources = Path(state_dir) / "news", clock, fetcher, sources
        self.dir.mkdir(parents=True, exist_ok=True)

    def _day(self, t):
        return datetime.fromtimestamp(t, timezone.utc).date().isoformat()

    def _con(self, day):
        con = sqlite3.connect(self.dir / ("%s.db" % day), timeout=30)
        con.execute("pragma journal_mode=wal")
        con.executescript(DDL)
        if "started_at" not in {r[1] for r in con.execute("pragma table_info(polls)")}:           # a day file made by a71fc4cb5: additive, old rows keep started_at NULL (unknown)
            con.execute("alter table polls add column started_at real")
            con.commit()
        return con

    def _days(self, now):
        return [self._day(now), self._day(now - 86400.0)]

    def _last_attempt(self, source, now):
        last = 0.0
        for d in self._days(now):
            if (self.dir / ("%s.db" % d)).exists():
                con = self._con(d)
                try:
                    r = con.execute("select max(received_at) from polls where source=?", (source,)).fetchone()[0]
                finally:
                    con.close()
                last = max(last, r or 0.0)
        return last

    def _seen_con(self):
        """Durable (source, item_id) identity across ALL days: first sighting is forever. Built from the day files the first time it is missing."""
        path = self.dir / "seen.db"
        fresh = not path.exists()
        con = sqlite3.connect(path, timeout=30)
        con.execute("create table if not exists seen(source text not null, item_id text not null, first_received_at real not null, primary key(source, item_id))")
        if fresh:
            for f in sorted(self.dir.glob("????-??-??.db")):
                day = sqlite3.connect(f)
                try:
                    con.executemany("insert or ignore into seen values(?,?,?)", day.execute("select source, item_id, first_received_at from items").fetchall())
                finally:
                    day.close()
            con.commit()
        return con

    def run_once(self):
        out = []
        for sid, kind, url, every in self.sources:
            now = self.clock()
            if now - self._last_attempt(sid, now) < every:
                continue
            out.append(self.poll(sid, kind, url, now))
            time.sleep(self.pause_s)                                 # unauthenticated hosts (Reddit) answer 429 to back-to-back requests
        return out

    def poll(self, sid, kind, url, started):
        if "{date}" in url:
            url = url.replace("{date}", (datetime.fromtimestamp(started, timezone.utc) - timedelta(days=1)).strftime("%Y/%m/%d"))
        status = err = size = note = None
        items = []
        try:
            status, body = self.fetcher(url)
            size = len(body)
            items, note = PARSERS[kind](body)
        except NewsError as exc:
            err = exc
            status = exc.http_status if status is None else status
        except Exception as exc:                                     # a parser bug must be a typed row, not an empty result
            err = NewsError("parse_error", "%s: %s" % (type(exc).__name__, exc))
        now = self.clock()                                           # receipt = fetch COMPLETION (success or error), never the pre-fetch time
        con = self._con(self._day(now))
        seen = self._seen_con()
        try:
            new = 0
            if err is None:
                for it in items:
                    if seen.execute("select 1 from seen where source=? and item_id=?", (sid, it["item_id"])).fetchone():
                        continue
                    con.execute("insert or ignore into items values(?,?,?,?,?,?,?)", (sid, it["item_id"], now, it["published_at"], it["title"], it["summary"], it["raw"]))
                    con.commit()                                     # item first, identity second: a crash between them can duplicate later, never lose an item
                    seen.execute("insert or ignore into seen values(?,?,?)", (sid, it["item_id"], now))
                    seen.commit()
                    new += 1
            con.execute("insert into polls(source, started_at, received_at, status, http_status, bytes, items_seen, new_items, error_class, error_detail) values(?,?,?,?,?,?,?,?,?,?)",
                        (sid, started, now, "error" if err else "ok", status, size, None if err else len(items), None if err else new, err.error_class if err else None,
                         err.detail if err else note))
            con.commit()
        finally:
            con.close()
            seen.close()
        return dict(source=sid, status="error" if err else "ok", error_class=err.error_class if err else None, items_seen=None if err else len(items), new_items=None if err else new)
