#!/usr/bin/env python3
"""One independent Office audit. All database handles are read-only; only trusted pins advance.
Single write (Alpha 2026-10-29): on a breach while office-paper-eval-1's evaluated book exists, one lazily opened
writable ledger handle calls ledger.halt_evaluation_entries(kind='zero_tolerance', actor='office-auditor') and nothing
else. The halt is irreversible and can only stop new entries.

The service owner must restrict --pin-dir and --observations to auditor authority.
Missing stores, observations, or initial trusted pins page as unknown, never clean.
"""
import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.office.auditor import audit_once, reconcile_ledger
from sniper.office.clock import corrected_now


EVAL_EPOCH = EVAL_BOOK = 'office-paper-eval-1'


def _breaches(result):
    return sorted(name + ':' + reason for name, check in result.get('checks', {}).items() for reason in check.get('breaches', []))


def halt_on_breach(ledger_path, result, *, now, halt=None):
    """The auditor's only write: the zero-tolerance halt of the eval-1 evaluated book."""
    from sniper.office import ledger as L
    halt = halt or L.halt_evaluation_entries
    conn = sqlite3.connect(Path(ledger_path).resolve().as_uri() + '?mode=rw', uri=True, timeout=1.)
    conn.row_factory = sqlite3.Row
    try:
        return halt(conn, epoch_id=EVAL_EPOCH, reason=(';'.join(_breaches(result)) or 'breach')[:160], actor='office-auditor',
                    now=now, kind='zero_tolerance')
    finally:
        conn.close()


def run_once(state_root, pin_dir, observations_path, *, now, page, max_age_s=120.):
    try:
        observations = json.loads(Path(observations_path).read_text())
        at = observations.get('at')
        if type(at) not in (int, float) or not 0 <= now - at <= max_age_s:
            observations = {}
        with ExitStack() as stack:
            handles = [stack.enter_context(closing(sqlite3.connect((Path(state_root) / name).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)))
                       for name in ('office.db', 'evaluation.db', 'ledger.db', 'null_ledger.db')]
            null = reconcile_ledger(handles[3])
            result = audit_once(*handles[:3], pin_dir, counters=observations.get('counters', {}),
                                heartbeats=observations.get('heartbeats', {}), now=now, max_age_s=max_age_s, page=page)
            result['checks']['null_reconciliation'] = null
            if null['state'] != 'clean':
                result['state'] = 'breach' if null['state'] == 'breach' else result['state'] if result['state'] == 'breach' else 'unknown'
                page(kind='office-audit', text='null_reconciliation:' + ','.join(null['breaches']))
            eval_open = handles[2].execute('SELECT 1 FROM books WHERE book_id=?', (EVAL_BOOK,)).fetchone() is not None
        if result['state'] == 'breach' and eval_open:
            result['eval_halt'] = halt_on_breach(Path(state_root) / 'ledger.db', result, now=now)
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        result = dict(state='unknown', at=now, reason=type(exc).__name__)
        result['page_result'] = page(kind='office-audit', text='audit_read_failed:' + type(exc).__name__)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--pin-dir', type=Path, required=True)
    parser.add_argument('--observations', type=Path, required=True)
    parser.add_argument('--clock-file', type=Path)
    args = parser.parse_args()
    from sniper.services.memecoin_alerts import send_alert
    now = corrected_now(args.clock_file or args.state_root.parent / 'clock_offset.json', host_now=time.time())
    if now is None:
        send_alert(kind='office-audit', text='clock_unknown')
        print(json.dumps({'state': 'unknown', 'reason': 'clock_unknown'}))
        return 2
    result = run_once(args.state_root, args.pin_dir, args.observations, now=now, page=send_alert)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {'clean': 0, 'breach': 1, 'unknown': 2}[result['state']]


if __name__ == '__main__':
    raise SystemExit(main())
