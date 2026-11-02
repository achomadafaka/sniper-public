#!/usr/bin/env python3
"""Operator-only office-paper-eval-1 -> office-v2-proof-1. Default dry-run; --apply closes eval-1 as STOPPED_BY_OPERATOR
(recorded stop 2026-10-30T09:48:08Z), then opens the proof epoch with one Head actor and seeds the v2 roster.
Office store only: no ledger, no services, no funded DB, no model calls."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from sniper.office import v2_transition as V
from sniper.office.host_auth import approved_path
from sniper.office.runtime import _sample


def main(argv=None, *, clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--clock-file', type=Path, required=True)
    parser.add_argument('--model', default=V.MODEL)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    conn = None
    try:
        approved_path(str(args.store))
        now = clock() if clock is not None else _sample(args.clock_file)['corrected_utc']
        conn = sqlite3.connect(args.store.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=1.)
        conn.execute('PRAGMA foreign_keys=ON')
        print(json.dumps(V.transition(conn, now=now, model=args.model, apply=args.apply), allow_nan=False)); return 0
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('v2_', 'epoch_')) else 'v2_transition_input_unknown'
        print(json.dumps({'state': 'refused', 'reason': reason}), file=sys.stderr); return 2
    finally:
        if conn is not None: conn.close()


if __name__ == '__main__': raise SystemExit(main())
