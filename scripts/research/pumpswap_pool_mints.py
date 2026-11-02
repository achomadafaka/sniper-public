#!/usr/bin/env python3
"""Post-hoc pool -> mint side table for the PumpSwap census (Alpha 2026-10-23, option a).

The universe worker maps a swap only from a create_pool event captured in its own tape, so pools
created before capture started stay 'unmapped' forever (by design: it never rewrites a prior row).
This script reads each unmapped pool's account ONCE on the research Helius key, through the shared
research pacer, and stores base_mint/quote_mint in its own file (data/desk/pumpswap/pool_mints.db).
The tape is opened read-only and never written. The census joins the side table at read time.

Pool account layout (PumpSwap Pool, verified 2026-10-23 against two tape create_pool rows):
  8 discriminator | u8 bump | u16 index | creator 32 @11 | base_mint 32 @43 | quote_mint 32 @75
status per pool: 'read' (owner is PumpSwap, data long enough), 'missing' (no account: closed or
never existed), 'wrong_owner', 'short_data'. Only 'read' rows carry mints; the rest stay unknown.
A row is written once; rerunning reads only pools without a row.

    python3 scripts/research/pumpswap_pool_mints.py
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO), str(REPO / "scripts" / "research")):
    if p not in sys.path:
        sys.path.insert(0, p)

PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BASE_OFF, QUOTE_OFF, MIN_LEN = 43, 75, 107
BATCH = 100                                     # getMultipleAccounts maximum

SCHEMA = ("CREATE TABLE IF NOT EXISTS pool_mints(pool TEXT PRIMARY KEY, base_mint TEXT, quote_mint TEXT, "
          "status TEXT NOT NULL, read_at REAL NOT NULL, context_slot INTEGER)")


def _b58(b):
    import base58
    return base58.b58encode(b).decode()


def decode(value):
    """(status, base_mint, quote_mint) for one getMultipleAccounts value."""
    if value is None:
        return "missing", None, None
    if value.get("owner") != PUMPSWAP_PROGRAM:
        return "wrong_owner", None, None
    data = base64.b64decode(value["data"][0])
    if len(data) < MIN_LEN:
        return "short_data", None, None
    return "read", _b58(data[BASE_OFF:BASE_OFF + 32]), _b58(data[QUOTE_OFF:QUOTE_OFF + 32])


def pending_pools(tape, side, lo, hi):
    """Unmapped pools with a swap RECEIVED in [lo, hi) (indexed swaps_ps_received range; the filter on map_status is inside it). Pools whose only unmapped rows
    are older than the window are not read here (stated bound): desk data accumulates and a reader reads only the latest window (operator order 2026-10-28)."""
    known = {r[0] for r in side.execute("SELECT pool FROM pool_mints")}
    rows = tape.execute("SELECT DISTINCT pool FROM swaps_ps WHERE received_at >= ? AND received_at < ? AND (map_status != 'mapped' OR map_status IS NULL)", (lo, hi))
    return sorted(r[0] for r in rows if r[0] not in known)


def fill(tape, side, fetch, *, window, now=time.time):
    """fetch(pools) -> (context_slot, [value|None per pool]). Raises on transport/RPC error: a failed batch
    writes nothing, so an error never reads as 'missing'. `window` = (lo, hi) received_at bounds: required, there is no whole-tape mode."""
    side.execute(SCHEMA)
    pools, done = pending_pools(tape, side, *window), 0
    for i in range(0, len(pools), BATCH):
        chunk = pools[i:i + BATCH]
        slot, values = fetch(chunk)
        if len(values) != len(chunk):
            raise ValueError("getMultipleAccounts returned %d values for %d pools" % (len(values), len(chunk)))
        at = now()
        side.executemany("INSERT OR IGNORE INTO pool_mints VALUES(?,?,?,?,?,?)",
                         [(p, b, q, s, at, slot) for p, (s, b, q) in zip(chunk, map(decode, values))])
        side.commit()
        done += len(chunk)
    return done


def research_fetch():
    from slot_lane_send_monitor import pick_research_url
    from sniper.services.memecoin_research_pacer import SharedPacer
    url = pick_research_url(os.environ)
    pacer = SharedPacer.for_url(url)

    def fetch(pools):
        pacer.acquire()
        body = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                "params": [pools, {"encoding": "base64", "commitment": "confirmed"}]}
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=20))
        if "error" in out:
            raise RuntimeError("getMultipleAccounts error: %s" % out["error"])
        return out["result"]["context"]["slot"], out["result"]["value"]
    return fetch


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tape", default=str(REPO / "data" / "desk" / "pumpswap" / "tape.db"))
    p.add_argument("--out", default=str(REPO / "data" / "desk" / "pumpswap" / "pool_mints.db"))
    p.add_argument("--hours", type=float, default=2.0, help="read unmapped pools RECEIVED in the last N hours (bounded window, no whole-tape scan)")
    a = p.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(os.environ.get("SNIPER_STATE_ROOT") or str(REPO))
    tape = sqlite3.connect("file:%s?mode=ro" % a.tape, uri=True, timeout=5)
    side = sqlite3.connect(a.out, timeout=5)
    side.execute(SCHEMA)                        # an empty file is a valid empty table for the census
    try:
        n = fill(tape, side, research_fetch(), window=(time.time() - a.hours * 3600, time.time() + 60))
        counts = dict(side.execute("SELECT status, COUNT(*) FROM pool_mints GROUP BY status"))
    finally:
        tape.close()
        side.close()
    print(json.dumps({"read_now": n, "by_status": counts}))


if __name__ == "__main__":
    main()
