#!/usr/bin/env python3
"""Operator-only office-pilot-discovery-2 -> office-paper-eval-1 open. Default dry-run; --inspect prints the approval draft;
--apply opens the Office epoch, then the idempotent ledger stage (evaluated paper book). No services, no funded DB."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from sniper.office import eval_transition as X, ledger as L, pilot_models as PM, pilot_seed as P
from sniper.office.host_auth import approved_path
from sniper.office.launcher_client import read_regular
from sniper.office.runtime import _sample


def _unique(pairs):
    doc = dict(pairs)
    if len(doc) != len(pairs): raise ValueError('eval_duplicate_json_key')
    return doc


def main(argv=None, *, clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('--store', '--ledger', '--desk', '--clock-file', '--models'):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument('--models-sha256', required=True)
    parser.add_argument('--cohort-id', required=True)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--record-sha256')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args(argv)
    conn = ledger = desk = None
    try:
        if args.inspect and (args.apply or args.rollback): raise ValueError('eval_inspection_readonly')
        config_root = ROOT / 'config/office'; profile = config_root / 'prompts' / X.NEW
        approvals = json.loads(read_regular(approved_path(str(profile / 'sha256.json'))), object_pairs_hook=_unique)
        seed = {'schema': 'office-pilot-seed-launcher-v1', 'files': approvals['source_pins'],
                'prompts': {slot: {'text': read_regular(approved_path(str(profile / (slot + '.md')))).decode(),
                                   'sha256': approvals['files'][slot + '.md']} for slot in P.EVAL1_SLOTS}}
        if P.approved_profile(config_root, seed) != X.NEW: raise ValueError('eval_prompt_profile_unapproved')
        models = PM.load(args.models, args.models_sha256)
        if set(models['seats']) != set(PM.EVAL1_PROVIDERS): raise ValueError('eval_models_not_five_seats')
        book = json.loads(read_regular(approved_path(str(config_root / 'book_policy_pilot.json'))))
        now = clock() if clock is not None else _sample(args.clock_file)['corrected_utc']
        for path in (args.store, args.ledger, args.desk): approved_path(str(path))
        conn = sqlite3.connect(args.store.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=1.)
        writes_ledger = args.apply and not args.rollback
        ledger = L.open_ledger(args.ledger) if writes_ledger else None   # the Office stage never reads the ledger
        desk = sqlite3.connect(args.desk.resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        cohort = X.cohort_identity(desk, args.cohort_id)
        policy_hash = next(iter(json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM agents LIMIT 1')))['policy_hash']
        manifest = X.frozen_manifest(conn) or X.build_manifest(conn, prompts=seed['prompts'], models=models, policy_hash=policy_hash)
        if X.frozen_manifest(conn) is not None:
            X.adopted_runtime(conn, prompts=seed['prompts'], models=models, book_policy=book['policy'])   # pins still match
        if args.inspect:
            result = {'state': 'approval_draft', 'manifest_sha256': X._hash(manifest),
                      'record': X.approval_draft(conn, manifest, book_policy=book['policy'], near_pass_cohort=cohort)}
        else:
            if args.record is None or not X.E._hex(args.record_sha256, 64): raise ValueError('eval_approved_record_pin_required')
            raw = read_regular(approved_path(str(args.record)))
            if hashlib.sha256(raw).hexdigest() != args.record_sha256: raise ValueError('eval_record_hash_mismatch')
            record = json.loads(raw, object_pairs_hook=_unique)
            result = X.transition(conn, manifest=manifest, record=record, now=now, book_policy=book['policy'],
                                  near_pass_cohort=cohort, apply=args.apply, rollback=args.rollback)
            if args.apply and not args.rollback and result['state'] in ('transitioned', 'already_applied'):
                result['ledger'] = X.provision_ledger(ledger, now=now) or 'bound'
        print(json.dumps(result, allow_nan=False)); return 0
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError, StopIteration) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('eval_', 'pilot_', 'epoch_')) else 'eval_transition_input_unknown'
        print(json.dumps({'state': 'refused', 'reason': reason}), file=sys.stderr); return 2
    finally:
        for c in (conn, ledger, desk):
            if c is not None: c.close()


if __name__ == '__main__': raise SystemExit(main())
