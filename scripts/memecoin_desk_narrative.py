#!/usr/bin/env python3
"""Collect-only narrative live-v2: observed Solana first sightings from narrative_guess,
plus retained WSOL PumpSwap buy flow. Bounded bootstrap/retention are lower bounds;
missing sources/FX remain unknown. Legacy v1 helpers/files stay intact.
No provider/model calls or trade gates. Output data/desk/narrative/live-v2/<UTC-day>.db.
Existing MEMECOIN_DESK_NARRATIVE_ENABLED opt-in and one-writer flock apply.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_narrative_live as LIVE
from sniper.services import memecoin_desk_theme_rules as R          # noqa: E402
from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

_spec = importlib.util.spec_from_file_location("desk_trending", ROOT / "scripts" / "memecoin_desk_trending.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

PASS_S = 60.0
WINDOWS = (300, 900, 3600)
TOP_TOKENS = 30
TOKEN_RE = re.compile(r"[^\W\d_]{3,}")
STOPWORDS = frozenset("""the and for you your are was this that with from not but all can our out his her its
coin coins token tokens official real new just get got has have had will one two who what why how when
""".split())

DDL = """
CREATE TABLE IF NOT EXISTS tags (mint TEXT PRIMARY KEY, theme TEXT NOT NULL, matched TEXT, rules_version TEXT NOT NULL,
    launch_received_at REAL NOT NULL, known_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS theme_agg (at REAL NOT NULL, theme TEXT NOT NULL, window_s INTEGER NOT NULL,
    launches INTEGER NOT NULL, band_entries INTEGER NOT NULL, buy_usd REAL, buyers INTEGER, slope REAL,
    coverage TEXT NOT NULL, rules_version TEXT NOT NULL, PRIMARY KEY(at, theme, window_s));
CREATE TABLE IF NOT EXISTS token_hourly (hour_start REAL NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('rule_word','name_token')),
    rank INTEGER NOT NULL, token TEXT NOT NULL, launches INTEGER NOT NULL, creators INTEGER NOT NULL,
    prev_hour_launches INTEGER NOT NULL,
    known_at REAL NOT NULL, rules_version TEXT NOT NULL, PRIMARY KEY(hour_start, kind, rank));
CREATE TABLE IF NOT EXISTS cursors (day TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL);
"""


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def name_tokens(symbol, name):
    """Distinct raw tokens of one launch (counted once per launch)."""
    return sorted({t for t in TOKEN_RE.findall(R.normalize(symbol, name)) if t not in STOPWORDS})


class Store:
    def __init__(self, out_dir, *, ddl=DDL):
        self.ddl=ddl
        self.out_dir, self.day, self.conn = Path(out_dir), None, None

    def db(self, at):
        day = utc_day(at)
        if day != self.day:
            if self.conn is not None:
                self.conn.commit()
                self.conn.close()
            self.conn = sqlite3.connect(self.out_dir / ("%s.db" % day), timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(self.ddl)
            self.day = day
        return self.conn

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()


def ingest_tags(store, desk, now):
    """New launches since each day file's cursor -> tags (known_at = now). Returns the count."""
    n = 0
    for day in sorted({utc_day(now - 86400), utc_day(now)}):
        src = T.ro(Path(desk) / "launches" / ("%s.db" % day))
        if src is None:
            continue
        db = store.db(now)
        cur = db.execute("SELECT last_rowid FROM cursors WHERE day=?", (day,)).fetchone()
        last = cur[0] if cur else 0
        try:
            rows = src.execute("SELECT rowid, mint, symbol, name, received_at FROM launches WHERE rowid > ? "
                               "AND received_at <= ? ORDER BY rowid", (last, now)).fetchall()
        except sqlite3.Error:
            rows = src.execute("SELECT rowid, mint, NULL, NULL, received_at FROM launches WHERE rowid > ? "
                               "AND received_at <= ? ORDER BY rowid", (last, now)).fetchall()   # pre-symbol files
        finally:
            src.close()
        for rowid, mint, symbol, name, rec in rows:
            theme, matched = R.tag(symbol, name) if (symbol or name) else ("unknown", None)
            db.execute("INSERT OR IGNORE INTO tags VALUES (?,?,?,?,?,?)",
                       (mint, theme, matched, R.RULES_VERSION, rec, now))
            last = rowid
            n += 1
        db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(day) DO UPDATE SET last_rowid=excluded.last_rowid",
                   (day, last))
    store.db(now).commit()
    return n


def tags_since(store_dir, since, until):
    """{mint: (theme, launch_received_at)} for launches received in (since, until], across day files."""
    out = {}
    for day in sorted({utc_day(since), utc_day(until)}):
        c = T.ro(Path(store_dir) / ("%s.db" % day))
        if c is None:
            continue
        try:
            for mint, theme, rec in c.execute("SELECT mint, theme, launch_received_at FROM tags "
                                              "WHERE launch_received_at > ? AND launch_received_at <= ?", (since, until)):
                out[mint] = (theme, rec)
        finally:
            c.close()
    return out


def band_entries(desk, since, until):
    out = {}
    for day in sorted({utc_day(since), utc_day(until)}):
        try:
            lines = (Path(desk) / "candidate_emissions" / ("%s.jsonl" % day)).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                t = float(row["snapshot"]["emission_at"])
            except (ValueError, KeyError, TypeError):
                continue
            if since < t <= until:
                out.setdefault(row["mint"], t)
    return out


def theme_aggregates(store_dir, desk, tape_rows, now, sol_usd):
    """[(theme, window_s, launches, band_entries, buy_usd, buyers, slope)] for every theme in the taxonomy."""
    tags_hour = tags_since(store_dir, now - 7200, now)              # an older launch can still trade
    entries = band_entries(desk, now - WINDOWS[-1], now)
    rows = []
    for w in WINDOWS:
        launches, band, buy, buyers = (collections.Counter(), collections.Counter(), collections.Counter(),
                                       collections.defaultdict(set))
        for mint, (theme, rec) in tags_hour.items():
            if now - w < rec <= now:
                launches[theme] += 1
        for mint, t in entries.items():
            if now - w < t and mint in tags_hour:
                band[tags_hour[mint][0]] += 1
        for _, _, _, mint, is_buy, q, actor, _, _, _, t in tape_rows:
            if is_buy and now - w < t <= now and mint in tags_hour:
                theme = tags_hour[mint][0]
                buy[theme] += q
                buyers[theme].add(actor)
        for theme in R.THEMES:
            rows.append([theme, w, launches[theme], band[theme],
                         buy[theme] / 1e9 * sol_usd if sol_usd else None, len(buyers[theme]), None])
    by = {(r[0], r[1]): r for r in rows}
    for theme in R.THEMES:                                          # launch-rate slope, 15 min over 60 min
        l15, l60 = by[(theme, 900)][2], by[(theme, 3600)][2]
        if l60:
            by[(theme, 900)][6] = (l15 / 15) / (l60 / 60)
    return [tuple(r) for r in rows]


def token_hour(store_dir, desk, hour_start):
    """(rule_word, name_token) -> {token: (launches, distinct creators)} for launches received in
    [hour_start, hour_start + 1 h). Creators split a crowd from one wallet mass-launching the same name."""
    words, tokens = collections.defaultdict(list), collections.defaultdict(list)
    for day in sorted({utc_day(hour_start), utc_day(hour_start + 3599)}):
        src = T.ro(Path(desk) / "launches" / ("%s.db" % day))
        if src is None:
            continue
        try:
            rows = src.execute("SELECT symbol, name, creator FROM launches WHERE received_at >= ? AND received_at < ?",
                               (hour_start, hour_start + 3600)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            src.close()
        for symbol, name, creator in rows:
            theme, matched = R.tag(symbol, name)
            if matched:
                words[matched.strip()].append(creator)
            for tok in name_tokens(symbol, name):
                tokens[tok].append(creator)
    count = lambda d: {k: (len(v), len(set(v))) for k, v in d.items()}
    return count(words), count(tokens)


def write_token_hour(store, store_dir, desk, hour_start, now):
    words, tokens = token_hour(store_dir, desk, hour_start)
    pw, pt = token_hour(store_dir, desk, hour_start - 3600)
    db = store.db(now)
    for kind, cur, prev in (("rule_word", words, pw), ("name_token", tokens, pt)):
        ranked = sorted(cur.items(), key=lambda kv: (-kv[1][0], kv[0]))[:TOP_TOKENS]
        db.executemany("INSERT OR IGNORE INTO token_hourly VALUES (?,?,?,?,?,?,?,?,?)",
                       [(hour_start, kind, i + 1, tok, n, creators, prev.get(tok, (0, 0))[0], now, R.RULES_VERSION)
                        for i, (tok, (n, creators)) in enumerate(ranked)])
    db.commit()


def run_pass(store, desk, tape, now, state):
    tagged = ingest_tags(store, desk, now)
    tape.refresh(now)
    cov, sol = T.coverage(desk, now)
    db = store.db(now)
    db.executemany("INSERT OR IGNORE INTO theme_agg VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(now, *r, cov, R.RULES_VERSION) for r in theme_aggregates(store.out_dir, desk, tape.rows, now, sol)])
    db.commit()
    hour = int(now // 3600) * 3600 - 3600                             # the last CLOSED hour
    if state.get("hour") != hour:
        write_token_hour(store, store.out_dir, desk, hour, now)
        state["hour"] = hour
    return {"tagged": tagged, "coverage": cov}


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_NARRATIVE_ENABLED", "0").strip() != "1":
        print("[desk-narrative] MEMECOIN_DESK_NARRATIVE_ENABLED is not 1; exiting")
        return 0
    desk = Path(state_root()) / "data" / "desk"
    out = desk / "narrative" / "live-v2"
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / "narrative.lock", "w")
    try:                                               # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-narrative] another narrative worker holds the lock; exiting")
        return 0
    store, state = Store(out,ddl=LIVE.DDL), {}
    try:
        while True:
            t0 = time.time()
            from sniper.util.clock import real_utc_now
            print(json.dumps(LIVE.run_pass(store,desk,real_utc_now(),state,host_now=t0)),flush=True)
            if not a.loop:
                return 0
            time.sleep(max(1.0, PASS_S - (time.time() - t0)))
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
