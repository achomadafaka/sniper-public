#!/usr/bin/env python3
"""Put one wallet on the paper roster (watch-only), the first step of the follow funnel.

J7 is out of the wallet path (operator 2026-10-02): fills come only from our own on-chain detection
of the armed roster's owners. The funnel is two operator steps, in this order:

    python3 scripts/memecoin_follow_add.py pump:<address> --handle <name> --confirm
    python3 scripts/memecoin_trial_arm.py start <name> <chain> --confirm

A pump.fun roster id is `pump:<Solana address>` (the id names the wallet the slot lane watches); an
EVM wallet's is `evm:<0x lowercase address>` (the EVM watcher watches that address on every allowed
EVM chain once armed there, e.g. china_sniper_1 on 56); a
Fomo uuid is a roster id with no address of its own, so the trial start refuses it until a proven
address exists. A retired (excluded) wallet is re-observed instead (`reobserve_wallet`: watch-only,
approval revoked). Nothing here arms anything: arming stays the trial script's and the operator's.
Dry run by default; --confirm writes.
ROLLBACK: python3 scripts/memecoin_roster_prune.py, or pause the wallet on the dashboard.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_trial_ops import valid_roster_uid as valid_uid  # noqa: E402,F401


def stored_handle(conn, uid):
    row = conn.execute("SELECT handle FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone()
    return (row[0] or "") if row else None


def plan(conn, uid):
    """(action, reason): add, reobserve, or nothing."""
    row = conn.execute("SELECT paused, COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id=?",
                       (uid,)).fetchone()
    if row is None:
        return "add", "not on the roster"
    if row[1]:
        return "reobserve", "retired (excluded)"
    if row[0]:
        return "add", "paused"
    return None, "already curated: paper copies it"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("uid")
    ap.add_argument("--handle", default="")
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    if not valid_uid(a.uid):
        print("REFUSE: %r is neither a Fomo uuid, pump:<Solana address> nor evm:<0x lowercase address>" % a.uid)
        return 1
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env, state_root
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    conn = get_connection() if a.confirm else get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    try:
        action, reason = plan(conn, a.uid)
        print("%s: %s -> %s" % (a.uid, reason, action or "nothing to do"))
        if action is None:
            return 0
        # add_wallet's manual UPSERT overwrites the handle: keep the stored name unless one is given,
        # and a new row needs a name (Gamma LOW on 20aa0ee91).
        handle = a.handle or stored_handle(conn, a.uid)
        if action == "add" and not handle:
            print("REFUSE: a new wallet needs --handle")
            return 1
        if not a.confirm:
            print("DRY RUN: nothing written. Re-run with --confirm.")
            return 0
        if action == "reobserve":
            ok = m.reobserve_wallet(conn, a.uid)
        else:
            m.add_wallet(conn, a.uid, handle, label="follow funnel")
            conn.commit()
            ok = True
        print("RESULT", "written" if ok else "refused")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
