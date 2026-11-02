#!/usr/bin/env python3
"""Exit price tape, part B: post-exit whole-bag quotes on the EVM research hosts, and on Solana
(stage 3b, MEMECOIN_EXIT_TAPE_SOLANA_ENABLED=1) PumpSwap pool-state values on the paid research Helius
key. Measurement only.

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §4a. Started once a minute
by scripts/memecoin_exit_tape_follow_watchdog.sh; the run lock below permits one process. It reads
the funded ledger READ-ONLY, writes only the tape file, and never signs or touches a wallet key.

    --dry-run   resolve the hosts and print them, no ledger read and no quote
    --once      one ledger poll and one worker pass, then exit (verification)
    --tape PATH --from-fill N   a verification run on its own file, starting after fill N
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files                         # noqa: E402
from sniper.util.state_paths import state_path, state_root              # noqa: E402
load_env_files(str(state_root()))
# The V4 PoolKey cache is read through db_core's default path: pin it to the state root's ledger,
# never a sniper_tracker.db relative to whatever directory launched this.
os.environ.setdefault("DB_PATH", str(state_path("sniper_tracker.db")))

from sniper.persistence.db_core import get_readonly_connection          # noqa: E402
from sniper.services import memecoin_exit_followup as fu                # noqa: E402
from sniper.util.clock import real_utc_now_iso                          # noqa: E402

logger = logging.getLogger("exit_tape_follow")
POLL_S = 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tape", default=None, help="tape file; default the state root's")
    ap.add_argument("--from-fill", type=int, default=None, help="start the cursor after this fill")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    usable, pending, refused = fu.resolve_hosts(os.environ)
    from urllib.parse import urlsplit
    logger.info("hosts usable=%s pending=%s refused=%s",
                {c: urlsplit(u).hostname for c, (u, _r) in usable.items()},
                {c: urlsplit(u).hostname for c, (u, _r) in pending.items()}, refused)
    sol_url, sol_why = fu.resolve_solana_host(os.environ)
    logger.info("solana lane: %s", ("on (B1 pool state, %s)" % urlsplit(sol_url).hostname) if sol_url else sol_why)
    if args.dry_run:
        return 0

    from sniper.services.memecoin_runner import acquire_run_lock
    _lock = acquire_run_lock(str(state_path("data", "memecoin_exit_tape_follow.lock"))
                             if args.tape is None else args.tape + ".lock")
    if _lock is None:
        return 0                                # a healthy follower holds it: the normal case

    tape_path = args.tape or str(state_path("data", "memecoin_exit_price_tape.db"))
    tape = fu.connect(tape_path)
    ledger = get_readonly_connection(str(state_path("sniper_tracker.db")))
    workers = {}
    for chain, (url, rps) in {**usable, **pending}.items():
        lane = fu.build_lane(chain, url, rps, pending=chain in pending)
        workers[chain] = fu.ChainWorker(chain, lane, tape_path)
    if sol_url:                                 # stage 3b, behind its own knob
        workers["solana"] = fu.SolanaWorker(fu.SolanaReader(sol_url, fu.SOLANA_RPS), tape_path)
    for f in fu.resumable(tape, set(workers)):
        workers[f["network_id"]].submit(f)
    if args.from_fill is not None:
        fu._meta(tape, "fill_cursor", int(args.from_fill))
    cursor = fu.init_cursor(tape, ledger)
    beat = state_path("data", "memecoin_exit_tape_follow.heartbeat")
    if args.once:
        conn = {c: fu.connect(tape_path) for c in workers}
    else:
        for w in workers.values():
            w.start()
    while True:
        now_iso = real_utc_now_iso()
        try:
            rows = fu.new_exits(ledger, cursor)
            for row in rows:
                follow = fu.record_exit(tape, ledger, row, lanes=workers, refused=refused,
                                        now_iso=now_iso)
                if follow is not None:
                    workers[follow["network_id"]].submit(follow)
                cursor = row["fill_id"]
            if rows:
                fu._meta(tape, "fill_cursor", cursor)
                logger.info("copied %d exit(s); cursor %d", len(rows), cursor)
        except Exception:
            logger.exception("exit tape follow: ledger poll failed; next poll continues")
        if args.once:
            for chain, w in workers.items():
                w.run_once(conn[chain])
        else:
            dead = [c for c, w in workers.items() if not w.thread.is_alive()]
            if dead:                            # no heartbeat: the watchdog restarts the process
                logger.error("exit tape follow: worker thread(s) %s died; exiting", dead)
                return 1
        try:
            beat.write_text(json.dumps({"ts": time.time(), "cursor": cursor,
                                        "workers": {c: w.stats() for c, w in workers.items()}},
                                       default=str))
        except Exception:
            logger.exception("could not write the heartbeat")
        if args.once:
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
