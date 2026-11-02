#!/usr/bin/env python3
"""Claim lag of the manual-intent queue, before vs after the woken worker (desk build step 2).

BEFORE is the record: requested_at -> claimed_at of the historical claimed intents, read-only from
the ledger (n=445 on 2026-10-21). AFTER has two parts, measured separately and added per intent:

  wake    the real ManualExitWorker + LaneWake on a temp DB: enqueue -> claim, idle worker, one
          enqueue per historical intent. Empirical, this host.
  queue   the wait behind earlier intents, replayed exactly from the historical arrival times and
          claim -> resolve service times through one FIFO worker (one buy at a time, as pass_lock
          enforces). Arithmetic on recorded timestamps, no threads.

The replay ignores operator-first ordering (desk intents did not exist) and the main loop's other
work, which no longer delays a woken worker. It is a harness number, not a production read: the
production read is requested_at -> claimed_at after deploy.

    python3 scripts/research/manual_intent_claim_lag_replay.py [--limit 500] [--ledger PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _t(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def _pct(xs):
    xs = sorted(xs)
    if not xs:
        return None
    pick = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return {"n": len(xs), "p50": round(pick(0.5), 3), "p90": round(pick(0.9), 3),
            "max": round(xs[-1], 3)}


def history(conn, limit):
    rows = conn.execute(
        "SELECT requested_at, claimed_at, resolved_at FROM memecoin_manual_intents "
        "WHERE claimed_at IS NOT NULL AND resolved_at IS NOT NULL "
        "ORDER BY requested_at DESC LIMIT ?", (int(limit),)).fetchall()
    out = [(_t(r[0]), _t(r[1]), _t(r[2])) for r in rows]
    return sorted(out)


def queue_waits(rows):
    """FIFO single server: wait_i = max(0, free_at - arrival_i); service_i = recorded claim->resolve."""
    free_at, waits = 0.0, []
    for arrival, claimed, resolved in rows:
        start = max(arrival, free_at)
        waits.append(start - arrival)
        free_at = start + max(0.0, resolved - claimed)
    return waits


def wake_latencies(n):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_manual_intents as mi
    from sniper.services.memecoin_manual_exit_intents import ManualExitWorker
    from sniper.services.memecoin_onchain_wake import LaneWake
    from sniper.util.clock import real_utc_now_iso
    tmp = Path(tempfile.mkdtemp(prefix="intent-replay-"))
    db, address = tmp / "ledger.db", str(tmp / "wake")
    seed = sqlite3.connect(db)
    m._ensure_memecoin_tables_in_transaction(seed)
    seed.commit()
    seed.close()
    def connect():
        conn = sqlite3.connect(str(db), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row      # as the runner's get_connection
        return conn
    claimed, stamps = threading.Event(), []

    def buy_fn(conn, mint, uid=None, **kw):
        stamps.append(time.monotonic())
        claimed.set()
        return {"status": "refused", "reason": "replay"}

    original = mi.wake_address
    mi.wake_address = lambda: address
    worker = ManualExitWorker(connect=connect, observe=connect,
                              drain_fn=lambda c: mi.drain(c, buy_fn, now_fn=real_utc_now_iso),
                              pending_fn=mi.pending_count, name="intent-replay",
                              wake=LaneWake(address))
    worker.start()
    out = []
    try:
        time.sleep(0.3)
        c = connect()
        for i in range(n):
            claimed.clear()
            started = time.monotonic()
            mi.enqueue(c, mint=f"M{i}", network_id="solana", now_iso=real_utc_now_iso())
            if not claimed.wait(5.0):
                raise RuntimeError(f"intent {i} never claimed")
            out.append(stamps[-1] - started)
            time.sleep(0.05)
        c.close()
    finally:
        worker.stop()
        mi.wake_address = original
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--ledger", default=None, help="ledger path (default: the state root's)")
    args = ap.parse_args(argv)
    from sniper.persistence import db_core
    ledger = db_core.get_readonly_connection(args.ledger)
    try:
        rows = history(ledger, args.limit)
    finally:
        ledger.close()
    before = [claimed - requested for requested, claimed, _ in rows]
    waits = queue_waits(rows)
    wakes = wake_latencies(len(rows))
    after = [w + q for w, q in zip(wakes, waits)]
    print(json.dumps({
        "window_local_time": [datetime.fromtimestamp(rows[0][0]).isoformat(),
                   datetime.fromtimestamp(rows[-1][0]).isoformat()] if rows else None,
        "before_claim_lag_s": _pct(before),
        "after_wake_s": _pct(wakes),
        "after_queue_wait_s": _pct(waits),
        "after_claim_lag_s": _pct(after),
        "scope": "harness: empirical wake on this host + exact FIFO replay of recorded times",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
