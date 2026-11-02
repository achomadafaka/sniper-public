#!/usr/bin/env python3
"""Entry feature capture, its own process (spec 2026-09-29-memecoin-entry-feature-capture-design.md).

Every few seconds: the terminal entry attempts the ledger has committed since the cursor, each
turned into one feature row in `data/memecoin_entry_features.db`. Phase P1 reads only committed
ledger rows (read-only connection) and our own EVM swap tape (read-only). It makes no network call,
and the funded runner never waits for it: the runner does not know it exists.

The tape keeps about an hour of blocks, so a window must be read within minutes of its decision.
A row captured later records `unavailable:tape_retention` rather than a guess.

    python3 scripts/memecoin_entry_capture_loop.py            # the watchdog starts this
    python3 scripts/memecoin_entry_capture_loop.py --once     # one pass, then exit
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The settings live with the state, not with the code: a pinned release has no .env, so loading
# from ROOT started on library defaults and lost MEMECOIN_ONCHAIN_V2_SWAP_CHAINS, which drops Base
# V2 pools from the tape's covered protocols (Gamma MED 3, 2026-09-29; the same defect as
# memecoin_refusal_capture_loop.py on 2026-09-18).
from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

load_env_files(str(state_root()))

from sniper.persistence import db_entry_features as store           # noqa: E402
from sniper.services import memecoin_entry_capture as cap           # noqa: E402
from sniper.util.clock import real_utc_now_iso                      # noqa: E402
from sniper.util.state_paths import state_path                      # noqa: E402

logger = logging.getLogger("memecoin")
PERIOD_S = 5.0


def ledger_path():
    """The funded ledger. The watchdog exports DB_PATH absolute; .env spells it relative
    ('sniper_tracker.db'), which a bare connect would resolve against a pinned release's own
    directory, where no ledger lives."""
    raw = os.environ.get("DB_PATH") or "sniper_tracker.db"
    return Path(raw) if Path(raw).is_absolute() else state_path(raw)


def open_tape():
    """Our EVM swap tape, read-only; None when the store does not exist."""
    path = state_path("data", "memecoin_onchain_volume.db")
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


T1_PERIOD_S = 10.0


def start_t1_worker():
    """P2, default OFF (MEMECOIN_ENTRY_CAPTURE_SOL_FLOW, _SOL_HOLDERS): the Solana T1 worker on its own
    thread, so a paced research read never holds the T0 capture (the EVM tape keeps one hour). The
    research key only (refused when it is also a funded key), through the key's shared pacer, and
    only while the funded Solana book is flat. Its own connections: a sqlite handle belongs to its
    thread. Returns the thread, or None."""
    import threading
    from sniper.services import memecoin_entry_capture_sol as sol
    if not (sol.enabled() or sol.holders_enabled()):
        return None
    from sniper.services.memecoin_slot_end_shadow import rpc_from_env
    rpc, why = rpc_from_env(os.environ)
    if rpc is None:
        print("[entry-capture] T1 Solana OFF: %s" % why, flush=True)
        return None

    def loop():
        db = store.connect(state_path("data", "memecoin_entry_features.db"))
        ledger = cap.open_ledger(str(ledger_path()))
        flat = sol.flat_check(ledger)
        while True:
            try:
                sol.run_t1_once(db, ledger, rpc, flat_fn=flat, now_ts=time.time())
            except Exception:
                logger.exception("entry capture T1 pass failed (keeps running)")
            time.sleep(T1_PERIOD_S)
    thread = threading.Thread(target=loop, name="entry-capture-t1", daemon=True)
    thread.start()
    print("[entry-capture] T1 Solana ON: flow=%s holders=%s (research key, shared pacer, flat-book "
          "gated)" % (sol.enabled(), sol.holders_enabled()), flush=True)
    return thread


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from sniper.services.memecoin_runner import acquire_run_lock
    lock = acquire_run_lock(str(state_path("data", "memecoin_entry_capture.lock")))
    if lock is None:
        print("[entry-capture] another capture loop holds the lock; exiting")
        return 0
    ledger = cap.open_ledger(str(ledger_path()))
    start_t1_worker()
    tape = None
    db = store.connect(state_path("data", "memecoin_entry_features.db"))
    heartbeat = state_path("data", "memecoin_entry_capture.heartbeat")
    total = 0
    while True:
        started = time.monotonic()
        stats = {}
        try:
            if tape is None:
                tape = open_tape()             # a tape that appears after start is picked up
            tapes = {chain: tape for chain in cap.TAPE_CHAINS} if tape is not None else {}
            written = cap.run_once(ledger, tapes, db, now_iso=real_utc_now_iso(), stats=stats)
            total += written
        except Exception:
            written = None
            logger.exception("entry capture pass failed")
        try:
            heartbeat.write_text(json.dumps({"ts": time.time(), "written": written,
                                             "total": total, "deferred": stats.get("deferred"),
                                             "pass_ms": round((time.monotonic() - started) * 1000, 1),
                                             "cursor": store.get_cursor(db)}))
        except OSError:
            logger.exception("entry capture heartbeat not written")
        if args.once:
            return 0
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    sys.exit(main())
