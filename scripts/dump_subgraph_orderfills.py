"""Dump a wallet's full orderFilledEvent history via Goldsky (C2 reader).

Usage:
    python3 scripts/dump_subgraph_orderfills.py 0xWALLET [--out FILE]
                                                [--side maker|taker|both]
                                                [--max-rows N]

Closes wallet-protocol-bulletproofing C2 (>20K-row activity backfill).
The free Goldsky-public orderbook-subgraph endpoint has no row cap and
no auth; cursor pagination by id_gt walks the full history.

Output is JSON Lines (one row per line) so downstream scripts can stream
without holding the full set in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.wallet_protocol.subgraph_reader import (
    fetch_order_filled_events,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("wallet")
    p.add_argument("--out", default="-",
                   help="output path; '-' = stdout (default)")
    p.add_argument("--side", default="both",
                   choices=["maker", "taker", "both"])
    p.add_argument("--max-rows", type=int, default=None,
                   help="optional cap; default is unlimited")
    args = p.parse_args()

    session = requests.Session()
    t0 = time.time()
    print(
        f"# Fetching orderFilledEvents for {args.wallet} (side={args.side})...",
        file=sys.stderr,
    )
    rows = fetch_order_filled_events(
        args.wallet, session=session,
        side=args.side, max_rows=args.max_rows,
    )
    elapsed = time.time() - t0
    print(
        f"# Got {len(rows)} rows in {elapsed:.1f}s "
        f"(rate {len(rows) / max(elapsed, 1e-3):.1f} rows/s)",
        file=sys.stderr,
    )

    sink = sys.stdout if args.out == "-" else open(args.out, "w")
    try:
        for r in rows:
            sink.write(json.dumps(r) + "\n")
    finally:
        if sink is not sys.stdout:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
