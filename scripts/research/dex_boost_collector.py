#!/usr/bin/env python3
"""DexScreener boost forward-return COLLECTOR, research only (Gamma prereg
docs/audits/2026-10-28-dexscreener-boost-forward-return-preregistration.md section 1, Alpha price-source ruling).

Events: the first appearance of a Solana mint in a CLASS, read from the desk intake store
data/desk/intake/dexscreener/<day>.db intake_rows: BOOST (filters boost_latest, boost_top) or PROFILE (filter profile).
"First" = first since 2026-10-22 in all intake history AND after this collector started (mints already seen are
left-censored, counted). first_seen_at = the intake row's observed_at (corrected UTC; NULL = unusable_clock, counted).
Every BOOST and every PROFILE event is priced the same way; matching to controls is the reader's (Gamma) job.

Pricing, executable only, round trip of a fixed 0.25 SOL clip: an exact BUY at first_seen + 30 s (window +30..+90 s),
then an exact SELL of the acquired quantity at +5 min, +15 min, +1 h, +4 h, +24 h (each within +-10 %). The price
source for every event and every offset is jupiter_capture = Jupiter /swap/v1/quote with the CAPTURE key only (never
the funded key, the funded seam or keyless lite-api). The original epoch used0.2req/s and a daily ceiling.
The2026-11-01 research resource epoch uses the keyed free provider limit1req/s, halved for10min after429,
and pauses while the paper arm (same key) is throttled above its own baseline. The local daily ceiling is removed.
(Amendment 7: one fill model; the PumpSwap tape only keeps ~21 min per pool, so it could not price +1 h). At offsets
<= +15 min, when the pool is on our tape, memecoin_desk_ps_paper also quotes into the diagnostics table, labelled
ps_tape_crosscheck: never a return, never matching.

States per (event, offset): known | route_absent | rate_limited | source_paused | provider_error | source_dead | late |
censored (cap_reached, entry_late, no_entry). Pause intervals and intake outages are rows in gaps.
A gap is never zero. DexScreener priceUsd is stored only as non_executable_diagnostic. Store:
data/research/dex_boost/<day>.db (the event's first_seen UTC day). Run at nice 19:

    env -i PATH="$PATH" HOME="$HOME" SNIPER_STATE_ROOT=<state root> \
        ionice -c3 nice -n19 python3 scripts/research/dex_boost_collector.py --loop [--capture-env FILE]

It never loads a .env file; the capture key comes only from the capture env file (mode 0600, that one variable), and
a secret-like variable name in its environment refuses start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

VERSION = "dex-boost-collector-v1"
HISTORY_FROM = "2026-10-22"
CLASSES = {"boost_latest": "BOOST", "boost_top": "BOOST", "profile": "PROFILE"}
RAW_ENDPOINTS = {"/token-boosts/latest/v1": "BOOST", "/token-boosts/top/v1": "BOOST", "/token-profiles/latest/v1": "PROFILE"}
CLIP_LAMPORTS = 250_000_000                      # 0.25 SOL, frozen by the prereg
ENTRY_AT, ENTRY_WINDOW = 30.0, (30.0, 90.0)
HORIZONS = (300.0, 900.0, 3600.0, 14400.0, 86400.0)
HORIZON_TOL = 0.10
# Keyed free plan:1RPS; operator removed the local daily research ceiling.
# https://developers.jup.ag/docs/portal/rate-limits
JUP_RATE, GLOBAL_RATE, DAILY_CAP = 1.0, 1.0, math.inf
TAPE_DIAG_MAX_OFFSET = 900.0                     # Amendment 7: ps_tape cross-check only at offsets <= +15 min
ROW_LIMIT = 5000
DUE_PAGE = 200                                   # due pending rows taken per day store per tick (indexed, oldest first)                                 # intake rows read per day file per tick (rowid range, indexed)
SOURCE_DEAD_S = 180.0                            # no successful intake poll for this long = source_dead
DOWN_AFTER_S = 60.0                              # no collector tick for this long = collector_down (Amendment 8)
WSOL = "So11111111111111111111111111111111111111112"
QUOTE_URL = "https://api.jup.ag/swap/v1/quote"    # the keyed host; the CAPTURE key only
STATES = ("known", "route_absent", "rate_limited", "source_paused", "provider_error", "source_dead", "late", "censored")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, class TEXT NOT NULL, network_id TEXT NOT NULL,
    mint TEXT NOT NULL, first_seen_at REAL NOT NULL, intake_day TEXT NOT NULL, intake_row_id INTEGER NOT NULL,
    filter TEXT NOT NULL, host_received_at REAL, clock_offset_s REAL, price_source TEXT, version TEXT NOT NULL,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, offset_s REAL NOT NULL,
    side TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, price_source TEXT, due_at REAL NOT NULL, taken_at REAL,
    taken_host_at REAL, response_at REAL, amount_in TEXT, amount_out TEXT, qty_atomic TEXT, spend_lamports TEXT,
    proceeds_lamports TEXT, route_id TEXT, pool TEXT, liquidity_reported TEXT, sol_usd REAL, sol_usd_at REAL,
    non_executable_diagnostic_price_usd TEXT, raw TEXT, UNIQUE(event_id, offset_s));
CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, offset_s REAL NOT NULL,
    side TEXT NOT NULL, label TEXT NOT NULL, taken_at REAL NOT NULL, amount_in TEXT, amount_out TEXT, reason TEXT,
    UNIQUE(event_id, offset_s));
CREATE TABLE IF NOT EXISTS pending(event_id TEXT NOT NULL, offset_s REAL NOT NULL, due_at REAL NOT NULL,
    PRIMARY KEY(event_id, offset_s));
CREATE INDEX IF NOT EXISTS quotes_taken ON quotes(taken_at);
CREATE INDEX IF NOT EXISTS pending_due ON pending(due_at);
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY, source TEXT NOT NULL, from_at REAL NOT NULL, to_at REAL NOT NULL,
    reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeat(at REAL PRIMARY KEY, intake_alive INTEGER, quotes_today INTEGER, note TEXT);
"""
SEEN = """
CREATE TABLE IF NOT EXISTS seen(class TEXT NOT NULL, mint TEXT NOT NULL, first_at REAL, left_censored INTEGER NOT NULL,
    PRIMARY KEY(class, mint));
CREATE TABLE IF NOT EXISTS cursors(intake_day TEXT PRIMARY KEY, last_row_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FAMILY_VERSION = 'wallet-family-probe-v1'
FAMILY_USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
FAMILY_CLIP_USDC = 25_000_000
FAMILY_HORIZONS = (5.,30.,300.)
FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS family_pairs(pair_id TEXT PRIMARY KEY,pool TEXT,source_signature TEXT,mint TEXT,
 source_receipt_host REAL,emitted_host REAL,baseline_due REAL,state TEXT,reason TEXT,version TEXT,candidate_json TEXT,
 UNIQUE(pool,source_signature));
CREATE TABLE IF NOT EXISTS family_pending(pair_id TEXT,arm TEXT,offset_s REAL,due_host REAL,PRIMARY KEY(pair_id,arm,offset_s));
CREATE INDEX IF NOT EXISTS family_pending_due ON family_pending(due_host);
CREATE TABLE IF NOT EXISTS family_quotes(pair_id TEXT,arm TEXT,offset_s REAL,state TEXT,reason TEXT,due_host REAL,
 requested_host REAL,response_host REAL,requested_utc REAL,response_utc REAL,clock_offset_s REAL,clock_sampled_at REAL,
 input_mint TEXT,output_mint TEXT,input_atomic TEXT,expected_output_atomic TEXT,minimum_output_atomic TEXT,
 acquired_qty_atomic TEXT,route_id TEXT,context_slot INTEGER,raw TEXT,accounted INTEGER NOT NULL DEFAULT 0,net_pct REAL,
 PRIMARY KEY(pair_id,arm,offset_s));
CREATE INDEX IF NOT EXISTS family_quotes_requested ON family_quotes(requested_utc);
"""


def utc_day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


class Bucket:
    """A token bucket; rate can be cut (after a 429) for a while."""

    def __init__(self, rate, burst=1.0):
        self.rate, self.burst, self.tokens, self.at, self.cut_until = rate, burst, burst, None, 0.0

    def take(self, now):
        rate = self.rate / 2 if now < self.cut_until else self.rate
        self.tokens = self.burst if self.at is None else min(self.burst, self.tokens + (now - self.at) * rate)
        self.at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def cut(self, now, for_s=600.0):
        self.cut_until = now + for_s


def resource_epoch(conn,host):
    """Tag new writes and their origin cohorts; never relabel old observations."""
    policy=json.dumps(dict(version='research-capacity-v2',daily_quote_cap=None if math.isinf(DAILY_CAP) else DAILY_CAP,jupiter_rps=JUP_RATE,global_rps=GLOBAL_RATE),sort_keys=True,allow_nan=False)
    epoch=hashlib.sha256(policy.encode()).hexdigest()
    conn.executescript('CREATE TABLE IF NOT EXISTS resource_epochs(epoch_id TEXT PRIMARY KEY,policy_json TEXT NOT NULL,first_seen_host REAL); CREATE TABLE IF NOT EXISTS resource_record_epochs(table_name TEXT,record_key TEXT,origin_epoch TEXT,first_write_epoch TEXT,last_write_epoch TEXT,PRIMARY KEY(table_name,record_key));')
    conn.execute('INSERT OR IGNORE INTO resource_epochs VALUES(?,?,?)',(epoch,policy,host if _finite(host) else None))
    tables={'events':(['event_id'],None),'quotes':(['event_id','offset_s'],('events',['event_id'])),'diagnostics':(['event_id','offset_s'],('events',['event_id'])),
            'family_pairs':(['pair_id'],None),'family_quotes':(['pair_id','arm','offset_s'],('family_pairs',['pair_id'])),
            'campaign_studies':(['study_id'],None),'campaign_quotes':(['study_id','arm','offset_s'],('campaign_studies',['study_id'])),'gaps':(['id'],None)}
    for table,(keys,parent) in tables.items():
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():continue
        key='json_array('+','.join('NEW.'+k for k in keys)+')'
        conn.execute('DROP TRIGGER IF EXISTS resource_epoch_'+table)
        for operation in ('INSERT','UPDATE'):
            if parent is not None:
                origin="COALESCE((SELECT origin_epoch FROM resource_record_epochs WHERE table_name='"+parent[0]+"' AND record_key=json_array("+','.join('NEW.'+k for k in parent[1])+")),'legacy_unattributed')"
            elif operation=='INSERT':origin="'"+epoch+"'"
            else:origin="COALESCE((SELECT origin_epoch FROM resource_record_epochs WHERE table_name='"+table+"' AND record_key="+key+"),'legacy_unattributed')"
            trigger='resource_epoch_'+operation.lower()+'_'+table
            conn.execute('DROP TRIGGER IF EXISTS '+trigger)
            conn.execute("CREATE TRIGGER "+trigger+" AFTER "+operation+" ON "+table+" BEGIN INSERT INTO resource_record_epochs VALUES('"+table+"',"+key+","+origin+",'"+epoch+"','"+epoch+"') ON CONFLICT(table_name,record_key) DO UPDATE SET last_write_epoch=excluded.last_write_epoch; END")
    conn.commit()
    return epoch


class Collector:
    def __init__(self, *, intake_dir, out_dir, desk_dir, clock_fn, jupiter_fn, ps_buy, ps_sell, paper_throttled_fn,
                 sol_usd_fn=lambda: (None, None), now_fn=time.time, raw_dir=None, family_probe=False, family_db=None,
                 campaign_probe=False, campaign_db=None, buyer_probe=False, buyer_source=None, buyer_activation=None):
        self.intake_dir, self.out_dir, self.desk_dir = Path(intake_dir), Path(out_dir), Path(desk_dir)
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.clock_fn, self.jupiter_fn, self.ps_buy, self.ps_sell = clock_fn, jupiter_fn, ps_buy, ps_sell
        self.paper_throttled_fn, self.sol_usd_fn, self.now_fn = paper_throttled_fn, sol_usd_fn, now_fn
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.seen = sqlite3.connect(str(self.out_dir / "seen.db"))
        self.seen.row_factory = sqlite3.Row
        self.seen.executescript(SEEN)
        self.family_probe,self.family_db=family_probe,Path(family_db) if family_db else None
        self.campaign_probe=campaign_probe
        if campaign_probe and campaign_db is None:raise ValueError('campaign candidate database required')
        if family_probe:
            if self.family_db is None:raise ValueError('family candidate database required')
            self.seen.executescript('CREATE TABLE IF NOT EXISTS family_seen(pair_id TEXT PRIMARY KEY,day TEXT NOT NULL); CREATE TABLE IF NOT EXISTS family_waiting(source_rowid INTEGER PRIMARY KEY,checked_at REAL);')
        self.days = {}
        self.jup, self.glob = Bucket(JUP_RATE), Bucket(GLOBAL_RATE)
        self.dead_since = None
        self.paused_since = None                    # the Jupiter pause while the paper arm is throttled (Am6)
        self.down = []                              # collector downtime intervals (corrected UTC), Amendment 8
        self._today = 0
        self.buyer=None
        if buyer_probe:
            if buyer_source is None:raise ValueError('buyer receipt source required')
            from sniper.office.wallet_buyer_quotes import BuyerProbe
            self.buyer=BuyerProbe(self,buyer_source,buyer_activation)
        self.campaign=None
        if campaign_probe:
            from sniper.office.wallet_campaign_quotes import CampaignProbe
            self.campaign=CampaignProbe(self,campaign_db)

    # ---- storage
    def day(self, day):
        if day not in self.days:
            c = sqlite3.connect(str(self.out_dir / ("%s.db" % day)))
            c.row_factory = sqlite3.Row
            c.executescript(SCHEMA)
            if self.family_probe:c.executescript(FAMILY_SCHEMA)
            if self.campaign_probe:self.campaign.init_day(c)
            self.resource_epoch=resource_epoch(c,self.now_fn())
            self.days[day] = c
        return self.days[day]

    def count(self, conn, name, k=1):
        conn.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET n=n+?", (name, k, k))

    def _intake_days(self):
        return sorted(p.stem for p in self.intake_dir.glob("????-??-??.db") if p.stem >= HISTORY_FROM)

    def _ro(self, path):
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)

    # ---- events
    def resume(self):
        """Amendment 8, every process start: reopen EVERY day store that still has pending rows, load the recorded
        collector downtimes, and record a new one when the last persisted tick is older than DOWN_AFTER_S."""
        for path in sorted(self.out_dir.glob("????-??-??.db")):
            conn = self.day(path.stem)
            if self.family_probe:self._family_finalize(conn)
            self.down += [tuple(r) for r in conn.execute("SELECT from_at, to_at FROM gaps WHERE source='collector'")]
            if not conn.execute("SELECT 1 FROM pending LIMIT 1").fetchone() and not (self.family_probe and conn.execute("SELECT 1 FROM family_pending LIMIT 1").fetchone()) and not (self.campaign_probe and self.campaign.has_pending(conn)):
                self._close(path.stem)              # finished day: no handle kept
        row = self.seen.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone()
        clock = self.clock_fn()
        if row is not None and clock is not None:
            last, utc = float(row[0]), self.now_fn() - clock[0]
            if utc - last > DOWN_AFTER_S:
                self._down(last, utc)
                self.seen.execute("INSERT OR REPLACE INTO meta VALUES('last_tick', ?)", (repr(utc),))
                self.seen.commit()                   # recorded once: the first tick does not record it again

    def _close(self, day):
        conn = self.days.pop(day, None)
        if conn is not None:
            conn.close()

    def _down(self, a, b):
        conn = self.day(utc_day(a))
        conn.execute("INSERT INTO gaps(source, from_at, to_at, reason) VALUES('collector', ?, ?, 'collector_down')", (a, b))
        conn.commit()
        self.down.append((a, b))

    def start(self):
        """Seed the seen set from all intake history and the raw dexscreener store (Amendment 2: 2026-10-22 onward, host
        time, used only to decide seen-before), left-censored, once."""
        if self.seen.execute("SELECT 1 FROM meta WHERE key='started_at'").fetchone():
            return
        n = 0
        for raw in sorted(self.raw_dir.glob("????-??-??.db")) if self.raw_dir else ():
            if raw.stem < HISTORY_FROM:
                continue
            src = self._ro(raw)
            try:
                rows = src.execute("SELECT endpoint, token_address FROM items WHERE chain_id='solana'").fetchall()
            finally:
                src.close()
            for endpoint, mint in rows:
                cls = RAW_ENDPOINTS.get(endpoint)
                if cls and mint and self.seen.execute("INSERT OR IGNORE INTO seen VALUES(?,?,NULL,1)", (cls, mint)).rowcount:
                    n += 1
        for d in self._intake_days():
            src = self._ro(self.intake_dir / ("%s.db" % d))
            try:
                rows = src.execute("SELECT id, filter, mint, observed_at FROM intake_rows WHERE network_id='solana'").fetchall()
            finally:
                src.close()
            for rid, filt, mint, at in rows:
                cls = CLASSES.get(filt)
                if cls and self.seen.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,1)", (cls, mint, at)).rowcount:
                    n += 1
            last = max([r[0] for r in rows], default=0)
            self.seen.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (d, last))
        self.seen.execute("INSERT INTO meta VALUES('started_at', ?)", (repr(self.now_fn()),))
        self.seen.execute("INSERT INTO meta VALUES('left_censored', ?)", (str(n),))
        self.seen.commit()

    def ingest(self):
        """New intake rows since each day's cursor; a first appearance per class is an event."""
        new = 0
        for d in self._intake_days():
            last = (self.seen.execute("SELECT last_row_id FROM cursors WHERE intake_day=?", (d,)).fetchone() or [0])[0]
            src = self._ro(self.intake_dir / ("%s.db" % d))
            try:
                rows = src.execute("SELECT id, filter, mint, observed_at, host_received_at, clock_offset_s FROM intake_rows "
                                   "WHERE id>? AND network_id='solana' ORDER BY id LIMIT ?", (last, ROW_LIMIT)).fetchall()
            finally:
                src.close()
            for rid, filt, mint, at, host_at, off in rows:
                cls = CLASSES.get(filt)
                if not cls or not self.seen.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,0)", (cls, mint, at)).rowcount:
                    continue
                if not _finite(at):
                    self.count(self.day(utc_day(host_at if _finite(host_at) else self.now_fn())), "unusable_clock")
                    continue
                out = self.day(utc_day(at))
                eid = "%s:%s:%s" % (cls, d, rid)
                out.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                            (eid, cls, "solana", mint, at, d, rid, filt, host_at, off, VERSION, self.now_fn()))
                out.execute("INSERT OR IGNORE INTO pending VALUES(?,?,?)", (eid, 0.0, at + ENTRY_AT))
                out.commit()
                new += 1
            if rows:
                self.seen.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (d, rows[-1][0]))
        self.seen.commit()
        return new

    # ---- liveness
    def intake_alive(self, now_utc):
        """A successful dexscreener poll within SOURCE_DEAD_S (corrected clock), else a gap interval is written."""
        d = utc_day(now_utc)
        path = self.intake_dir / ("%s.db" % d)
        ok = None
        if path.exists():
            src = self._ro(path)
            try:
                ok = src.execute("SELECT MAX(received_at) FROM polls WHERE status='ok'").fetchone()[0]
            except sqlite3.Error:
                ok = None
            finally:
                src.close()
        clock = self.clock_fn()
        if ok is not None and clock is not None:
            ok = ok - clock[0]                       # polls.received_at is host time
        alive = ok is not None and 0 <= now_utc - ok <= SOURCE_DEAD_S
        if not alive and self.dead_since is None:
            self.dead_since = now_utc
        if alive and self.dead_since is not None:
            conn = self.day(utc_day(self.dead_since))
            conn.execute("INSERT INTO gaps(source, from_at, to_at, reason) VALUES('intake', ?, ?, 'source_dead')",
                         (self.dead_since, now_utc))
            conn.commit()
            self.dead_since = None
        return alive

    # ---- quotes
    def _record(self, conn, eid, offset, side, state, due, **f):
        cols = ["event_id", "offset_s", "side", "state", "due_at"] + list(f)
        conn.execute("INSERT OR IGNORE INTO quotes(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))),
                     [eid, offset, side, state, due] + list(f.values()))
        conn.execute("DELETE FROM pending WHERE event_id=? AND offset_s=?", (eid, offset))

    def _window(self, offset):
        if offset == 0.0:
            return ENTRY_WINDOW
        return offset * (1 - HORIZON_TOL), offset * (1 + HORIZON_TOL)

    def tick(self):
        now = self.now_fn()
        clock = self.clock_fn()                      # (offset_s, sampled_at) or None
        if clock is None or (self.family_probe or self.campaign_probe) and (not isinstance(clock,(tuple,list)) or len(clock)!=2 or not all(_finite(v) for v in clock) or not _finite(now) or clock[1]>now):
            return {"clock": "unknown"}               # no corrected time: no quote, retried next tick
        utc = now - clock[0]
        last = self.seen.execute("SELECT value FROM meta WHERE key='last_tick'").fetchone()
        if last is not None and utc - float(last[0]) > DOWN_AFTER_S:
            self._down(float(last[0]), utc)          # a stalled loop inside one process is downtime too
        self.seen.execute("INSERT OR REPLACE INTO meta VALUES('last_tick', ?)", (repr(utc),))
        self.seen.commit()
        alive = self.intake_alive(utc)
        if alive:
            self.ingest()
        done = 0
        try:
            self._today = self.quotes_today(utc)      # all persisted day files, including closed stores
        except (sqlite3.Error,OSError):
            return {"clock":"ok","intake_alive":alive,"quotes":0,"budget":"unknown"}
        # every event day whose last horizon can still be due: today and the two UTC days before it (24 h + 10 % reaches
        # back at most 26.4 h, so after midnight yesterday AND, early in the day, the day before), opened on a restart too
        for d in sorted(set(self.days) | {utc_day(utc - k * 86400) for k in (0, 1, 2)}):
            if not (self.out_dir / ("%s.db" % d)).exists() and d not in self.days:
                continue
            conn = self.day(d)
            due = conn.execute("SELECT p.event_id, p.offset_s, p.due_at, e.mint, e.first_seen_at, e.price_source "
                               "FROM pending p JOIN events e USING(event_id) WHERE p.due_at <= ? ORDER BY p.due_at "
                               "LIMIT ?", (utc, DUE_PAGE)).fetchall()
            for row in due:
                done += self._one(conn, row, utc, clock)
            conn.commit()
            if d < utc_day(utc - 2 * 86400) and not conn.execute("SELECT 1 FROM pending LIMIT 1").fetchone() and not (self.family_probe and conn.execute("SELECT 1 FROM family_pending LIMIT 1").fetchone()) and not (self.campaign_probe and self.campaign.has_pending(conn)):
                self._close(d)                       # an old finished day keeps no handle
        result={"clock": "ok", "intake_alive": alive, "quotes": done}
        if self.family_probe:
            why=self._family_ingest(self.now_fn(),clock)
            result['family']=dict(source_reason=why,quotes=self._family_due(clock),pending_emissions=self.seen.execute('SELECT count(*) FROM family_waiting').fetchone()[0])
        if self.campaign_probe:
            why=self.campaign.ingest(self.now_fn(),clock)
            result['campaign']=dict(source_reason=why,quotes=self.campaign.due(clock,DAILY_CAP),execution_state='quote_only')
        if self.buyer is not None:
            result['buyer']=self.buyer.tick(clock,DAILY_CAP)
        return result

    # Family jobs share this owner's admission and Jupiter seam; no separate client.
    def _family_ingest(self, host, clock):
        try:
            src=self._ro(self.family_db);src.row_factory=sqlite3.Row
            try:
                last=int((self.seen.execute("SELECT value FROM meta WHERE key='family_cursor'").fetchone() or ['0'])[0])
                fresh=src.execute('SELECT rowid AS source_rowid,* FROM wallet_family_candidates WHERE rowid>? ORDER BY rowid LIMIT ?',(last,DUE_PAGE)).fetchall()
                waiting=[r[0] for r in self.seen.execute('SELECT source_rowid FROM family_waiting ORDER BY checked_at,source_rowid LIMIT ?',(DUE_PAGE,))]
                rows={r['source_rowid']:r for r in fresh}
                for rid in waiting:
                    row=src.execute('SELECT rowid AS source_rowid,* FROM wallet_family_candidates WHERE rowid=?',(rid,)).fetchone()
                    if row:rows[rid]=row
            finally:src.close()
        except (sqlite3.Error,OSError,ValueError):return 'candidate_source_unknown'
        for rid,row in rows.items():
            if row['early'] is None:
                self.seen.execute('INSERT INTO family_waiting VALUES(?,?) ON CONFLICT(source_rowid) DO UPDATE SET checked_at=excluded.checked_at',(rid,host))
            else:
                self._family_pair(dict(row),host,clock)
                self.seen.execute('DELETE FROM family_waiting WHERE source_rowid=?',(rid,))
            self.seen.commit()
        if fresh:
            self.seen.execute("INSERT OR REPLACE INTO meta VALUES('family_cursor',?)",(str(fresh[-1]['source_rowid']),));self.seen.commit()
        return None

    def _family_pair(self, row, host, clock):
        import hashlib
        pid=hashlib.sha256(json.dumps([row['pool'],row['source_signature']]).encode()).hexdigest()
        source,emitted,due,known=(row[k] for k in ('source_receipt','emitted_at','due','evidence_known_at'))
        reason=None
        if not all(_finite(v) for v in (source,emitted,due)) or due!=source+10 or emitted<source:reason='candidate_clock_unknown'
        elif emitted>host:reason='future_candidate'
        elif not _finite(known):reason='family_evidence_unknown'
        elif known>emitted:reason='future_family_evidence'
        elif row['policy']!='research_only':reason='candidate_policy_unknown'
        elif row['early']!=1 or emitted>=due:reason='emission_not_early'
        mint=None;tape=None
        if reason is None:
            try:
                tape=self._ro(self.desk_dir/'pumpswap/tape.db')
                found=tape.execute('SELECT DISTINCT mint FROM ps_watch_events WHERE pool=? AND signature=? LIMIT 2',(row['pool'],row['source_signature'])).fetchall()
                if len(found)==1:mint=found[0][0]
            except (sqlite3.Error,OSError):pass
            finally:
                if tape is not None:tape.close()
            import re
            if not isinstance(mint,str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',mint) or mint in (FAMILY_USDC,WSOL):reason='candidate_mint_unknown'
        day=utc_day((source if _finite(source) else host)-clock[0])
        self.seen.execute('INSERT OR IGNORE INTO family_seen VALUES(?,?)',(pid,day));self.seen.commit()
        day=self.seen.execute('SELECT day FROM family_seen WHERE pair_id=?',(pid,)).fetchone()[0]
        conn=self.day(day)
        if conn.execute('SELECT 1 FROM family_pairs WHERE pair_id=?',(pid,)).fetchone():return
        with conn:
            conn.execute('INSERT INTO family_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,row['pool'],row['source_signature'],mint,source,emitted,due,'censored' if reason else 'pending',reason,FAMILY_VERSION,json.dumps(row,sort_keys=True)))
            if not reason:
                for arm,target in (('early',emitted),('baseline',due)):
                    conn.execute('INSERT OR IGNORE INTO family_pending VALUES(?,?,0,?)',(pid,arm,target))

    def _family_record(self,conn,pid,arm,offset,state,reason,due,**fields):
        values=dict(pair_id=pid,arm=arm,offset_s=offset,state=state,reason=reason,due_host=due,**fields)
        cols=list(values)
        conn.execute('INSERT OR REPLACE INTO family_quotes(%s) VALUES(%s)'%(','.join(cols),','.join('?' for _ in cols)),list(values.values()))
        conn.execute('DELETE FROM family_pending WHERE pair_id=? AND arm=? AND offset_s=?',(pid,arm,offset))

    def _family_no_entry(self,conn,pid,arm,due):
        for h in FAMILY_HORIZONS:
            conn.execute("INSERT OR IGNORE INTO family_quotes(pair_id,arm,offset_s,state,reason,due_host) VALUES(?,?,?,'censored','no_entry',?)",(pid,arm,h,due+h))

    def _family_finalize(self,conn):
        expected={(arm,h) for arm in ('early','baseline') for h in (0,*FAMILY_HORIZONS)}
        pairs=conn.execute("SELECT pair_id FROM family_pairs e WHERE state='pending' AND reason IS NULL AND NOT EXISTS(SELECT 1 FROM family_pending p WHERE p.pair_id=e.pair_id)").fetchall()
        with conn:
            for pair in pairs:
                rows=conn.execute('SELECT arm,offset_s,state FROM family_quotes WHERE pair_id=?',(pair['pair_id'],)).fetchall()
                states={(r['arm'],r['offset_s']):r['state'] for r in rows}
                state,reason='partial','quote_coverage_partial'
                if set(states)==expected:
                    if all(s=='known' for s in states.values()):state,reason='quoted','all_horizons_observed'
                    elif not any(states[(arm,0)]=='known' for arm in ('early','baseline')):state,reason='censored','entry_unavailable'
                conn.execute('UPDATE family_pairs SET state=?,reason=? WHERE pair_id=?',(state,reason,pair['pair_id']))

    def _family_due(self,clock):
        done=0
        for day in sorted(self.days):
            conn=self.day(day)
            for r in conn.execute("SELECT pair_id,arm,due_host FROM family_quotes WHERE offset_s=0 AND state!='known' LIMIT ?",(DUE_PAGE,)).fetchall():
                self._family_no_entry(conn,r['pair_id'],r['arm'],r['due_host'])
            jobs=conn.execute('SELECT p.*,e.mint,e.baseline_due FROM family_pending p JOIN family_pairs e USING(pair_id) WHERE p.due_host<=? ORDER BY p.due_host LIMIT ?',(self.now_fn(),DUE_PAGE)).fetchall()
            for job in jobs:done+=self._family_one(conn,job,clock)
            conn.commit()
            self._family_finalize(conn)
        return done

    def _family_one(self,conn,row,clock):
        pid,arm,h,due=row['pair_id'],row['arm'],row['offset_s'],row['due_host']
        requested=self.now_fn();utc=requested-clock[0]
        if requested<due:return 0  # a clock rollback cannot create an early baseline or exit
        deadline=row['baseline_due'] if h==0 and arm=='early' else due+(5 if h==0 else h*.1)
        if requested>deadline or h==0 and arm=='early' and requested>=deadline:
            self._family_record(conn,pid,arm,h,'censored','window_passed',due,requested_host=requested,requested_utc=utc)
            if h==0:self._family_no_entry(conn,pid,arm,due)
            return 0
        if self._today>=DAILY_CAP:
            self._family_record(conn,pid,arm,h,'censored','cap_reached',due,requested_host=requested,requested_utc=utc)
            if h==0:self._family_no_entry(conn,pid,arm,due)
            return 0
        if not self.glob.take(utc):return 0
        if h==0:inp,out,amount=FAMILY_USDC,row['mint'],FAMILY_CLIP_USDC
        else:
            buy=conn.execute("SELECT acquired_qty_atomic FROM family_quotes WHERE pair_id=? AND arm=? AND offset_s=0 AND state='known'",(pid,arm)).fetchone()
            if not buy:
                self._family_record(conn,pid,arm,h,'censored','no_entry',due);return 0
            inp,out,amount=row['mint'],FAMILY_USDC,int(buy[0])
        self._today+=1
        # Persist admission before an external read. A crash is censored, never an uncounted retry.
        common=dict(requested_host=requested,requested_utc=utc,clock_offset_s=clock[0],clock_sampled_at=clock[1],input_mint=inp,output_mint=out,input_atomic=str(amount),accounted=1)
        self._family_record(conn,pid,arm,h,'censored','quote_interrupted',due,**common);conn.commit()
        try:quote,why,extra=self._jupiter(inp,out,amount,utc)
        except Exception:quote,why,extra=None,'provider_error',{}
        response=self.now_fn();reason=why
        state='known';extra=dict(common,response_host=response,response_utc=response-clock[0],route_id=extra.get('route_id'))
        if not _finite(response) or response<requested:reason='clock_regressed'
        elif response>deadline or h==0 and arm=='early' and response>=deadline:reason='response_late'
        if quote is None:reason=reason or 'provider_error'
        elif reason is None:
            if quote.get('inputMint')!=inp or quote.get('outputMint')!=out or str(quote.get('inAmount'))!=str(amount):reason='quote_identity_mismatch'
            else:
                expected,minimum=quote.get('outAmount'),quote.get('otherAmountThreshold')
                valid=lambda v:isinstance(v,str) and v.isascii() and v.isdigit() and len(v)<=78 and int(v)>0
                if not valid(expected) or not valid(minimum) or int(minimum)>int(expected):reason='quote_amount_unknown'
                elif not quote.get('routePlan'):reason='route_unknown'
                else:
                    extra.update(expected_output_atomic=expected,minimum_output_atomic=minimum,context_slot=quote.get('contextSlot') if type(quote.get('contextSlot')) is int else None,raw=json.dumps(quote,sort_keys=True,default=str)[:4000])
                    if h==0:extra['acquired_qty_atomic']=expected
        if reason:state='censored';extra.pop('acquired_qty_atomic',None)
        with conn:
            self._family_record(conn,pid,arm,h,state,reason,due,**extra)
            if h==0:
                if state=='known':
                    for horizon in FAMILY_HORIZONS:conn.execute('INSERT OR IGNORE INTO family_pending VALUES(?,?,?,?)',(pid,arm,horizon,response+horizon))
                else:self._family_no_entry(conn,pid,arm,due)
        return 1

    def _one(self, conn, row, utc, clock):
        eid, offset, due, mint, t0 = row["event_id"], row["offset_s"], row["due_at"], row["mint"], row["first_seen_at"]
        lo, hi = self._window(offset)
        if utc - t0 > hi:                            # the window has passed: never a silent later price
            down = any(a < t0 + hi and b > t0 + lo for a, b in self.down)
            state, reason = ("censored", "collector_down") if down else ("late", "window_passed")
            self._record(conn, eid, offset, "buy" if offset == 0 else "sell", state, due, reason=reason, taken_at=utc)
            if offset == 0:
                self._censor_rest(conn, eid, due, "collector_down" if down else "entry_late")
            return 0
        if utc - t0 < lo:
            return 0
        if any(a <= due <= b for a, b in self.down):   # the persisted due instant fell in an outage (Am8/9; entry = t0+30)
            self._record(conn, eid, offset, "buy" if offset == 0 else "sell", "censored", due, reason="collector_down",
                         taken_at=utc)
            if offset == 0:
                self._censor_rest(conn, eid, due, "collector_down")
            return 0
        if self._today >= DAILY_CAP:
            self._record(conn, eid, offset, "buy" if offset == 0 else "sell", "censored", due, reason="cap_reached",
                         taken_at=utc)
            self.count(conn, "cap_reached")
            if offset == 0:
                self._censor_rest(conn, eid, due, "cap_reached")
            return 0
        if not self.glob.take(utc):
            return 0                                 # paced: try again next tick (may end late)
        self._today += 1
        if offset == 0:
            return self._entry(conn, eid, mint, due, utc, clock)
        return self._exit(conn, eid, mint, offset, due, utc, clock)

    def quotes_today(self, utc):
        """Quote attempts taken today (UTC), across every day file (a quote lands in its event's day file)."""
        day0 = utc - utc % 86400
        total=0
        # A completed event-day file may still contain today's admissions after a restart.
        paths={p.stem:p for p in self.out_dir.glob('????-??-??.db')}
        for day in sorted(set(paths)|set(self.days)):
            owned=day in self.days
            c=self.days[day] if owned else self._ro(paths[day])
            try:
                total+=c.execute("SELECT COUNT(*) FROM quotes WHERE taken_at >= ? AND reason IS NOT 'window_passed' AND reason IS NOT 'cap_reached'",(day0,)).fetchone()[0]
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='family_quotes'").fetchone():
                    total+=c.execute('SELECT COALESCE(SUM(accounted),0) FROM family_quotes WHERE requested_utc>=?',(day0,)).fetchone()[0]
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='campaign_quotes'").fetchone():
                    total+=c.execute('SELECT COALESCE(SUM(accounted),0) FROM campaign_quotes WHERE requested_utc>=?',(day0,)).fetchone()[0]
            finally:
                if not owned:c.close()
        if self.buyer is not None:total+=self.buyer.quotes_today(utc)
        return total

    def _censor_rest(self, conn, eid, due, reason):
        for h in HORIZONS:
            self._record(conn, eid, h, "sell", "censored", due, reason=reason)

    def _ps(self, fn, mint, amount, utc, clock):
        """The Office ledger's quote seam (Amendment 4 cond. 6): corrected UTC -> host through ONE clock sample."""
        from sniper.office.ledger import _quote
        return _quote(fn, self.desk_dir, mint, amount, utc, utc, {"offset_s": clock[0], "sampled_at": clock[1]})

    def _late_response(self, conn, eid, offset, due, utc, clock):
        """(late, response time in corrected UTC). A quote whose RESPONSE arrives after its window is late (the request
        time is never backdated into the window); corrected with the same frozen clock offset as the request, and the
        same corrected response time is stored on every outcome row."""
        resp = self.now_fn() - clock[0]
        t0 = conn.execute("SELECT first_seen_at FROM events WHERE event_id=?", (eid,)).fetchone()[0]
        if resp - t0 <= self._window(offset)[1]:
            return False, resp
        self._record(conn, eid, offset, "buy" if offset == 0 else "sell", "late", due, reason="response_late",
                     price_source="jupiter_capture", taken_at=utc, taken_host_at=utc + clock[0],
                     response_at=resp)
        return True, resp

    def _diagnostic(self, conn, eid, offset, side, mint, amount, utc, clock):
        """Amendment 7: a ps_tape quote at offsets <= +15 min, a labelled cross-check of the Jupiter quote against our
        tape's reserve math. Never a return, never matching, never an event state; a tape failure is a typed row here."""
        if offset > TAPE_DIAG_MAX_OFFSET or self.ps_buy is None:
            return
        try:
            quote, why = self._ps(self.ps_buy if side == "buy" else self.ps_sell, mint, amount, utc, clock)
        except Exception as exc:                     # a diagnostic can never break the study's own quote
            quote, why = None, "diagnostic_error:%s" % type(exc).__name__
        out = None if quote is None else (quote.get("tokens_atomic") if side == "buy" else quote.get("lamports"))
        conn.execute("INSERT OR IGNORE INTO diagnostics(event_id, offset_s, side, label, taken_at, amount_in, amount_out, "
                     "reason) VALUES(?,?,?,?,?,?,?,?)", (eid, offset, side, "ps_tape_crosscheck", utc, str(amount),
                                                        None if out is None else str(out), why or ('fee_basis:' + quote['proof']['fee_basis'] if 'fee_basis' in ((quote or {}).get('proof') or {}) else None)))

    def _entry(self, conn, eid, mint, due, utc, clock):
        sol_usd, sol_at = self.sol_usd_fn()
        source = "jupiter_capture"                   # Amendment 7: one fill model for every event
        quote, why, extra = self._jupiter(WSOL, mint, CLIP_LAMPORTS, utc)
        late, resp = self._late_response(conn, eid, 0.0, due, utc, clock)
        if quote is not None and late:
            self._censor_rest(conn, eid, due, "entry_late")
            return 1
        extra = dict(extra, response_at=resp)
        if quote is None:
            self._record(conn, eid, 0.0, "buy", _state(why, source), due, reason=why, price_source=source, taken_at=utc,
                         taken_host_at=utc + clock[0], response_at=resp)
            self._censor_rest(conn, eid, due, "no_entry")
            return 1
        qty = str(quote["outAmount"])
        conn.execute("UPDATE events SET price_source=? WHERE event_id=?", (source, eid))
        self._record(conn, eid, 0.0, "buy", "known", due, price_source=source, taken_at=utc, taken_host_at=utc + clock[0],
                     amount_in=str(CLIP_LAMPORTS), amount_out=qty, qty_atomic=qty, spend_lamports=str(CLIP_LAMPORTS),
                     sol_usd=sol_usd, sol_usd_at=sol_at, raw=json.dumps(quote, sort_keys=True, default=str)[:4000], **extra)
        self._diagnostic(conn, eid, 0.0, "buy", mint, CLIP_LAMPORTS, utc, clock)
        t0 = conn.execute("SELECT first_seen_at FROM events WHERE event_id=?", (eid,)).fetchone()[0]
        for h in HORIZONS:
            conn.execute("INSERT OR IGNORE INTO pending VALUES(?,?,?)", (eid, h, t0 + h))
        return 1

    def _exit(self, conn, eid, mint, offset, due, utc, clock):
        source = "jupiter_capture"
        qty = conn.execute("SELECT qty_atomic FROM quotes WHERE event_id=? AND offset_s=0 AND state='known'",
                           (eid,)).fetchone()
        if qty is None:
            self._record(conn, eid, offset, "sell", "censored", due, reason="no_entry")
            return 0
        qty = int(qty[0])
        quote, why, extra = self._jupiter(mint, WSOL, qty, utc)
        late, resp = self._late_response(conn, eid, offset, due, utc, clock)
        if quote is not None and late:
            return 1
        extra = dict(extra, response_at=resp)
        if quote is None:
            self._record(conn, eid, offset, "sell", _state(why, source), due, reason=why, price_source=source,
                         taken_at=utc, taken_host_at=utc + clock[0], response_at=resp)
            return 1
        proceeds = quote["outAmount"]
        sol_usd, sol_at = self.sol_usd_fn()
        self._record(conn, eid, offset, "sell", "known", due, price_source=source, taken_at=utc,
                     taken_host_at=utc + clock[0], amount_in=str(qty), amount_out=str(proceeds), qty_atomic=str(qty),
                     proceeds_lamports=str(proceeds), sol_usd=sol_usd, sol_usd_at=sol_at,
                     raw=json.dumps(quote, sort_keys=True, default=str)[:4000], **extra)
        self._diagnostic(conn, eid, offset, "sell", mint, qty, utc, clock)
        return 1

    def _jupiter(self, inp, out, amount, utc):
        """Capture-only provider pacing and observed429 slowdown survive owner restarts."""
        if self.paper_throttled_fn():
            if self.paused_since is None:
                self.paused_since = utc
            return None, "paper_arm_throttled", {}       # the paper arm shares the key: the collector backs off first
        if self.paused_since is not None:            # the pause ended: record its interval (Am6 paused time)
            conn = self.day(utc_day(self.paused_since))
            conn.execute("INSERT INTO gaps(source, from_at, to_at, reason) VALUES('jupiter_capture', ?, ?, "
                         "'paper_arm_throttled')", (self.paused_since, utc))
            conn.commit()
            self.paused_since = None
        try:
            if not _finite(utc):return None,'quote_clock_unknown',{}
            saved=self.seen.execute("SELECT value FROM meta WHERE key='capture_jupiter_rate'").fetchone()
            rate_state=json.loads(saved[0]) if saved else dict(last=None,cut_until=0.)
            if not isinstance(rate_state,dict) or not _finite(rate_state.get('cut_until')):return None,'quote_rate_state_unknown',{}
            last=rate_state.get('last');cut=rate_state['cut_until']
            if last is not None:
                if not _finite(last) or utc<last:return None,'quote_rate_clock_regressed',{}
                if utc-last<1./(JUP_RATE/2 if utc<cut else JUP_RATE):return None,'rate_limited',{}
            self.jup.cut_until=max(self.jup.cut_until,cut)
            if not self.jup.take(utc):return None,'rate_limited',{}
            rate_state['last']=utc
            self.seen.execute("INSERT OR REPLACE INTO meta VALUES('capture_jupiter_rate',?)",(json.dumps(rate_state,allow_nan=False),));self.seen.commit()
        except (sqlite3.Error,ValueError,TypeError):
            self.seen.rollback();return None,'quote_rate_store_unknown',{}
        quote, why = self.jupiter_fn(inp, out, amount)
        if why == "rate_limited":
            self.jup.cut(utc)
            rate_state['cut_until']=self.jup.cut_until
            try:
                self.seen.execute("INSERT OR REPLACE INTO meta VALUES('capture_jupiter_rate',?)",(json.dumps(rate_state,allow_nan=False),));self.seen.commit()
            except sqlite3.Error:
                self.seen.rollback();return None,'quote_rate_store_unknown',{}
        if quote is None:
            return None, why, {}
        plan = quote.get("routePlan") or []
        return quote, None, {"route_id": "|".join(str((p.get("swapInfo") or {}).get("ammKey")) for p in plan)[:500],
                             "pool": str((plan[0].get("swapInfo") or {}).get("ammKey")) if plan else None}


def _state(why, source):
    """A failed quote's typed state. ps_tape reasons are the tape's own (no route on the tape at that cut)."""
    if why == "paper_arm_throttled":
        return "source_paused"
    if why == "rate_limited":
        return "rate_limited"
    if why == "route_absent":
        return "route_absent"
    if source == "jupiter_capture":
        return "provider_error"
    if any(k in (why or "") for k in ("capture", "gap", "commit", "block")):
        return "source_dead"                         # our tape could not prove the state at that cut
    return "route_absent"


# ---- production wiring

def jupiter_capture(inp, out, amount, *, key=None, timeout=5.0):
    """Exact-in quote on the CAPTURE key (passed in from --capture-env). (quote, None) or (None, rate_limited |
    route_absent | provider_error)."""
    if not key:
        return None, "provider_error"
    q = urllib.parse.urlencode({"inputMint": inp, "outputMint": out, "amount": str(int(amount)), "slippageBps": "50",
                                "swapMode": "ExactIn"})
    req = urllib.request.Request(QUOTE_URL + "?" + q, headers={"x-api-key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "rate_limited"
        text = e.read().decode("utf-8", "replace")[:500]
        return None, ("route_absent" if any(s in text for s in ("COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE",
                                                                  "NO_ROUTES_FOUND")) else "provider_error")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, "provider_error"
    if not isinstance(body, dict) or not str(body.get("outAmount", "")).isdigit() or str(body.get("inAmount")) != str(int(amount)):
        return None, "provider_error"                 # only an exact-quantity answer counts
    return body, None


CAPTURE_VAR = "MEMECOIN_CAPTURE_JUPITER_API_KEY"
SECRET_MARKS = ("KEY", "TOKEN", "SECRET", "PASS", "PRIVATE", "SEED", "MNEMONIC", "AUTH")


def funded_env_refusal(environ):
    """Alpha / Delta: the collector process never holds the funded key or any other secret. It loads no .env file and
    is launched under env -i with PATH, HOME and SNIPER_STATE_ROOT only; a variable whose NAME looks like a secret
    (the capture key included: it comes from --capture-env, not the environment) refuses start, whatever its value."""
    present = [n for n in environ if any(m in n.upper() for m in SECRET_MARKS)]
    if present:
        raise SystemExit("secret-like variable(s) present in the environment (%d): refusing to start" % len(present))
    return True


def read_capture_env(path) -> str:
    """The capture key from its own file: owner-only mode, exactly one assignment, and only MEMECOIN_CAPTURE_JUPITER_
    API_KEY. Anything else refuses start. The value is returned, never logged."""
    import stat
    path = Path(path)
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SystemExit("capture env file is readable by group/others: refusing to start")
        lines = [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    except OSError:
        raise SystemExit("capture env file missing or unreadable: refusing to start") from None
    pairs = [l.split("=", 1) for l in lines]
    if len(pairs) != 1 or len(pairs[0]) != 2 or pairs[0][0].strip() != CAPTURE_VAR:
        raise SystemExit("capture env file must hold exactly one %s assignment: refusing to start" % CAPTURE_VAR)
    key = pairs[0][1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("capture key empty: refusing to start")
    return key


def key_identity_guard(out_dir, capture):
    """Amendment 4 cond. 1, Alpha's design: the collector never sees the funded key. It reads a 32-byte host salt
    (out_dir/host_salt, owner-only) and the funded key's reference fingerprint (out_dir/funded_key.fp = hex
    sha256(host_salt || funded key), written once by Alpha), computes the capture key's fingerprint the same way, and
    refuses to start when the capture key is missing, either file is missing, unreadable, malformed or group/world
    readable, or the two fingerprints are equal. Nothing secret is logged. Raises SystemExit; returns the reference
    file's age in days (printed at start: a rotated funded key needs a regenerated funded_key.fp, Gamma)."""
    import hashlib
    import hmac
    import stat
    capture = (capture or "").strip()
    if not capture:
        raise SystemExit("capture key missing: refusing to start")
    salt_path, fp_path = Path(out_dir) / "host_salt", Path(out_dir) / "funded_key.fp"
    try:
        if stat.S_IMODE(salt_path.stat().st_mode) & 0o077:
            raise SystemExit("host_salt is readable by group/others: refusing to start")
        salt = salt_path.read_bytes()
        ref = fp_path.read_text().strip().lower()
        age_days = (time.time() - fp_path.stat().st_mtime) / 86400
    except OSError:
        raise SystemExit("funded key reference missing or unreadable: refusing to start") from None
    if len(salt) != 32 or len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
        raise SystemExit("funded key reference malformed: refusing to start")
    mine = hashlib.sha256(salt + capture.encode()).hexdigest()
    if hmac.compare_digest(mine, ref):
        raise SystemExit("the capture key IS the funded key: refusing to start")
    return age_days


def paper_throttled(main_db, *, now=None):
    """True when the paper arm's rate_limited over the last 10 minutes exceeds twice its trailing-24 h rate (and at
    least 3), read-only from memecoin_jupiter_load. Unreadable = True (back off first)."""
    now = now or time.time()
    try:
        c = sqlite3.connect(Path(main_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            def rl(since):
                return c.execute("SELECT COALESCE(SUM(rate_limited), 0) FROM memecoin_jupiter_load WHERE role='paper' AND "
                                 "ts_minute >= ?", (datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M"),)
                                 ).fetchone()[0]
            recent, day = rl(now - 600), rl(now - 86400)
        finally:
            c.close()
    except sqlite3.Error:
        return True
    return recent >= 3 and recent > 2 * day * 600 / 86400


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--tick-s", type=float, default=5.0)
    p.add_argument("--capture-env", default=None, help="file holding only the capture key (default "
                   "<state>/data/research/dex_boost/capture.env, mode 0600)")
    p.add_argument("--family-probe",action="store_true",help="Opt-in separate research family quote namespace; shared caps")
    p.add_argument("--campaign-probe",action="store_true",help="Opt-in launch quote study; existing client and shared caps, no orders")
    p.add_argument('--buyer-study',help='Opt-in frozen activation JSON; research quote study only')
    a = p.parse_args(argv)
    from sniper.util.state_paths import state_path
    funded_env_refusal(os.environ)                   # never loads .env: launched with an allowlisted environment
    key = read_capture_env(a.capture_env or state_path("data", "research", "dex_boost", "capture.env"))
    age = key_identity_guard(state_path("data", "research", "dex_boost"), key)
    print(json.dumps({"key_guard": "ok", "funded_key_fp_age_days": round(age, 2),
                      "note": "regenerate funded_key.fp on every funded Jupiter key rotation"}), flush=True)
    try:
        os.nice(19)
        import subprocess
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False, capture_output=True)
    except OSError:
        pass
    from sniper.services import memecoin_desk_ps_paper as PSP
    from sniper.services.memecoin_desk_intake import clock_sample
    clock_path = Path(state_path("data", "clock_offset.json"))

    def clock():
        off, at = clock_sample(clock_path, now=time.time())
        return None if off is None else (off, at)
    buyer_activation=json.loads(Path(a.buyer_study).read_text()) if a.buyer_study else None
    col = Collector(intake_dir=state_path("data", "desk", "intake", "dexscreener"),
                    out_dir=state_path("data", "research", "dex_boost"), desk_dir=state_path("data", "desk"),
                    clock_fn=clock, jupiter_fn=lambda i, o, n: jupiter_capture(i, o, n, key=key),
                    ps_buy=PSP.buy, ps_sell=PSP.sell,
                    paper_throttled_fn=lambda: paper_throttled(state_path("sniper_tracker.db")),
                    raw_dir=state_path("data", "desk", "dexscreener"),family_probe=a.family_probe,
                    family_db=state_path("data", "office", "wallet_network.db") if a.family_probe else None,
                    campaign_probe=a.campaign_probe,campaign_db=state_path("data", "office", "wallet_network.db") if a.campaign_probe else None,
                    buyer_probe=bool(a.buyer_study),buyer_activation=buyer_activation,
                    buyer_source=state_path("data", "desk", "pumpswap", "tape.db") if a.buyer_study else None)
    col.start()
    col.resume()
    while True:
        out = col.tick()
        print(json.dumps(out), flush=True)
        if not a.loop:
            return 0
        time.sleep(a.tick_s)


if __name__ == "__main__":
    sys.exit(main())
