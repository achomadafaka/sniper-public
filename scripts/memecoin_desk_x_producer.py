#!/usr/bin/env python3
"""Desk X producer: J7's main feed socket (nyc.j7tracker.io) recorded to data/desk/x/<UTC day>.db.

Read-only, off every entry path, its own process. Operator 2026-10-21 (40be676f): a persistent
producer on J7_TOKEN_2 storing posts, ai_suggestion and token_meta; raw post text reaches only the
Haiku news researcher, never a decision prompt, and never this process's log.

  posts          tweet, tweet_update, quoted_tweet, reply_tweet, external_message, tweet_deleted and
                 each element of initialTweets: one row per frame (updates repeat a tweet_id).
  ai_suggestion  J7's own tweet->ticker guess (ai_suggestion, ai_suggestion_update), a separate table
                 so nobody reads it as our evidence.
  token_meta     J7's tweet->mint link (multi-chain, mint as sent).
  other          every other frame kind, whole.
  connection     connect / disconnect / connect_error / auth_error, so a reader can name coverage gaps.
Times: received_at = host epoch seconds; created_at_ms = J7's createdAt in ms, NULL when absent or 0
(quoted_tweet sends 0 for unknown). Payloads are stored whole as JSON.

Credentials: J7_TOKEN_2 is re-read from the state root's .j7.env on every (re)connect. This process
never refreshes it: the funded runner's J7Session owns the rotating chain (AGENTS.md, J7 session
renewal). Only the feed socket is opened, never the wallets socket the funded relay reads.

    MEMECOIN_DESK_X_PRODUCER_ENABLED=1 python3 scripts/memecoin_desk_x_producer.py
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.feeds.j7_feed import HEADERS, load_j7_env, token_exp     # noqa: E402
from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

FEED_URL = "https://nyc.j7tracker.io"
TOKEN_KEY = "J7_TOKEN_2"
POSTS = {"tweet", "tweet_update", "quoted_tweet", "reply_tweet", "external_message", "tweet_deleted"}
SUGGESTIONS = {"ai_suggestion", "ai_suggestion_update"}
HEARTBEAT_S = 10.0
RETENTION_DAYS = 30                       # default; ~0.46 GB/day measured 2026-10-21 (30-min supervised run)
AUTH_BACKOFF_S = 900.0                                      # one refused token retried at most this often

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, event TEXT NOT NULL, tweet_id TEXT,
    received_at REAL NOT NULL, created_at_ms INTEGER, handle TEXT, author_id TEXT, post_type TEXT,
    payload_json TEXT NOT NULL, clock_offset_s REAL);
CREATE INDEX IF NOT EXISTS idx_posts_tweet ON posts(tweet_id);
CREATE INDEX IF NOT EXISTS idx_posts_handle ON posts(handle, received_at);
CREATE INDEX IF NOT EXISTS idx_posts_received ON posts(received_at);
CREATE TABLE IF NOT EXISTS ai_suggestion (id INTEGER PRIMARY KEY, event TEXT NOT NULL, tweet_id TEXT,
    ticker TEXT, ai_ticker TEXT, received_at REAL NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ai_suggestion_tweet ON ai_suggestion(tweet_id);
CREATE TABLE IF NOT EXISTS token_meta (id INTEGER PRIMARY KEY, tweet_id TEXT, mint TEXT, chain TEXT,
    symbol TEXT, received_at REAL NOT NULL, payload_json TEXT NOT NULL, clock_offset_s REAL);
CREATE INDEX IF NOT EXISTS idx_token_meta_mint ON token_meta(mint);
CREATE TABLE IF NOT EXISTS other (id INTEGER PRIMARY KEY, event TEXT NOT NULL, received_at REAL NOT NULL,
    payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL,
    detail TEXT, clock_offset_s REAL, session_id TEXT);
CREATE INDEX IF NOT EXISTS idx_connection_at ON connection(at);
"""


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _ms(value):
    """J7 createdAt as int ms; None when absent, 0, or not an integer (an ISO string stays in the payload)."""
    return value if type(value) is int and value > 0 else None


def _post(event, p, at, clock_offset_s=None):
    tweet = p.get("tweet") if event == "tweet_deleted" and isinstance(p.get("tweet"), dict) else p
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    return ("posts", {"event": event, "tweet_id": str(p["id"]) if p.get("id") is not None else None,
                      "received_at": at, "created_at_ms": _ms(tweet.get("createdAt", tweet.get("created_at"))),
                      "handle": author.get("handle") or tweet.get("handle"),
                      "author_id": author.get("id") or tweet.get("authorId"), "post_type": tweet.get("type"),
                      "payload_json": json.dumps(p, ensure_ascii=False, sort_keys=True), 'clock_offset_s': clock_offset_s})


def rows_for(event, payload, at, *, clock_offset_s=None):
    """[(table, row)] for one frame. Never raises on a payload shape: an unexpected shape goes to other."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        if event == "initialTweets" and isinstance(payload, list):
            return [_post("initialTweets", p, at, clock_offset_s) for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            if event in POSTS:
                return [_post(event, payload, at, clock_offset_s)]
            if event in SUGGESTIONS:
                return [("ai_suggestion", {"event": event, "tweet_id": payload.get("tweet_id"),
                                           "ticker": payload.get("ticker"), "ai_ticker": payload.get("ai_ticker"),
                                           "received_at": at, "payload_json": raw})]
            if event == "token_meta":
                meta = payload.get("tokenMeta") if isinstance(payload.get("tokenMeta"), dict) else {}
                return [("token_meta", {"tweet_id": payload.get("id"), "mint": payload.get("mint") or meta.get("mint"),
                                        "chain": meta.get("chain"), "symbol": meta.get("symbol"),
                                        "received_at": at, "payload_json": raw, 'clock_offset_s': clock_offset_s})]
    except (TypeError, ValueError, AttributeError):
        pass
    return [("other", {"event": event, "received_at": at, "payload_json": raw})]


class DayStore:
    """One open SQLite file per UTC day of received_at; one writer (the main thread)."""

    def __init__(self, out_dir, retention_days=RETENTION_DAYS):
        self.out_dir, self.day, self.conn = Path(out_dir), None, None
        self.retention_days = retention_days

    def prune(self, day):
        """Day files older than the retention window go (with their WAL/SHM); 0 or less keeps all."""
        if self.retention_days <= 0:
            return
        oldest = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for path in self.out_dir.glob("????-??-??.db*"):
            if path.name[:10] < oldest:
                path.unlink(missing_ok=True)

    def _for(self, at):
        day = utc_day(at)
        if day != self.day:
            if self.conn is not None:
                self.conn.commit()                          # yesterday's rows before its file closes
                self.conn.close()
            self.conn = sqlite3.connect(self.out_dir / ("%s.db" % day), timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            for table, additions in (('posts', [('clock_offset_s','REAL')]),
                    ('token_meta', [('clock_offset_s','REAL')]),
                    ('connection', [('clock_offset_s','REAL'),('session_id','TEXT')])):
                existing = {r[1] for r in self.conn.execute('PRAGMA table_info('+table+')')}
                for column, sql_type in additions:
                    if column not in existing:
                        self.conn.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+sql_type)
            self.day = day
            self.prune(day)
        return self.conn

    def write(self, items):
        """items: [(table, row)] in arrival order; committed per call."""
        for table, row in items:
            conn = self._for(row.get("received_at", row.get("at")))
            cols = sorted(row)
            conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
                         [row[c] for c in cols])
        if self.conn is not None:
            self.conn.commit()

    def close(self):
        if self.conn is not None:
            self.conn.close()


def current_token(env_path):
    """J7_TOKEN_2 as the runner last saved it; None when absent. Never logged."""
    return load_j7_env(env_path).get(TOKEN_KEY) or None


def fingerprint(token):
    return hashlib.sha256(str(token).encode()).hexdigest()[:12]


def backoff_left(marker, token, now):
    """Seconds this token must still wait after a refusal: 0 once the runner saved a new token or the
    window passed (Gamma M1: a refused token must not hit the funded relay's account every minute)."""
    try:
        m = json.loads(marker.read_text())
        if m["fp"] == fingerprint(token):
            return max(0.0, float(m["at"]) + AUTH_BACKOFF_S - now)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0.0


class Feed:
    """What the socket thread hands the main thread: frames via the queue, state under a lock."""

    def __init__(self, stop, *, clock=time.time, clock_offset_fn=lambda: None):
        self.inbox, self.stop, self.lock, self.refused = queue.Queue(), stop, threading.Lock(), None
        self.clock, self.clock_offset_fn = clock, clock_offset_fn
        self.session_id, self.epoch_at = uuid.uuid4().hex, clock()
        self.state = {"connected": False, "counts": {}, "last_frame_at": None, "last_error": None}
        self.inbox.put(('connection', self._connection('epoch', self.epoch_at, 'desk-x-coverage-v2')))

    def _offset(self):
        try:
            value = self.clock_offset_fn()
            return value if type(value) in (int,float) and math.isfinite(value) else None
        except Exception:
            return None

    def _connection(self, kind, at, detail=None):
        return dict(at=at, kind=kind, detail=detail, clock_offset_s=self._offset(), session_id=self.session_id)

    def report(self, kind, detail=None):
        with self.lock:
            self.state["connected"] = kind == "connect"
            if kind != "connect":
                self.state["last_error"] = kind
            self.inbox.put(('connection', self._connection(kind, self.clock(), detail)))
        if kind in ("auth_error", "connect_error"):
            # J7 answers a refused token with connect_error {'message': 'Invalid token'} (tap 2026-09-09);
            # transport loss reconnects without one. Stop rather than let socketio retry it forever.
            self.refused, self.stop["now"] = kind, True

    def frame(self, event, payload):
        with self.lock:
            at = self.clock()
            self.state["last_frame_at"] = at
            self.state["counts"][event] = self.state["counts"].get(event, 0) + 1
            for item in rows_for(event, payload, at, clock_offset_s=self._offset()):
                self.inbox.put(item)

    def checkpoint(self):
        """Drain under the capture lock; publish the frontier only after the writer commits it."""
        with self.lock:
            at, items = self.clock(), []
            while not self.inbox.empty():
                items.append(self.inbox.get_nowait())
            proof = self._connection('heartbeat', at, json.dumps({'connected':self.state['connected']}))
            items.append(('connection', proof))
            beat = {**self.state, 'counts':dict(self.state['counts']), 'ts':at, 'queue':0,
                    'session_id':self.session_id, 'epoch_at':self.epoch_at,
                    'clock_offset_s':proof['clock_offset_s'], 'committed_through':at}
            return items, beat

    def snapshot(self):
        with self.lock:
            return {**self.state, "counts": dict(self.state["counts"])}


def make_client(env_path, feed):
    import socketio
    sio = socketio.Client(reconnection=True, reconnection_delay=5, reconnection_delay_max=120,
                          logger=False, engineio_logger=False)

    @sio.event
    def connect():
        feed.report("connect")
        sio.emit("user_connected", current_token(env_path))         # what the web client sends

    @sio.event
    def connect_error(data):
        feed.report("connect_error", str(data)[:200])

    @sio.event
    def disconnect(*_):
        feed.report("disconnect")

    @sio.on("auth_error")
    def on_auth_error(data=None):
        feed.report("auth_error", str(data)[:200])

    @sio.on("*")
    def on_any(event, *args):
        feed.frame(event, args[0] if args else None)

    return sio


def main(argv=None, *, env_root=None, client_factory=make_client, stop=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    load_env_files(str(env_root or state_root()))
    if os.getenv("MEMECOIN_DESK_X_PRODUCER_ENABLED", "0").strip() != "1":
        print("[desk-x] MEMECOIN_DESK_X_PRODUCER_ENABLED is not 1; exiting")
        return 0
    try:                                       # after load_env_files: cron's .env value applies (Gamma)
        retention = int(os.getenv("MEMECOIN_DESK_X_RETENTION_DAYS", str(RETENTION_DAYS)))
    except ValueError:
        print("[desk-x] MEMECOIN_DESK_X_RETENTION_DAYS is not an integer; exiting", file=sys.stderr)
        return 2
    root = Path(env_root or state_root())
    env_path = Path(os.getenv("J7_ENV_FILE") or root / ".j7.env")
    if current_token(env_path) is None:
        print("[desk-x] no %s in %s; exiting" % (TOKEN_KEY, env_path), file=sys.stderr)
        return 2
    out_dir = root / "data" / "desk" / "x"
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = open(out_dir / "producer.lock", "w")
    try:                                                    # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-x] another producer holds the lock; exiting")
        return 0
    marker = out_dir / "auth_backoff.json"
    left = backoff_left(marker, current_token(env_path), time.time())
    if left > 0:
        print("[desk-x] token refused earlier; backing off %.0f s more (or until .j7.env changes)" % left)
        return 0

    def refuse(kind):
        token = current_token(env_path)
        marker.write_text(json.dumps({"fp": fingerprint(token), "at": time.time(), "kind": kind}))
        print("[desk-x] %s; no reconnect for %.0f s on this token" % (kind, AUTH_BACKOFF_S), file=sys.stderr)

    stop = stop or {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    from sniper.services.memecoin_desk_intake import clock_offset
    feed = Feed(stop, clock_offset_fn=lambda: clock_offset(root/'data'/'clock_offset.json'))
    inbox = feed.inbox
    sio = client_factory(env_path, feed)
    store, beat_at = DayStore(out_dir, retention), 0.0
    try:
        sio.connect(FEED_URL, socketio_path="socket.io", transports=["websocket"], headers=HEADERS,
                    auth=lambda: {"token": current_token(env_path)}, wait_timeout=20)
    except Exception as exc:                                # the client keeps no retry after a first failure
        print("[desk-x] first connect failed: %s" % type(exc).__name__, file=sys.stderr)
        refuse("first_connect_failed")
        return 3
    try:
        while not stop["now"]:
            items = []
            try:
                items.append(inbox.get(timeout=1.0))
                while True:
                    items.append(inbox.get_nowait())
            except queue.Empty:
                pass
            if items:
                store.write(items)
            now = time.time()
            if now - beat_at >= HEARTBEAT_S:
                beat_at = now
                pending, beat = feed.checkpoint()
                store.write(pending)
                tmp = out_dir / "producer.heartbeat.tmp"
                tmp.write_text(json.dumps({**beat,
                                           "token_exp": token_exp(current_token(env_path))}))
                tmp.replace(out_dir / "producer.heartbeat")
    except KeyboardInterrupt:
        pass
    finally:
        sio.disconnect()
        rest = []
        while not inbox.empty():
            rest.append(inbox.get_nowait())
        store.write(rest)
        store.close()
    if feed.refused:
        refuse(feed.refused)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
