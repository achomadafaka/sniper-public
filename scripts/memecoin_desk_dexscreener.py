#!/usr/bin/env python3
"""Desk DexScreener context producer: boosts, profiles, takeovers, ads and trending metas, stored raw.

DESK CONTEXT ONLY (operator 4c930f3a; Alpha 2026-10-21): never on a fill, copy or funded-exit path, never
inside any study label or outcome (tape labels stay U-tape-only, Gamma),
never a price, fill or mark. A hard-filter fact whose only source is DexScreener is null. Every row
carries source='dexscreener' and our received_at. DexScreener's API terms (2023-08-18) allow commercial
and non-commercial use without a key on the free tier.

Budget: one GET per endpoint per pass (6 per minute in all; each endpoint's stated limit is 60/min). It
shares this host's IP with the funded runner's DexScreener reads, which use the separate tokens/pairs
endpoints. A 429 pauses every endpoint for PAUSE_ON_429_S; an error or bad JSON is a poll row with its
reason, never an empty success.

    MEMECOIN_DESK_DEXSCREENER_ENABLED=1 python3 scripts/memecoin_desk_dexscreener.py --loop
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

BASE = "https://api.dexscreener.com"
ENDPOINTS = ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1",
             "/community-takeovers/latest/v1", "/ads/latest/v1", "/metas/trending/v1")
PASS_S = 60.0
TIMEOUT_S = 10.0
PAUSE_ON_429_S = 120.0
RETENTION_DAYS = 30

DDL = """
CREATE TABLE IF NOT EXISTS polls (id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL, received_at REAL NOT NULL,
    status INTEGER, items INTEGER, error TEXT, source TEXT NOT NULL DEFAULT 'dexscreener');
CREATE TABLE IF NOT EXISTS items (poll_id INTEGER NOT NULL, endpoint TEXT NOT NULL, received_at REAL NOT NULL,
    chain_id TEXT, token_address TEXT, payload_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'dexscreener');
CREATE INDEX IF NOT EXISTS items_token ON items(chain_id, token_address, received_at);
"""


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def http_get(url, timeout=TIMEOUT_S):
    """(status, parsed JSON or None, error slug or None). Never raises."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sniper-desk/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None, "http_%d" % exc.code
    except (TimeoutError, OSError):
        return None, None, "transport"
    try:
        return status, json.loads(body), None
    except ValueError:
        return status, None, "malformed_json"


def items_of(endpoint, payload):
    """[(chain_id, token_address, item)]; metas have no token, so their address is NULL."""
    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        return None
    return [(it.get("chainId"), it.get("tokenAddress"), it) for it in rows if isinstance(it, dict)]


class Store:
    def __init__(self, out_dir, retention_days=RETENTION_DAYS):
        self.out_dir, self.day, self.conn, self.retention_days = Path(out_dir), None, None, retention_days

    def for_(self, at):
        day = utc_day(at)
        if day != self.day:
            if self.conn is not None:
                self.conn.commit()
                self.conn.close()
            self.conn = sqlite3.connect(self.out_dir / ("%s.db" % day), timeout=30)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(DDL)
            self.day = day
            if self.retention_days > 0:
                oldest = datetime.fromtimestamp(at - self.retention_days * 86400, timezone.utc).strftime("%Y-%m-%d")
                for p in self.out_dir.glob("????-??-??.db*"):
                    if p.name[:10] < oldest:
                        p.unlink(missing_ok=True)
        return self.conn

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()


def poll_pass(store, *, get=http_get, clock=time.time, paused_until=0.0):
    """One GET per endpoint unless paused. Returns (stats, paused_until)."""
    stats = {"ok": 0, "failed": 0, "items": 0, "paused": clock() < paused_until}
    if stats["paused"]:
        return stats, paused_until
    for ep in ENDPOINTS:
        status, payload, err = get(BASE + ep)
        at = clock()
        rows = items_of(ep, payload) if err is None else None
        if err is None and (status != 200 or rows is None):
            err = "http_%s" % status if status != 200 else "unexpected_shape"
        db = store.for_(at)
        cur = db.execute("INSERT INTO polls (endpoint, received_at, status, items, error) VALUES (?,?,?,?,?)",
                         (ep, at, status, None if err else len(rows), err))
        if err:
            stats["failed"] += 1
            if status == 429:                          # every endpoint backs off, not just this one
                db.commit()
                return {**stats, "paused": True}, at + PAUSE_ON_429_S
            continue
        db.executemany("INSERT INTO items (poll_id, endpoint, received_at, chain_id, token_address, payload_json) "
                       "VALUES (?,?,?,?,?,?)",
                       [(cur.lastrowid, ep, at, c, t, json.dumps(it, sort_keys=True)) for c, t, it in rows])
        stats["ok"] += 1
        stats["items"] += len(rows)
    store.conn.commit()
    return stats, paused_until


def main(argv=None, *, get=http_get):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_DEXSCREENER_ENABLED", "0").strip() != "1":
        print("[desk-dexscreener] MEMECOIN_DESK_DEXSCREENER_ENABLED is not 1; exiting")
        return 0
    out = Path(state_root()) / "data" / "desk" / "dexscreener"
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / "producer.lock", "w")
    try:                                               # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-dexscreener] another producer holds the lock; exiting")
        return 0
    store, paused_until = Store(out), 0.0
    try:
        while True:
            t0 = time.time()
            stats, paused_until = poll_pass(store, get=get, paused_until=paused_until)
            print(json.dumps(stats, sort_keys=True), flush=True)
            if not a.loop:
                return 0
            time.sleep(max(1.0, PASS_S - (time.time() - t0)))
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
