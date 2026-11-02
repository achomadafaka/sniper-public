"""Compute and dump a wallet's capital ledger via the Goldsky activity-subgraph.

Usage:
    python3 scripts/dump_capital_ledger.py 0xWALLET
    python3 scripts/dump_capital_ledger.py 0xWALLET --json
    python3 scripts/dump_capital_ledger.py 0xWALLET --json --events

Closes wallet-protocol-bulletproofing C1 (deposit/withdraw ledger). Onchain
SPLIT events count as deposits, MERGE events as withdrawals; REDEMPTION
events are settlement payouts (NOT withdrawals — wallet doesn't control
timing/amount).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import requests

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.wallet_protocol.capital_ledger import (
    compute_capital_ledger,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("wallet")
    p.add_argument("--json", action="store_true", help="JSON output instead of text")
    p.add_argument(
        "--events", action="store_true",
        help="include the full event list in JSON output (very large)",
    )
    args = p.parse_args()

    session = requests.Session()
    t0 = time.time()
    print(f"# Fetching capital ledger for {args.wallet}...", file=sys.stderr)
    ledger = compute_capital_ledger(args.wallet, session=session)
    elapsed = time.time() - t0
    print(
        f"# {ledger.n_splits} splits + {ledger.n_merges} merges + "
        f"{ledger.n_redemptions} redemptions in {elapsed:.1f}s",
        file=sys.stderr,
    )

    if args.json:
        out = asdict(ledger)
        if not args.events:
            out["events"] = f"<{len(ledger.events)} events; pass --events to include>"
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    print(f"Wallet:                    {ledger.wallet}")
    print(f"Splits (deposits):         {ledger.n_splits}")
    print(f"Merges (withdrawals):      {ledger.n_merges}")
    print(f"Redemptions:               {ledger.n_redemptions}")
    print(f"Total deposited:           ${ledger.total_deposits_usdc:>15,.2f}")
    print(f"Total withdrawn:           ${ledger.total_withdrawals_usdc:>15,.2f}")
    print(f"Total redemption payout:   ${ledger.total_redemption_payout_usdc:>15,.2f}")
    print(f"Net capital funded:        ${ledger.net_capital_funded_usdc:>15,.2f}")
    if ledger.first_event_ts:
        print(f"First event ts:            {ledger.first_event_ts}")
        print(f"Last event ts:             {ledger.last_event_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
