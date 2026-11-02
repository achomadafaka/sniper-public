#!/usr/bin/env python3
"""Dump-group live worker CLI. Single instance (flock), nice 19 / ionice idle by the launcher, curve tape only (v1).
    nice -n 19 ionice -c3 python3 scripts/memecoin_desk_dump_group_worker.py [--state-dir data/desk] [--once N]
Writes <state-dir>/dump_group/cache.json (atomic) and worker.heartbeat.json. Exit 2 on a frozen detector (the watchdog restarts it into a full replay)."""
import argparse
import fcntl
import json
import os
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_dump_group_source as S  # noqa: E402
from sniper.services.memecoin_desk_dump_group_worker import Worker  # noqa: E402

LEDGER = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"


def ledger_signatures():
    """Our funded fills' signatures, read-only and short (secondary exclusion check; the primary is our wallet addresses)."""
    from sniper.persistence.db_core import get_readonly_connection
    con = get_readonly_connection(LEDGER)
    try:
        return [r[0] for r in con.execute("select tx_sig from memecoin_fills where network_id='solana' and tx_sig is not null")]
    finally:
        con.close()


def own_addresses():
    from sniper.services.memecoin_wallet_map import own_address_set
    return own_address_set(os.environ.get("MEMECOIN_OWN_ADDRESSES") or _env_file("MEMECOIN_OWN_ADDRESSES"))


def _env_file(key):
    try:
        for line in open(Path(LEDGER).parent / ".env"):             # the funded checkout's .env: a pinned release worktree (.runtime/dump_group/<sha>) has none
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=str(ROOT / "data" / "desk"))
    ap.add_argument("--once", type=int, default=0, help="run N cycles and exit (tests / smoke)")
    ap.add_argument("--replay-hours", type=float, default=24.0, help="history replayed at start (default the full 24 h lookback; a shorter value shortens the published flag memory AND the frequent-bot counts)")
    a = ap.parse_args()
    state = Path(a.state_dir) / "dump_group"
    state.mkdir(parents=True, exist_ok=True)
    lock = open(state / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("dump_group_worker: another instance holds the lock", file=sys.stderr)
        return 75
    own = own_addresses()
    if not own:
        print("dump_group_worker: WARNING no own addresses (MEMECOIN_OWN_ADDRESSES unset): exclusions invalid, no kill is ever proven", file=sys.stderr, flush=True)
    excl = S.Exclusions(own, ledger=ledger_signatures)
    w = Worker(a.state_dir, state / "cache.json", state / "worker.heartbeat.json", excl, replay_hours=a.replay_hours)
    n = 0
    while True:
        hb = w.cycle()
        n += 1
        if w.frozen:
            print(json.dumps({"frozen": w.frozen, "error": w.last_error}), file=sys.stderr, flush=True)
            return 2
        if a.once and n >= a.once:
            print(json.dumps({k: hb[k] for k in ("warm", "rows_fed", "episodes", "cursor_day", "cutoff", "published")}, default=str))
            return 0
        if hb.get("caught_up"):
            time.sleep(0.25)


if __name__ == "__main__":
    sys.exit(main())
