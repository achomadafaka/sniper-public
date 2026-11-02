#!/usr/bin/env python3
"""Resumable header fetch for the v3 blind-replay cohort (4663). Idempotent: fetched headers are
cached forever in block_times, so re-runs only fetch the remainder. Steady throttled rate with a
hard socket timeout — bursty fetching trips the shared ingest RPC into silent throttles."""
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/home/bigpapi/Desktop/Sniper')
from dotenv import load_dotenv                                   # noqa: E402
load_dotenv('/home/bigpapi/Desktop/Sniper/.env')
socket.setdefaulttimeout(8)
from concurrent.futures import ThreadPoolExecutor                # noqa: E402

from sniper.execution.evm_wallet import _rpc                     # noqa: E402
from sniper.persistence import db_ticks as t                     # noqa: E402
from sniper.settings import memecoin as cfg                      # noqa: E402

rpc_url = cfg.evm_rpc_for_chain("4663")
tc = t.connect_ticks()
m = sqlite3.connect('/home/bigpapi/Desktop/Sniper/sniper_tracker.db')
m.row_factory = sqlite3.Row
w0 = tc.execute("SELECT MIN(observed_at) FROM pool_ticks WHERE chain_id='4663'").fetchone()[0]
mints = [r[0].lower() for r in m.execute(
    "SELECT DISTINCT mint FROM memecoin_positions WHERE network_id='4663' AND is_open=0 "
    "AND strategy IN ('control','real_shadow') AND opened_at >= ?", (w0,))]
need = set()
for mint in mints:
    rows = tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id='4663' AND quote_side IS NOT NULL "
        "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
        (mint, mint)).fetchall()
    if len(rows) != 1:
        continue
    need |= {r[0] for r in tc.execute(
        "SELECT DISTINCT block_no FROM pool_ticks WHERE chain_id='4663' AND pool=? "
        "AND block_no IS NOT NULL", (str(rows[0][0]).lower(),))}
have = {r[0] for r in tc.execute("SELECT block_no FROM block_times WHERE chain_id='4663'")}
missing = sorted(need - have)
print(f"missing {len(missing)} headers", flush=True)


def one(b):
    try:
        resp = _rpc(rpc_url, "eth_getBlockByNumber", [hex(int(b)), False])
        blk = resp.get("result") if isinstance(resp, dict) else None
        return (b, int(blk["timestamp"], 16)) if blk and blk.get("timestamp") else (b, None)
    except Exception:
        return (b, None)


done = 0
for i in range(0, len(missing), 2000):
    chunk = missing[i:i + 2000]
    with ThreadPoolExecutor(max_workers=4) as ex:
        got = {b: ts for b, ts in ex.map(one, chunk) if ts is not None}
    t.backfill_block_times(tc, "4663", list(got), lambda b: got.get(b),
                           datetime.now(timezone.utc).isoformat())
    done += len(got)
    print(f"progress {i + len(chunk)}/{len(missing)} fetched_ok={done}", flush=True)
    time.sleep(0.5)
print("HEADERS DONE", flush=True)
