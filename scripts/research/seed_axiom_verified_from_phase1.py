#!/usr/bin/env python3
"""Import five historical EVM candidates without proof authority.

The old receipt checks did not establish payment by each candidate.
The command inserts unverified rows and never replaces an existing proof.
The default mode prints candidates. --confirm inserts missing candidates.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.persistence.db_axiom_identity import insert_historical_candidates

DB = REPO / "sniper_tracker.db"
SOURCE = "phase1_evm_resolver + anchored recheck 2026-09-16"

# handle, address, anchored events, anchored bought, unanchored bought, unanchored reads, status
ROWS = [
    ("CryoPalmar", "0x38d5a4f4d620849f5859de604bbd59efb7d68bde", 10, 10, 10, 10, "axiom_confirmed"),
    ("chrollo0o0", "0xf102d9372e410e7498e4079599733b80351aff29", 10, 10, 10, 10, "axiom_confirmed"),
    ("RoaringKittyy", "0xc41ff324a9e61417833041c2ccb508683a079bb1", 2, 2, 4, 4, "unverified"),
    ("hopa", "0xd719b156d08e11523da7c326a17ecbfbc7689b07", 2, 2, 9, 9, "unverified"),
    ("MontyInFull", "0xd750158927f791eab81a5a551c82a958a585c689", 0, 0, 9, 10, "unverified"),
]
NETWORK_ID = "4663"


def build(conn):
    out = []
    for handle, address, anchored, bought, un_bought, un_reads, status in ROWS:
        row = conn.execute("SELECT source_user_id, handle FROM memecoin_wallets WHERE handle=?",
                           (handle,)).fetchone()
        if not row:
            print(f"SKIP {handle}: no memecoin_wallets row", file=sys.stderr)
            continue
        detail = json.dumps({"source": SOURCE, "code_class": "eip7702_delegated_eoa",
                             "anchored_events": anchored, "anchored_bought": bought,
                             "unanchored_bought": un_bought, "unanchored_reads": un_reads,
                             "status_basis": "legacy evidence requires verification",
                             "historical_status": status}, sort_keys=True)
        out.append((row[0], row[1], handle, NETWORK_ID, address, "unverified", 0, 0,
                    0, 0, detail))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True, help="print only (default)")
    group.add_argument("--confirm", action="store_true", help="write the rows")
    ap.add_argument("--db", default=str(DB), help="ledger path (default: the repo's own)")
    args = ap.parse_args()

    db = args.db
    read = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = build(read)
    read.close()
    for r in rows:
        print(f"{r[2]:<16} {r[4]} {r[3]}  {r[5]:<16} checked={r[9]} matches={r[6]}")
    if not args.confirm:
        print(f"\n--dry-run: {len(rows)} row(s) prepared, nothing written. "
              f"Use --confirm to write them.")
        return
    now = datetime.now(timezone.utc).isoformat()
    write = sqlite3.connect(db, timeout=30)
    try:
        ensure_axiom_tables(write)
        inserted = insert_historical_candidates(write, rows, now)
    finally:
        write.close()
    print(f"\n--confirm: inserted {inserted} row(s) to axiom_verified_addresses.")


if __name__ == "__main__":
    main()
