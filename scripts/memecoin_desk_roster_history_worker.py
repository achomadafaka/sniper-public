#!/usr/bin/env python3
"""Desk roster-history snapshot worker (sniper/services/memecoin_desk_roster_history.py). Reads memecoin_wallets
through get_readonly_connection only, every POLL_S seconds; writes <state>/data/desk/roster_history.db and a
heartbeat. Never writes the main ledger, never on an entry path. A failed read writes no row (the poll gap then
reads roster_unknown).

    python3 scripts/memecoin_desk_roster_history_worker.py --state-dir data/desk [--once]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("desk_roster_history")


def main(argv=None, *, clock=time.time, sleep=time.sleep, read=None) -> int:
    from sniper.services import memecoin_desk_roster_history as R
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", required=True)
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    desk = Path(a.state_dir)
    desk.mkdir(parents=True, exist_ok=True)
    lock = open(desk / "roster_history.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another roster-history worker holds the lock")
        return 1
    if read is None:
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_memecoin import roster_state_rows

        def read():
            conn = get_readonly_connection()
            try:
                return roster_state_rows(conn)
            finally:
                conn.close()
    store = R.open_store(desk / "roster_history.db")
    last = None
    while True:
        start = clock()                  # cadence only
        try:
            current = read()
            at = clock()                 # observation time AFTER the read: never earlier than the state seen (Delta P1)
            last = R.poll_once(store, current, at, last)
            (desk / "roster_history.heartbeat").write_text(json.dumps({"ts": at, "wallets": len(last)}))
        except Exception:
            log.exception("roster_history poll failed start=%s; no row written", start)
            last = None
        if a.once:
            return 0
        sleep(max(0.0, R.POLL_S - (clock() - start)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
