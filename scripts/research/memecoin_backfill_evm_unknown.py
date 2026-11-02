#!/usr/bin/env python3
"""One-time backfill (fix 3/3): re-attribute open positions stuck at network_id 'evm-unknown' to the
concrete chain the feed carried for the same mint on another event. Clears the current stuck rows
immediately instead of waiting for the sweep self-heal (fix 2) to reach them.

Uses the SAME helpers the entry path and sweep use — resolve_network_from_events (unambiguous only).
Truly-unresolvable mints (the feed never carried a concrete chain) are left as-is; they leave via the
24h unquotable guard, which is correct.

    python3 scripts/research/memecoin_backfill_evm_unknown.py               # dry-run (default)
    python3 scripts/research/memecoin_backfill_evm_unknown.py --apply       # backup + heal + verify
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from sniper.persistence import db_memecoin as m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--mode", default="paper")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row

    stuck = c.execute(
        "SELECT id, mint FROM memecoin_positions "
        "WHERE is_open=1 AND execution_mode=? AND mint LIKE '0x%' "
        "AND (network_id IS NULL OR network_id IN ('', 'evm-unknown'))", (args.mode,)).fetchall()
    by_mint = {}
    for r in stuck:
        by_mint.setdefault(r["mint"], []).append(r["id"])

    resolvable = unresolvable = 0
    print(f"stuck evm-unknown open positions: {len(stuck)} across {len(by_mint)} mints (mode={args.mode})")
    for mint, ids in sorted(by_mint.items()):
        chain = m.resolve_network_from_events(c, mint)
        if chain:
            resolvable += len(ids)
            print(f"  {mint[:18]}  x{len(ids):2}  -> {chain}")
        else:
            unresolvable += len(ids)
            print(f"  {mint[:18]}  x{len(ids):2}  -> UNRESOLVABLE (feed never carried a concrete chain; leave for 24h guard)")
    print(f"\nresolvable: {resolvable} positions   unresolvable: {unresolvable} positions")

    if not args.apply:
        print("\n(dry-run — pass --apply to back up and heal)")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"data/backups/sniper_tracker-preheal-evmunknown-{stamp}.db"
    # consistent point-in-time backup even with the runner writing concurrently (WAL)
    c.execute("VACUUM INTO ?", (backup,))
    print(f"\nbackup: {backup}")

    healed = m.heal_open_evm_unknown_from_events(c, mode=args.mode)
    remaining = c.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 AND execution_mode=? "
        "AND mint LIKE '0x%' AND (network_id IS NULL OR network_id IN ('', 'evm-unknown'))",
        (args.mode,)).fetchone()[0]
    print(f"healed: {healed} positions   still evm-unknown (unresolvable): {remaining}")


if __name__ == "__main__":
    main()
