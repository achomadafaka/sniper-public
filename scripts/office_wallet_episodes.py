#!/usr/bin/env python3
"""Rebuilds drain episodes for watch-listed wallets from stored PumpSwap rows, live tape plus plain and packed archive days. Read-only on the tapes; whole-table scans, so nice it and run it off any seat path.

    python3 scripts/office_wallet_episodes.py [--state-root DIR] [--no-archive]"""
import argparse
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.office import wallet_watch as WW
from sniper.office.wallet_network import WalletNetwork
from sniper.services import memecoin_desk_ps_archive as AR
from scripts.office_outcome_table import archive_days


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-root', default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--no-archive', action='store_true')
    args = ap.parse_args()
    root = Path(args.state_root)
    net = WalletNetwork(root / 'data/office/wallet_network.db')
    wallets = [r['address'] for r in WW.read_list(net)]
    if not wallets:
        print('no listed wallets'); return 0
    found = {w: [] for w in wallets}
    tape = sqlite3.connect((root / 'data/desk/pumpswap/tape.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    for w, rows in WW.scan(tape, wallets).items(): found[w] += rows
    tape.close()
    gone = []
    if not args.no_archive:
        arch = root / 'data/archive/pumpswap'
        for day, _packed in archive_days(arch):
            try:
                with AR.open_day(arch, day, scratch_dir=root / 'data/archive/scratch-episodes') as conn:
                    for w, rows in WW.scan(conn, wallets).items(): found[w] += rows
            except (OSError, sqlite3.Error) as exc:
                gone.append(day)
    print(dict(wallets=len(wallets), rows=sum(len(v) for v in found.values()), episodes=WW.store_episodes(net, {w: WW.unique_swaps(v) for w, v in found.items()}, time.time()), days_unread=gone))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
