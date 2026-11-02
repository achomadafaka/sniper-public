#!/usr/bin/env python3
"""Fee reconciler CLI (sniper/services/memecoin_fee_reconciler.py): network fees of booked funded fills.

Default is a DRY RUN: it prints the fills-without-fee counter per chain/kind and each chain's backlog size,
with no network call. --live spends research-host calls and needs MEMECOIN_FEE_RECONCILER_ENABLED=1, which
the operator sets after approving the first backfill (research quota). Each chain pass stops on its first
error or 429; --limit bounds the txs per chain per run.

    python3 scripts/memecoin_fee_reconciler.py                          # dry run, all chains
    python3 scripts/memecoin_fee_reconciler.py --live --chain solana --limit 50
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402


def main(argv=None, *, post=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chain", action="append", choices=sorted(("solana", "8453", "56", "4663")))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--live", action="store_true")
    p.add_argument("--db")
    a = p.parse_args(argv)
    load_env_files(str(state_root()))
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection
    from sniper.services import memecoin_fee_reconciler as F
    chains = a.chain or ["solana", "8453", "56", "4663"]
    conn = get_connection(a.db) if a.db else get_connection()
    try:
        report = {"fills_without_fee": m.fills_without_fee(conn), "mode": "live" if a.live else "dry_run"}
        if not a.live:
            report["backlog"] = {c: len(m.sol_fee_backlog(conn, limit=10**6) if c == "solana"
                                     else m.evm_fee_backlog(conn, c, limit=10**6)) for c in chains}
            print(json.dumps(report, sort_keys=True))
            return 0
        if os.getenv("MEMECOIN_FEE_RECONCILER_ENABLED", "0").strip() != "1":
            print("[fee-reconciler] --live needs MEMECOIN_FEE_RECONCILER_ENABLED=1 (operator approves the first "
                  "backfill); nothing sent", file=sys.stderr)
            return 2
        report["passes"] = {}
        for chain in chains:
            try:
                url = F.host_for(chain, os.environ)
            except ValueError as exc:
                report["passes"][chain] = {"refused": str(exc)}
                continue
            _, gap, cap = F.HOSTS[chain]
            report["passes"][chain] = F.run_pass(conn, chain, F.Paced(url, gap, cap, post=post), limit=a.limit)
        report["fills_without_fee_after"] = m.fills_without_fee(conn)
        print(json.dumps(report, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
