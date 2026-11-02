#!/usr/bin/env python3
"""Operator-only discovery-1 -> discovery-2 transition. Default dry-run; no services or ledger writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from sniper.office import epoch_transition as E,pilot_seed as P
from sniper.office.host_auth import approved_path
from sniper.office.launcher_client import read_regular
from sniper.office.runtime import _sample


def _unique(pairs):
    doc=dict(pairs)
    if len(doc)!=len(pairs):raise ValueError('epoch_duplicate_json_key')
    return doc


def main(argv=None,*,clock=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--clock-file',type=Path,required=True)
    parser.add_argument('--record',type=Path)
    parser.add_argument('--record-sha256')
    parser.add_argument('--inspect',action='store_true',help='print a draft; fill old tool hash and Alpha approval before sealing')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args(argv)
    conn=ledger=None
    try:
        if args.inspect and (args.apply or args.rollback):raise ValueError('epoch_inspection_readonly')
        config_root=ROOT/'config/office';profile=config_root/'prompts/discovery2'
        approvals=json.loads(read_regular(approved_path(str(profile/'sha256.json'))),object_pairs_hook=_unique)
        seed={'schema':'office-pilot-seed-launcher-v1','files':approvals['source_pins'],
              'prompts':{slot:{'text':read_regular(approved_path(str(profile/(slot+'.md')))).decode(),
                               'sha256':approvals['files'][slot+'.md']} for slot in P.SLOTS}}
        if P.approved_profile(config_root,seed)!=E.NEW:raise ValueError('epoch_prompt_profile_unapproved')
        manifest,_=P.resolve_manifest(config_root=config_root,launcher_config=lambda by:seed,launcher_check=lambda by:by=='crew')
        guard=hashlib.sha256(read_regular(approved_path(str(config_root/'host_guard_pilot.json')))).hexdigest()
        now=clock() if clock is not None else _sample(args.clock_file)['corrected_utc']
        for path in (args.store,args.ledger):approved_path(str(path))
        conn=sqlite3.connect(args.store.resolve().as_uri()+('?mode=rw' if args.apply else '?mode=ro'),uri=True,timeout=1.)
        ledger=sqlite3.connect(args.ledger.resolve().as_uri()+'?mode=ro',uri=True,timeout=1.)
        conn.row_factory=ledger.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if args.inspect:
            result={'state':'approval_draft','record':E.approval_draft(conn,ledger,manifest,guard_hash=guard,release_sha=ROOT.name if E._hex(ROOT.name,40) else None)}
        else:
            if args.record is None or not E._hex(args.record_sha256,64):raise ValueError('epoch_approved_record_pin_required')
            raw=read_regular(approved_path(str(args.record)))
            if hashlib.sha256(raw).hexdigest()!=args.record_sha256:raise ValueError('epoch_record_hash_mismatch')
            record=json.loads(raw,object_pairs_hook=_unique)
            if not E._hex(ROOT.name,40) or record['release']['sha']!=ROOT.name:raise ValueError('epoch_exact_release_required')
            result=E.transition(conn,ledger,manifest=manifest,record=record,now=now,guard_hash=guard,apply=args.apply,rollback=args.rollback)
        print(json.dumps(result,allow_nan=False));return 0
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,RecursionError) as exc:
        reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('epoch_','pilot_')) else 'epoch_transition_input_unknown'
        print(json.dumps({'state':'refused','reason':reason}),file=sys.stderr);return 2
    finally:
        if conn is not None:conn.close()
        if ledger is not None:ledger.close()


if __name__=='__main__':raise SystemExit(main())
