#!/usr/bin/env python3
"""Clears a parked Office v2 seat as a logged host action. Run with the seat unit stopped. --restart-calibration voids earlier calibration wakes."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import seat_host as H

parser=argparse.ArgumentParser()
parser.add_argument('--config',required=True);parser.add_argument('--by',required=True,choices=('operator','head'));parser.add_argument('--reason',required=True)
parser.add_argument('--restart-calibration',action='store_true')
args=parser.parse_args()
cfg=json.loads(Path(args.config).read_text());now=time.time()
conn=sqlite3.connect(cfg['quota_db'],timeout=5)
H.clear_park(conn,actor_id=cfg['actor'],cleared_by=args.by,reason=args.reason,now=now)
if args.restart_calibration:
    path=Path(cfg['host_state']);state=json.loads(path.read_text())
    state.update(wake_types=[],calibration_since=now,per_wake_max=None,parked_reason=None)
    path.write_text(json.dumps(state))
print('cleared',cfg['actor'],now)
