#!/usr/bin/env python3
"""Reconciler for fill-anchored position->pool resolution.

The post-entry hook in the runner loop resolves a position at the end of the iteration that filled
it. This sweeps up whatever it missed — a hook that fails silently is a gap nobody sees.

Safe to run repeatedly: the store is immutable and an already-resolved position costs no receipt
fetch, so a frequent cron is cheap. Reads the free public endpoint by default; a backfill is not
funded work and must not spend custody quota.

    python3 scripts/memecoin_position_pool_reconcile.py [limit]
"""
import os
import sys
import time
from pathlib import Path

# Anchored at the repo root, never the process CWD. A relative path here does not error when the
# cwd is elsewhere — `sqlite3.connect` CREATES an empty ledger and the run reports "nothing to
# reconcile", which reads exactly like a clean pass. Fails toward absent, which is the direction
# nobody investigates.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(str(ROOT))

import sniper.execution.evm_v4_quote as Q  # noqa: E402
from sniper.persistence import db_ticks as t  # noqa: E402
from sniper.services.memecoin_position_pool_worker import (  # noqa: E402
    resolve_pending_position_pools)
from sniper.util.clock import real_utc_now_iso  # noqa: E402

CHAIN = os.environ.get("MEMECOIN_POSITION_POOL_CHAIN", "4663")
RPC = os.environ.get("MEMECOIN_POSITION_POOL_RPC", "https://robinhood-rpc.publicnode.com")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 25


def main():
    import sqlite3
    mconn = sqlite3.connect(str(ROOT / "sniper_tracker.db"), timeout=30)
    mconn.row_factory = sqlite3.Row
    tc = t.connect_ticks()      # already anchored at the state root, not the CWD

    def receipt_fn(tx):
        time.sleep(0.25)                 # paced: the tape shares this endpoint at ~107 rows/sec
        return (Q._rpc(RPC, "eth_getTransactionReceipt", [tx]) or {}).get("result") or {}

    def key_fn(pool_id):
        return Q._pool_key(CHAIN, pool_id, RPC)

    rep = resolve_pending_position_pools(mconn, tc, CHAIN, receipt_fn=receipt_fn, key_fn=key_fn,
                                         now_iso=real_utc_now_iso(), limit=LIMIT)
    print(f"position-pool reconcile chain={CHAIN}: {rep}", flush=True)
    # unidentified is not an error: two candidate pools both holding the mint is an honest refusal
    if rep["unidentified"]:
        print(f"  {rep['unidentified']} position(s) could not be uniquely identified "
              f"(ambiguous receipt) — stored nothing, which is correct", flush=True)


if __name__ == "__main__":
    main()
