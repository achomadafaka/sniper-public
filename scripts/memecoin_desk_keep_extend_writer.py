#!/usr/bin/env python3
"""Write data/desk/pumpswap/keep_extend.json for the PumpSwap capture worker (one-way; the worker never reads a ledger).

Lists (a) the pool of every hard_filter PASS ticket first seen in the last 120 s and (b) the pool of every Office order that is not closed or censored.
Each pool carries an `until` in this host's receipt clock (time.time(), the clock the worker stamps rows with; nothing is converted): PASS pools until first
sight + SPAN_S + 60 s (the first sight is kept in keep_extend.state.json so the deadline never slides), open-order pools until now + SPAN_S, refreshed each run.
Read-only on both databases. Run from cron every minute; a missing run just lets the list go stale (the worker then ignores it).
  python3 scripts/memecoin_desk_keep_extend_writer.py [--state-root DIR]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.services import memecoin_desk_keep_extend as KE  # noqa: E402

NEW_PASS_S = 120.0
CLOSED = ("closed", "censored")


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def fresh_pass_pools(desk_db, now):
    """Pools of PASS tickets whose ts is within NEW_PASS_S of now. A ticket older than that was missed by the cadence and gets no extension (counted by the caller)."""
    conn = _ro(desk_db)
    try:
        rows = conn.execute("SELECT ts,pool FROM desk_tickets WHERE stage='hard_filter' AND verdict='PASS' AND pool IS NOT NULL ORDER BY id DESC LIMIT 400").fetchall()
    finally:
        conn.close()
    fresh, stale = set(), 0
    for ts, pool in rows:
        try:
            age = now - datetime.fromisoformat(ts).timestamp()
        except (TypeError, ValueError):
            continue
        if -5.0 <= age <= NEW_PASS_S:
            fresh.add(pool)
        elif NEW_PASS_S < age <= KE.SPAN_S:
            stale += 1
    return fresh, stale


def open_order_pools(ledger_db):
    if not Path(ledger_db).exists():
        return set()
    conn = _ro(ledger_db)
    try:
        return {r[0] for r in conn.execute("SELECT pool FROM orders WHERE state NOT IN (%s) AND pool IS NOT NULL" % ",".join("?" * len(CLOSED)), CLOSED)}
    finally:
        conn.close()


def build(prev_state, fresh, open_pools, now):
    """-> (pools for the file, new state). prev_state: {pool: first_seen}. A PASS pool keeps its first-seen deadline; a pool past its deadline leaves the state."""
    state = {p: t for p, t in prev_state.items() if now < t + KE.SPAN_S + 60.0}
    for p in fresh:
        state.setdefault(p, now)
    pools = {p: t + KE.SPAN_S + 60.0 for p, t in state.items()}
    for p in open_pools:
        pools[p] = max(pools.get(p, 0.0), now + KE.SPAN_S)
    return pools, state


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args(argv)
    root = args.state_root
    out = root / "data/desk/pumpswap" / KE.FILE_NAME
    state_path = root / "data/desk/pumpswap/keep_extend.state.json"
    now = time.time()
    try:
        prev = json.loads(state_path.read_text()) if state_path.exists() else {}
        prev = {p: float(t) for p, t in prev.items() if isinstance(p, str)}
    except (OSError, ValueError, TypeError):
        prev = {}                  # a lost state only shortens deadlines to a fresh +SPAN_S for pools seen again; it never extends past 60 s of lag
    fresh, stale = fresh_pass_pools(root / "data/memecoin_desk.db", now)
    open_pools = open_order_pools(root / "data/office/v2/explore-ledger/ledger.db")
    pools, state = build(prev, fresh, open_pools, now)
    KE.write(str(out), pools, now)
    tmp = str(state_path) + ".tmp.%d" % os.getpid()
    Path(tmp).write_text(json.dumps(state))
    os.replace(tmp, state_path)
    print(json.dumps(dict(pools=len(pools), new_pass=len(fresh), open_orders=len(open_pools), pass_missed_by_cadence=stale)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
