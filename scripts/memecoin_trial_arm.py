#!/usr/bin/env python3
"""Start, end or list trial arms (spec docs/superpowers/specs/2026-10-01-memecoin-trial-arm-design.md
sections 5.1 and 5.6; rule 3a0b141ee). Dry run by default; --confirm writes. The operator runs
--confirm; nobody else.

    python3 scripts/memecoin_trial_arm.py start <handle> <chain> [--confirm]
    python3 scripts/memecoin_trial_arm.py end <handle> <chain> --keep-armed|--disarm [--ack-stop VERDICT] [--confirm]
    python3 scripts/memecoin_trial_arm.py status

START refuses when the chain has no trial clip (MEMECOIN_REAL_TRIAL_CLIP_USD_<chain>), the wallet is
excluded, already armed on that chain, already has an open trial there, has no proven address on that
chain (our on-chain detection could not see its buys), or shows no on-chain buy on that chain in the
last 7 days (the trial would only expire). J7 is out of the wallet path (operator 2026-10-02): the
evidence is our own, never a J7 delivery: onchain_wallet_detections at its addresses, and on Solana
the Wallet Map tape (it bought before one of our sources, or in the same slot as one of our sources).
It writes the trial row FIRST, then arms the
chain with force (several candidates fail the copy-PnL promote gate; that is why they are trials). A
refused arm deletes the row. END needs exactly one of --keep-armed / --disarm, so ending a trial never
silently leaves a wallet on the full clip. --keep-armed on a trial the grader stopped (DEMOTE, SHADOW,
INSUFFICIENT, or a stop with no verdict: STOPPED) also needs --ack-stop naming that verdict.

The logic lives in sniper/services/memecoin_trial_ops.py, shared with the Wallet Map's buttons (POST
/api/memecoin/wallets/<uid>/trial): one implementation, the same evidence gate, the same refusals.
This script resolves the handle and prints.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# THE OPS MODULE IS IMPORTED ONLY AFTER main() LOADS .env (2026-10-07, china_sniper_1): it imports
# sniper.settings.memecoin, whose constants are read at import, so a module-level import froze
# MEMECOIN_EVM_ALLOWLIST at its code default (no 56): an evm: id got no 56 address and the start
# refused 'no proven 56 address'.
def _ops():
    from sniper.services import memecoin_trial_ops as ops
    return ops


def start(conn, handle, chain, *, confirm, now_iso, evidence_ack=None):
    ops = _ops()
    w = ops.wallet_by_handle(conn, handle)
    missing = ops.tables_missing(conn)
    if w is None and not missing:
        from sniper.services.memecoin_chains import canonical_chain_id
        from sniper.settings import memecoin as cfg
        chain = canonical_chain_id(chain) or str(chain)
        return {"ok": False, "written": False, "handle": handle, "chain": chain,
                "clip_usd": cfg.trial_clip_usd_for(chain),
                "reason": "no single wallet with handle %r" % handle}
    return ops.start(conn, chain, uid=w[0] if w else None, handle=handle, confirm=confirm,
                     now_iso=now_iso, evidence_ack=evidence_ack)


def end(conn, handle, chain, *, keep_armed, disarm, confirm, now_iso, ack_stop=None):
    ops = _ops()
    w = ops.wallet_by_handle(conn, handle)
    return ops.end(conn, chain, uid=w[0] if w else None, handle=handle, keep_armed=keep_armed,
                   disarm=disarm, confirm=confirm, now_iso=now_iso, ack_stop=ack_stop)


def status(conn):
    return _ops().status(conn)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "end"):
        p = sub.add_parser(name)
        p.add_argument("handle")
        p.add_argument("chain")
        p.add_argument("--confirm", action="store_true")
        if name == "start":
            # A wallet we could not watch before arming has no detection to show (china_sniper_1);
            # the operator's text starts it anyway and is written into the arm reason.
            p.add_argument("--evidence-ack", default=None, metavar="TEXT")
        if name == "end":
            p.add_argument("--keep-armed", action="store_true")
            p.add_argument("--disarm", action="store_true")
            p.add_argument("--ack-stop", default=None, metavar="VERDICT")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    # The live .env and state paths, as every operator script: a config read without them is the
    # code default (no trial clip anywhere), and a relative DB_PATH would open the wrong ledger.
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env, state_root
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    from sniper.persistence.db_core import get_connection
    from sniper.util.clock import real_utc_now_iso
    conn = get_connection()
    try:
        if a.cmd == "status":
            rows, reason = status(conn)
            if reason:
                print(reason)
                return 1
            for row in rows:
                print(row)
            if not rows:
                print("no open trials")
            return 0
        if a.cmd == "start":
            out = start(conn, a.handle, a.chain, confirm=a.confirm, now_iso=real_utc_now_iso(),
                        evidence_ack=a.evidence_ack)
        else:
            out = end(conn, a.handle, a.chain, keep_armed=a.keep_armed, disarm=a.disarm,
                      confirm=a.confirm, now_iso=real_utc_now_iso(), ack_stop=a.ack_stop)
        print(out)
        if out["ok"] and not out["written"]:
            print("DRY RUN: nothing written. Re-run with --confirm.")
        return 0 if out["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
