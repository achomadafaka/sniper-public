#!/usr/bin/env python3
"""4663 WebSocket shadow: does a second provider deliver the push lane's logs that ours misses?

Alpha order 2026-10-08 (operator yes): a 48 h free trial before buying a dedicated 4663 socket.
Standalone and read-only. It never touches the funded watcher (scripts/research/onchain_evm_wallet_watch.py)
or the ledger: it reads the watched roster and J7 events through a read-only handle and writes only --db.

SOCKETS (each its own connection, the push lane's three log filters, no newHeads):
  drpc        wss form of MEMECOIN_RESEARCH_DRPC_RPC_URL_4663 (the keyed research account). Every
              subscribe goes through the account's shared pacer; a limit answer (429, code 15) pauses
              every user of the account and STOPS this run (operator rule: stop on 429).
  publicnode  wss://robinhood-rpc.publicnode.com, the host the funded watcher subscribes on today.
  The chain-owner host has no JSON-RPC WebSocket (wss://rpc.mainnet.chain.robinhood.com -> HTTP 400,
  /ws -> 404, probed 2026-10-08), so "public" is not a socket here. The funded watcher's own receipts
  (onchain_wallet_detections, '4663:<tx>:<mint>') are the third column of the J7 comparison.

FILTERS: Transfers TO watched addresses, Transfers FROM them, and Initialize on our PoolManager, built
by the push lane's own functions from the same roster function (evm_watched_owners), re-read every
--roster-s; a change resubscribes every socket and is recorded as a new roster epoch.

EDGE CASES, mapped before building:
- Money path: none. No ledger write, no signing, its own process and DB.
- Fill path: none of its work is on it. publicnode is per-IP keyless and the funded watcher uses it,
  so this socket only subscribes (three requests per connect) and reconnects no faster than 5 s; a
  refusal backs off 60 s to 600 s. --sockets drpc runs without it.
- Concurrency: one thread per socket, one writer thread owns the DB (queue), one J7 thread.
- Absent vs zero: a log a socket never delivered counts as a MISS only while that socket had all its
  filters acked for the roster epoch of the log; otherwise it is DOWN. Unknown is never a miss.
- Failures: connect error / close / silence -> reconnect with backoff, recorded as an event; dRPC limit
  -> stop the run with stop_reason; a roster read error keeps the previous set.
- Units: receipts are host wall-clock seconds (time.time), one clock for every socket. Block and log
  index are decimal ints. J7 received_at is our receipt time of the J7 event, also host clock.
- Sample: logs that at least one socket delivered. A log no socket delivered is invisible here; the
  J7 table is the check on that (a watched buy no socket delivered).
- Funded guard (Gamma, c3beeead9 review): publicnode's per-IP limits are unknown and the funded watcher
  subscribes there from this IP. While our publicnode socket is up, any provider refusal in the funded
  watcher's log, or 2+ of its push-lane reconnect lines (socket error, stall, silence, forced reconnect,
  socket dark) within 10 min, drops our publicnode socket for the rest of the run, recorded as a
  'dropped' event. Baseline (Gamma): the funded lane stalls about once every few hours alone, so one
  line proves nothing. Its J7 column then falls back to the funded watcher's own detections.
- Rollback: stop the process; delete --db. Nothing else changes.

    python3 scripts/research/evm_4663_ws_shadow.py --hours 48            # run
    python3 scripts/research/evm_4663_ws_shadow.py --report              # read the DB
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_ROOT = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
sys.path.insert(0, str(REPO))

CHAIN = "4663"
PUBLICNODE = "wss://robinhood-rpc.publicnode.com"
DEFAULT_DB = STATE_ROOT / "data" / "research_4663_ws_shadow.db"
RECONNECT_MIN_S, RECONNECT_MAX_S = 5.0, 60.0
REFUSAL_MIN_S, REFUSAL_MAX_S = 60.0, 600.0
SILENT_S = 600.0            # no message at all on a socket for this long: reconnect (Initialize ~6/min)
J7_PERIOD_S = 10.0
FUNDED_LOG = STATE_ROOT / "logs" / "onchain_evm_armed.log"
# The funded push lane's own fault lines (memecoin_onchain_push / onchain_evm_wallet_watch). A roster
# resubscribe ("armed set changed") and the "push mode on" connect line are not faults.
FUNDED_FAULT = re.compile(r"push lane: (socket error|newHeads stalled|socket silent|"
                          r"caller forced a reconnect)|socket dark|cross-check: forcing a reconnect")
FUNDED_REFUSAL = re.compile(r"push lane: provider refused")
FAULTS_TO_DROP, FAULT_WINDOW_S = 2, 600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at REAL, ends_at REAL,
    args_json TEXT, stopped_at REAL, stop_reason TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run_id TEXT, socket TEXT, at REAL,
    kind TEXT, epoch INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS logs (tx TEXT, log_index INTEGER, block INTEGER, sub TEXT, address TEXT,
    topic1 TEXT, topic2 TEXT, removed INTEGER, epoch INTEGER, first_at REAL, block_hash TEXT,
    PRIMARY KEY (tx, log_index));
CREATE TABLE IF NOT EXISTS receipts (tx TEXT, log_index INTEGER, socket TEXT, at REAL, n INTEGER,
    PRIMARY KEY (tx, log_index, socket));
CREATE TABLE IF NOT EXISTS retracted_logs (tx TEXT, log_index INTEGER, block INTEGER, block_hash TEXT,
    PRIMARY KEY (tx, log_index, block, block_hash));
CREATE TABLE IF NOT EXISTS j7 (event_id TEXT PRIMARY KEY, tx TEXT, trader TEXT, mint TEXT,
    received_at REAL, epoch INTEGER);
CREATE INDEX IF NOT EXISTS idx_receipts_tx ON receipts (tx);
CREATE INDEX IF NOT EXISTS idx_events_socket ON events (socket, at);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    if "block_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(logs)")}:
        conn.execute("ALTER TABLE logs ADD COLUMN block_hash TEXT")


def drpc_ws_url():
    http = os.getenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_%s" % CHAIN) or ""
    if "lb.drpc.org/ogrpc" not in http:
        return None
    return http.replace("https://lb.drpc.org/ogrpc", "wss://lb.drpc.org/ogws")


def parse_log(result):
    """(tx, log_index, block, address, topics, removed, block_hash), or None."""
    try:
        return (str(result["transactionHash"]).lower(), int(str(result["logIndex"]), 16),
                int(str(result["blockNumber"]), 16), str(result.get("address") or "").lower(),
                [str(t).lower() for t in result.get("topics") or []], int(bool(result.get("removed"))),
                str(result.get("blockHash") or "").lower() or None)
    except (KeyError, TypeError, ValueError):
        return None


def is_limit(error, *, http_status=None):
    from sniper.services.memecoin_research_pacer import _drpc_limit
    return http_status == 429 or _drpc_limit(error)


class Roster:
    """The watched set, re-read read-only; `epoch` increments on every change."""

    def __init__(self, read_fn):
        self.read_fn, self.lock = read_fn, threading.Lock()
        self.addresses, self.epoch = (), 0
        self.refresh()

    def refresh(self):
        try:
            new = tuple(sorted(self.read_fn()))
        except Exception:
            return False
        with self.lock:
            if new == self.addresses:
                return False
            self.addresses, self.epoch = new, self.epoch + 1
            return True

    def current(self):
        with self.lock:
            return self.epoch, self.addresses


def filters(addresses, pool_manager, init_topic):
    from sniper.services import memecoin_onchain_push as push
    out = {}
    for name, f in (("transfers", push.armed_transfer_filter(addresses)),
                    ("sells", push.sell_transfer_filter(addresses)),
                    ("initialize", push.initialize_filter(pool_manager, init_topic))):
        if f:
            out[name] = f
    return out


class Shadow:
    def __init__(self, *, db_path, sockets, roster, pool_manager, init_topic, connect_fn=None,
                 pacer=None, clock=time.time, run_id=None, j7_read=None, sleep=time.sleep,
                 funded_log=None):
        self.db_path, self.sockets, self.roster = str(db_path), dict(sockets), roster
        self.pool_manager, self.init_topic = pool_manager, init_topic
        self.connect_fn, self.pacer, self.clock, self.sleep = connect_fn, pacer, clock, sleep
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.q, self.stop = queue.Queue(), threading.Event()
        self.stop_reason, self.j7_read = None, j7_read
        self.up, self.dropped = {}, set()
        self.funded_log = funded_log          # socket -> fully acked now; sockets dropped for good

    # --- one socket ------------------------------------------------------------------------------
    def _event(self, socket, kind, epoch=None, detail=None):
        self.q.put(("event", socket, self.clock(), kind, epoch, detail))

    def _halt(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop.set()

    def run_socket(self, name, url):
        if self.connect_fn is None:
            from websockets.sync.client import connect as connect_fn
        else:
            connect_fn = self.connect_fn
        delay, refusal = RECONNECT_MIN_S, 0.0
        while not self.stop.is_set() and name not in self.dropped:
            epoch, addresses = self.roster.current()
            subs = filters(addresses, self.pool_manager, self.init_topic)
            wait = delay
            try:
                if name == "drpc" and self.pacer is not None:
                    self.pacer.acquire()
                with connect_fn(url, open_timeout=10) as ws, self._session(name):
                    self._event(name, "connect", epoch)
                    pending, rid, acked = {}, 0, {}
                    for sub, params in subs.items():
                        if name == "drpc" and self.pacer is not None and rid:
                            self.pacer.acquire()
                        rid += 1
                        pending[rid] = sub
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "eth_subscribe",
                                            "params": params}))
                    last_msg = self.clock()
                    while not self.stop.is_set():
                        if name in self.dropped:
                            break
                        if self.roster.current()[0] != epoch:
                            self._event(name, "resubscribe", epoch)
                            wait = 0.0
                            break
                        if self.clock() - last_msg > SILENT_S:
                            self._event(name, "down", epoch, "silent %.0fs" % SILENT_S)
                            break
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        last_msg = self.clock()
                        msg = json.loads(raw)
                        if "id" in msg and msg.get("id") in pending:
                            sub = pending.pop(msg["id"])
                            if msg.get("error") is not None:
                                if name == "drpc" and is_limit(msg["error"]):
                                    self._limit(name, msg["error"])
                                    return
                                self._event(name, "refused", epoch, "%s: %s" % (sub, msg["error"]))
                                refusal = min(REFUSAL_MAX_S, max(REFUSAL_MIN_S, refusal * 2))
                                wait = refusal
                                break
                            acked[str(msg.get("result"))] = sub
                            if len(acked) == len(subs):
                                self._event(name, "up", epoch, ",".join(sorted(acked.values())))
                                self.up[name] = True
                                delay, refusal = RECONNECT_MIN_S, 0.0
                            continue
                        params = msg.get("params") or {}
                        if msg.get("method") == "eth_subscription" and params.get("subscription") in acked:
                            log = parse_log(params.get("result"))
                            if log:
                                self.q.put(("log", name, last_msg, acked[params["subscription"]], epoch, log))
                        elif msg.get("error") is not None and name == "drpc" and is_limit(msg["error"]):
                            self._limit(name, msg["error"])
                            return
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if name == "drpc" and status == 429:
                    self._limit(name, "HTTP 429")
                    return
                self._event(name, "down", epoch, "%s: %s" % (type(exc).__name__, str(exc)[:200]))
                delay = min(RECONNECT_MAX_S, delay * 2)
            if wait and name not in self.dropped:
                self.stop.wait(wait)

    @contextlib.contextmanager
    def _session(self, name):
        try:
            yield
        finally:
            was_up = self.up.get(name)
            self.up[name] = False
            if name in self.dropped:
                self._event(name, "down", None, "dropped")
            elif was_up:
                # A NORMAL END IS RECORDED TOO (review SF01): without it the session stayed open in
                # the events and only the run's stop closed it.
                self._event(name, "down", None, "session_end")

    def funded_fault(self, line, socket="publicnode"):
        """A funded push-lane line seen while our `socket` is up: any refusal, or the second reconnect
        within FAULT_WINDOW_S, drops it (Gamma's guard). Returns True when it dropped it."""
        refusal = bool(FUNDED_REFUSAL.search(line))
        if not (refusal or FUNDED_FAULT.search(line)) or socket in self.dropped or not self.up.get(socket):
            return False
        now = self.clock()
        self.faults = [t for t in getattr(self, "faults", []) if now - t <= FAULT_WINDOW_S] + [now]
        if not refusal and len(self.faults) < FAULTS_TO_DROP:
            self._event(socket, "funded_fault", None, line.strip()[-200:])
            return False
        self.dropped.add(socket)
        self._event(socket, "dropped", None, "funded fault: %s" % line.strip()[-200:])
        return True

    def tail_funded_log(self, path, poll_s=2.0):
        """Follow the funded watcher's log from its end at start; a rotated or truncated file is
        reopened and read from its top."""
        fh, inode = None, None
        while not self.stop.is_set() and "publicnode" in self.sockets and "publicnode" not in self.dropped:
            try:
                st = os.stat(path)
                if fh is None or st.st_ino != inode or st.st_size < fh.tell():
                    first = fh is None
                    if fh is not None:
                        fh.close()
                    fh, inode = open(path, "r", errors="replace"), st.st_ino
                    if first:
                        fh.seek(0, os.SEEK_END)
                for line in fh.readlines():
                    self.funded_fault(line)
            except OSError as exc:
                self._event("funded_log", "read_failed", None, type(exc).__name__)
            self.stop.wait(poll_s)
        if fh is not None:
            fh.close()

    def _limit(self, name, error):
        from sniper.services.memecoin_research_pacer import DRPC_LIMIT_PAUSE_S
        if self.pacer is not None:
            self.pacer.pause(DRPC_LIMIT_PAUSE_S)
        self._event(name, "limit", None, str(error)[:200])
        self._halt("drpc_limit: %s" % str(error)[:120])

    # --- the writer --------------------------------------------------------------------------------
    def writer(self, conn):
        last_commit = self.clock()
        while not (self.stop.is_set() and self.q.empty()):
            try:
                item = self.q.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item is not None:
                self.write(conn, item)
            if self.clock() - last_commit >= 1.0:
                conn.commit()
                last_commit = self.clock()
        conn.commit()

    def write(self, conn, item):
        kind = item[0]
        if kind == "event":
            _k, socket, at, ev, epoch, detail = item
            conn.execute("INSERT INTO events(run_id, socket, at, kind, epoch, detail) VALUES (?,?,?,?,?,?)",
                         (self.run_id, socket, at, ev, epoch, detail))
        elif kind == "log":
            _k, socket, at, sub, epoch, (tx, idx, block, address, topics, removed, block_hash) = item
            identity = (tx, idx, block, block_hash or "")
            if removed:
                conn.execute("INSERT OR IGNORE INTO retracted_logs VALUES (?,?,?,?)", identity)
            elif conn.execute("SELECT 1 FROM retracted_logs WHERE tx=? AND log_index=? AND block=? "
                              "AND block_hash=?", identity).fetchone():
                # shortcut: a reused retracted hash stays censored; add canonical proof if needed.
                return
            old = conn.execute("SELECT block, block_hash, removed, epoch FROM logs WHERE tx=? AND log_index=?",
                               (tx, idx)).fetchone()
            same_block = old is not None and old[:2] == (block, block_hash)
            if same_block:
                epoch = old[3]
            if old is not None:
                if removed and not same_block and old[1] is not None:
                    return                     # a late removal of an earlier inclusion
                if not removed and same_block and old[2]:
                    return                     # a repeated old live log cannot undo its retraction
                if not removed and not same_block:
                    conn.execute("INSERT OR IGNORE INTO retracted_logs VALUES (?,?,?,?)",
                                 (tx, idx, old[0], old[1] or ""))
                    conn.execute("DELETE FROM receipts WHERE tx=? AND log_index=?", (tx, idx))
            conn.execute("INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tx, log_index) "
                         "DO UPDATE SET block=excluded.block, block_hash=excluded.block_hash, "
                         "removed=excluded.removed, epoch=excluded.epoch, sub=excluded.sub, "
                         "address=excluded.address, topic1=excluded.topic1, topic2=excluded.topic2",
                         (tx, idx, block, sub, address, topics[1] if len(topics) > 1 else None,
                          topics[2] if len(topics) > 2 else None, removed, epoch, at, block_hash))
            if not removed:
                conn.execute("INSERT INTO receipts VALUES (?,?,?,?,1) ON CONFLICT(tx, log_index, socket) "
                             "DO UPDATE SET n=n+1", (tx, idx, socket, at))
        elif kind == "j7":
            _k, event_id, tx, trader, mint, received_at, epoch = item
            conn.execute("INSERT OR IGNORE INTO j7 VALUES (?,?,?,?,?,?)",
                         (event_id, tx, trader, mint, received_at, epoch))

    # --- J7 buys by watched wallets ----------------------------------------------------------------
    def j7_loop(self):
        cursor = None
        while not self.stop.wait(J7_PERIOD_S if cursor is not None else 0):
            try:
                cursor, rows = self.j7_read(cursor)
            except Exception as exc:
                self._event("j7", "read_failed", None, type(exc).__name__)
                continue
            epoch, watched = self.roster.current()
            for event_id, tx, trader, mint, received_at in rows:
                if trader in watched:
                    self.q.put(("j7", event_id, tx, trader, mint, received_at, epoch))

    def run(self, hours, conn):
        conn.execute("INSERT INTO runs(run_id, started_at, ends_at, args_json) VALUES (?,?,?,?)",
                     (self.run_id, self.clock(), self.clock() + hours * 3600,
                      json.dumps({"sockets": sorted(self.sockets), "hours": hours})))
        conn.commit()
        threads = [threading.Thread(target=self.run_socket, args=(n, u), name="ws-" + n, daemon=True)
                   for n, u in self.sockets.items()]
        if self.j7_read is not None:
            threads.append(threading.Thread(target=self.j7_loop, name="j7", daemon=True))
        if self.funded_log is not None:
            threads.append(threading.Thread(target=self.tail_funded_log, args=(self.funded_log,),
                                            name="funded-log", daemon=True))
        writer = threading.Thread(target=self.writer, args=(conn,), name="writer", daemon=True)
        for t in threads + [writer]:
            t.start()
        end = self.clock() + hours * 3600
        while not self.stop.is_set() and self.clock() < end:
            if self.roster.refresh():
                self._event("roster", "epoch", self.roster.current()[0],
                            "%d addresses" % len(self.roster.current()[1]))
            self.stop.wait(60.0)
        self._halt(self.stop_reason or "duration")
        for t in threads:
            t.join(timeout=15)
        writer.join(timeout=30)
        conn.execute("UPDATE runs SET stopped_at=?, stop_reason=? WHERE run_id=?",
                     (self.clock(), self.stop_reason, self.run_id))
        conn.commit()


def drpc_account_pacer():
    """The keyed dRPC account's one shared pacer (memecoin_research_pacer): every subscribe and the
    connect pass `acquire`, a limit answer `pause`s every user of the account. Built here so the
    account-pacer guard (tests/test_memecoin_research_pacer.py) sees the canonical pacer called."""
    from sniper.services.memecoin_research_pacer import DRPC_GAP_S, SharedPacer
    pacer = SharedPacer.named("drpc-keyed", gap_s=DRPC_GAP_S)

    class Paced:
        def acquire(self):
            pacer.acquire()

        def pause(self, seconds):
            return pacer.pause(seconds)
    return Paced()


# --- the ledger reads (read-only) ------------------------------------------------------------------
def ledger_ro():
    from sniper.persistence.db_core import get_readonly_connection
    path = Path(os.getenv("DB_PATH") or "sniper_tracker.db")      # .env carries it relative
    conn = get_readonly_connection(str(path if path.is_absolute() else STATE_ROOT / path))
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def make_j7_read(conn_fn):
    """J7 4663 buys after the cursor (starts at the newest id: this run's buys only). The handle is
    opened on the first call, on the J7 thread that uses it."""
    holder = []

    def read(cursor):
        if not holder:
            holder.append(conn_fn())
        conn = holder[0]
        if cursor is None:
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM memecoin_events").fetchone()[0]
            return top, []
        rows = conn.execute(
            "SELECT id, fomo_event_id, mint, received_at FROM memecoin_events WHERE id > ? "
            "AND network_id=? AND side='buy' AND delivery_lane='j7_wallets' ORDER BY id",
            (cursor, CHAIN)).fetchall()
        out = []
        for rid, fid, mint, received in rows:
            cursor = max(cursor, rid)
            parts = str(fid or "").split(":")
            if len(parts) < 5 or parts[0] != "swap":
                continue
            try:
                at = datetime.fromisoformat(str(received).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue
            out.append((str(fid), parts[2].lower(), parts[3].lower(), mint, at))
        return cursor, out
    return read


# --- the report -------------------------------------------------------------------------------------
def run_end(conn, run_id, started, ends, stopped, newest, now):
    """Where a run's open sessions end: its stopped_at; `now` while it is the newest run and not past
    its ends_at (live); else its last recorded event (it stopped without a record, so nothing after
    that is known)."""
    if stopped is not None:
        return stopped
    if run_id == newest and now <= (ends if ends is not None else now):
        return now
    last = conn.execute("SELECT MAX(at) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
    return last if last is not None else started


def up_intervals(conn, socket, now=None):
    """[(start, end, epoch)] while `socket` had every filter acked, each inside its OWN run (review
    SF01, 2026-10-08): a session left open closes at run_end(), never at another run's stop, and
    epochs are per process, so time containment in the run is what binds a log to a session."""
    now = time.time() if now is None else now
    runs = conn.execute("SELECT run_id, started_at, ends_at, stopped_at FROM runs "
                        "ORDER BY started_at").fetchall()
    newest = runs[-1][0] if runs else None
    out = []
    for run_id, started, ends, stopped in runs:
        start = epoch = None
        for at, kind, ep in conn.execute("SELECT at, kind, epoch FROM events WHERE socket=? AND run_id=? "
                                         "ORDER BY at, id", (socket, run_id)):
            if kind == "up":
                start, epoch = at, ep
            elif kind in ("down", "resubscribe", "refused", "limit", "connect") and start is not None:
                out.append((start, at, epoch))
                start = None
        if start is not None:
            out.append((start, max(start, run_end(conn, run_id, started, ends, stopped, newest, now)),
                        epoch))
    return out


def was_up(intervals, at, epoch):
    return any(s <= at <= e and (epoch is None or ep == epoch) for s, e, ep in intervals)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def report(conn, sockets, ledger=None, grace_s=120.0):
    lines = []
    up = {s: up_intervals(conn, s) for s in sockets}
    logs = conn.execute("SELECT l.tx, l.log_index, l.sub, l.epoch, COALESCE("
                        "(SELECT MIN(r.at) FROM receipts r WHERE r.tx=l.tx AND r.log_index=l.log_index), "
                        "l.first_at) FROM logs l WHERE COALESCE(l.removed,0)=0").fetchall()
    rec = {}
    for tx, idx, socket, at in conn.execute("SELECT tx, log_index, socket, at FROM receipts"):
        rec[(tx, idx, socket)] = at
    runs = conn.execute("SELECT run_id, started_at, ends_at, stopped_at, stop_reason FROM runs "
                        "ORDER BY started_at").fetchall()
    for run_id, started, ends, stopped, reason in runs:
        live = stopped is None and run_id == runs[-1][0] and time.time() <= (ends or 0)
        lines.append("run %s %s -> %s stop=%s%s" % (
            run_id, started, stopped, reason,
            " (live)" if live else " (no stop recorded: its open sessions end at its last event)"
            if stopped is None else ""))
    for s in sockets:
        seen = missed = down = 0
        lags = []
        miss_by_sub = {}
        for tx, idx, sub, epoch, first in logs:
            at = rec.get((tx, idx, s))
            if at is not None:
                seen += 1
                lags.append(1000 * (at - first))
            elif was_up(up[s], first, epoch):
                missed += 1
                miss_by_sub[sub] = miss_by_sub.get(sub, 0) + 1
            else:
                down += 1
        uptime = sum(e - b for b, e, _ in up[s])
        notes = conn.execute("SELECT COALESCE(SUM(n), 0) FROM receipts WHERE socket=?", (s,)).fetchone()[0]
        dropped = conn.execute("SELECT MIN(at) FROM events WHERE socket=? AND kind='dropped'", (s,)).fetchone()[0]
        lines.append("%-10s logs seen %d, MISSED while up %d %s, socket down %d | lag behind first "
                     "p50 %s ms p90 %s ms | up %.1f h" % (s, seen, missed, miss_by_sub, down,
                     None if not lags else round(pct(lags, .5)), None if not lags else round(pct(lags, .9)),
                     uptime / 3600))
        lines.append("%-10s notifications %d = %.2f/min while up%s" % (
            s, notes, notes / (uptime / 60) if uptime > 0 else 0.0,
            "" if dropped is None else " | DROPPED at %s (funded fault)" % dropped))
    j7 = conn.execute("SELECT event_id, tx, trader, mint, received_at, epoch FROM j7").fetchall()
    lines.append("J7 4663 buys by watched wallets: %d" % len(j7))
    for event_id, tx, trader, mint, received, epoch in j7:
        # THE BUY'S OWN IDENTITY (review SF02, 2026-10-08): a cell is credited only by a Transfer of
        # THIS coin (the token contract is l.address) to THIS trader, never removed by a reorg; a
        # router tx moving several coins or paying several wallets must not lend this buy another's
        # delivery. Addresses compare lowercased.
        tx, trader, mint = str(tx).lower(), str(trader).lower(), str(mint or "").lower()
        cells = []
        for s in sockets:
            at = conn.execute("SELECT MIN(r.at) FROM receipts r JOIN logs l ON l.tx=r.tx AND "
                              "l.log_index=r.log_index WHERE r.tx=? AND r.socket=? "
                              "AND l.sub='transfers' AND lower(l.address)=? AND lower(l.topic2)=? "
                              "AND COALESCE(l.removed, 0)=0",
                              (tx, s, mint, "0x" + trader[2:].rjust(64, "0"))).fetchone()[0]
            if at is not None:
                cells.append("%s %+.1fs" % (s, at - received))
            else:
                cells.append("%s %s" % (s, "MISSED" if was_up(up[s], received, None) else "down"))
        if ledger is not None:
            # The funded watcher's canonical signature for this buy, and the same wallet.
            hit = ledger.execute("SELECT MIN(detected_at) FROM onchain_wallet_detections WHERE "
                                 "signature=? AND network_id=? AND lower(address)=?",
                                 ("%s:%s:%s" % (CHAIN, tx, mint), CHAIN, trader)).fetchone()[0]
            if hit:
                t = datetime.fromisoformat(str(hit).replace("Z", "+00:00")).timestamp()
                cells.append("funded_watcher %+.1fs" % (t - received))
            else:
                cells.append("funded_watcher none")
        lines.append("  %s %s %s %s" % (tx[:12], trader[:10], mint[:10], " | ".join(cells)))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--sockets", default="drpc,publicnode")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(STATE_ROOT)
    urls = {"drpc": drpc_ws_url(), "publicnode": PUBLICNODE}
    names = [n.strip() for n in a.sockets.split(",") if n.strip()]
    if any(urls.get(n) is None for n in names):
        raise SystemExit("unknown or unconfigured socket in %s" % names)
    conn = sqlite3.connect(a.db, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    if a.report:
        print("\n".join(report(conn, names, ledger=ledger_ro())))
        return
    lock = open(a.db + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another shadow run holds %s.lock" % a.db)
    from sniper.execution.evm_v4_quote import V4, _INIT_TOPIC
    from sniper.services.axiom_address_verify import evm_watched_owners
    ledger = ledger_ro()
    roster = Roster(lambda: evm_watched_owners(ledger, CHAIN).keys())
    shadow = Shadow(db_path=a.db, sockets={n: urls[n] for n in names}, roster=roster,
                    pool_manager=V4[CHAIN]["pool_manager"], init_topic=_INIT_TOPIC,
                    pacer=drpc_account_pacer(),
                    j7_read=make_j7_read(ledger_ro), funded_log=str(FUNDED_LOG))
    print("[4663-ws-shadow] run %s: sockets %s, %d watched addresses, %.1f h, db %s"
          % (shadow.run_id, names, len(roster.current()[1]), a.hours, a.db), flush=True)
    shadow.run(a.hours, conn)
    print("[4663-ws-shadow] stopped: %s" % shadow.stop_reason, flush=True)


if __name__ == "__main__":
    main()
