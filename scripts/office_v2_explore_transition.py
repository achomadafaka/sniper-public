#!/usr/bin/env python3
"""Operator-only office-v2-proof-1 -> office-v2-explore-1. Default dry-run; --apply seals the proof epoch, opens the explore epoch
(Head, Judge, Scout, change log segment 1), then provisions the evaluated paper book in the new ledger file. Seat models come from
config/office/seat_<role>_v2.json. No services, no funded DB, no model calls."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from sniper.office import ledger as L, v2_explore as X
from sniper.office.host_auth import approved_path
from sniper.office.runtime import _sample


def _seats(config_root):
    out = {}
    for role in X.ROLES:
        doc = json.loads((config_root / f'seat_{role}_v2.json').read_text())
        out[role] = {'family': doc['family'], 'model': doc['model']}
    return out


def main(argv=None, *, clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--clock-file', type=Path, required=True)
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--config-root', type=Path, default=ROOT / 'config/office')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    conn = ledger = None
    try:
        approved_path(str(args.store)); approved_path(str(args.ledger))
        now = clock() if clock is not None else _sample(args.clock_file)['corrected_utc']
        policy = json.loads((args.config_root / 'book_policy_pilot.json').read_text())['policy']
        conn = sqlite3.connect(args.store.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=1.)
        conn.execute('PRAGMA foreign_keys=ON')
        result = X.transition(conn, now=now, seats=_seats(args.config_root), book_policy=policy, release_sha=args.release_sha, apply=args.apply)
        if args.apply:
            ledger = L.open_ledger(args.ledger)
            result['ledger'] = X.provision_ledger(ledger, now=now) or 'bound'
            result['ready'] = X.ready(conn, ledger) or 'ready'
        print(json.dumps(result, allow_nan=False)); return 0
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('explore_', 'v2_', 'epoch_')) else 'explore_transition_input_unknown'
        print(json.dumps({'state': 'refused', 'reason': reason}), file=sys.stderr); return 2
    finally:
        for c in (conn, ledger):
            if c is not None: c.close()


if __name__ == '__main__': raise SystemExit(main())
