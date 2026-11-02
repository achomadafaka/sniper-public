#!/usr/bin/env python3
"""Fill entry_token_age_s for funded Solana positions the pair source could not date.

The age comes from DexScreener's pairCreatedAt, and a LaunchLab curve is not indexed before it
graduates, so 30 of 32 funded LaunchLab closes since 2026-09-01 carry no age at all against 93% on
Raydium CPMM. Age at entry is a historical fact, so it is derived here — off the funded hot path,
on the research endpoint — from the oldest signature on the mint account.

    python3 scripts/memecoin_backfill_token_age.py --dry-run
    python3 scripts/memecoin_backfill_token_age.py --apply

A DERIVED AGE NEVER OVERWRITES AN INDEXED ONE. Rows that already carry a value are left alone and
only have their source labelled `dexscreener_pair`, which costs no network read. New values are
labelled `mint_first_slot` so a reader can always tell an inference from a venue timestamp.
"""
import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESEARCH_RPC = "https://solana-rpc.publicnode.com"


def _get_sigs_fn(url, *, sleep_s=0.15):
    """getSignaturesForAddress against the RESEARCH endpoint, never the funded custody lane.

    A research walk that shares the funded read quota is how a backfill becomes an outage
    (2026-08-21: a 2 s retry loop burned ~7k funded quota).
    """
    import json
    import urllib.request

    def call(address, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                           "params": [address, params]}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            # publicnode refuses the default python-urllib agent.
            "User-Agent": "Mozilla/5.0 (compatible; sniper-research/1.0)"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.load(resp)
        time.sleep(sleep_s)
        return payload.get("result") or []
    return call


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    ap.add_argument("--since", default="2026-09-01")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--rpc", default=RESEARCH_RPC)
    args = ap.parse_args(argv)

    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, configure_memecoin_state_env
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    from sniper.persistence.db_core import get_connection
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_token_age import (
        SOURCE_MINT_FIRST_SLOT, SOURCE_PAIR, age_seconds, mint_first_block_time)

    conn = get_connection()
    try:
        m.ensure_memecoin_tables(conn)
        # Label what is already there. No network, no overwrite, just provenance.
        labelled = conn.execute(
            "UPDATE memecoin_positions SET entry_token_age_source=? "
            "WHERE entry_token_age_s IS NOT NULL AND entry_token_age_source IS NULL",
            (SOURCE_PAIR,)).rowcount
        if args.apply:
            conn.commit()
        print("rows labelled %s: %d%s" % (SOURCE_PAIR, labelled,
                                          "" if args.apply else " (dry run, not committed)"))

        rows = conn.execute(
            "SELECT id, mint, opened_at FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana' "
            "  AND entry_token_age_s IS NULL AND opened_at>=? "
            "ORDER BY opened_at DESC LIMIT ?", (args.since, int(args.limit))).fetchall()
        print("positions needing an age: %d" % len(rows))
        if not rows:
            return 0

        get_sigs = _get_sigs_fn(args.rpc)
        filled = unreachable = 0
        for row in rows:
            first = mint_first_block_time(row["mint"], get_sigs_fn=get_sigs)
            age = age_seconds(row["opened_at"], first)
            if age is None:
                unreachable += 1
                continue
            filled += 1
            print("  pos %-7s %-12s age %10.0f s (%.1f h)"
                  % (row["id"], str(row["mint"])[:12], age, age / 3600.0))
            if args.apply:
                conn.execute(
                    "UPDATE memecoin_positions SET entry_token_age_s=?, entry_token_age_source=? "
                    "WHERE id=? AND entry_token_age_s IS NULL",
                    (age, SOURCE_MINT_FIRST_SLOT, row["id"]))
        if args.apply:
            conn.commit()
        print("\nderived %d, unreachable %d%s" % (filled, unreachable,
                                                  "" if args.apply else " (dry run)"))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
