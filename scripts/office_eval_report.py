#!/usr/bin/env python3
"""Print bounded eval health, or a separately sealed look. Never registers a look."""
import argparse
import json
import os
import stat
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sniper.office.eval_report import build_report


def read_manifest(path):
    """A FIFO/device/symlink or oversized record must not hang a bounded report."""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>4*1024*1024:raise ValueError('look_file_budget')
        raw=stream.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('look_file_budget')
    return raw


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('office','ledger','desk'):p.add_argument('--'+key,required=True)
    p.add_argument('--now',type=float,required=True,help='explicit corrected UTC Unix seconds')
    p.add_argument('--epoch-id',default='office-paper-eval-1')
    p.add_argument('--mode',choices=['health','look'],default='health')
    p.add_argument('--limits-json',help='explicit bounded resource overrides; look ceilings are sealed')
    p.add_argument('--look-record');p.add_argument('--look-sha256')
    p.add_argument('--sol-usd',type=float);p.add_argument('--sol-at',type=float)
    a=p.parse_args()
    out=build_report(a.office,a.ledger,desk_path=a.desk,epoch_id=a.epoch_id,now=a.now,mode=a.mode,limits=json.loads(a.limits_json) if a.limits_json else None,
        look_record=read_manifest(a.look_record) if a.look_record else None,look_sha256=a.look_sha256,
        sol_usd=a.sol_usd,sol_at=a.sol_at)
    print(json.dumps(out,sort_keys=True,allow_nan=False))
    return 0 if out.get('economics',{}).get('state') in ('known','partial') or out['state']=='known' else 2


if __name__=='__main__':raise SystemExit(main())
