#!/usr/bin/env python3
"""Print a pilot slice drop-in, or read back exact live caps. Never installs or changes a service."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office.pilot_caps import render,verify,read_child_receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--child-receipt',type=Path,action='append',default=[])
    args=parser.parse_args(argv)
    if not args.check:
        print(render(args.manifest),end='')
        return 0
    try:
        result=subprocess.run(['systemctl','--user','show','office.slice',
            '--property=CPUQuotaPerSecUSec,MemoryMax,TasksMax,IOWeight,ControlGroup'],
            capture_output=True,text=True,timeout=2,check=True)
        properties=dict(line.split('=',1) for line in result.stdout.splitlines())
    except (OSError,ValueError,subprocess.SubprocessError):
        properties={}
    try:
        receipts=[read_child_receipt(path) for path in args.child_receipt]
        report=verify(args.manifest,properties,child_receipts=receipts)
    except (OSError,ValueError,TypeError):
        report={'state':'refused','reason':'child_receipt_unknown','ready_for_child_preexec':False}
    print(json.dumps(report,allow_nan=False))
    return 0 if report.get('ready_for_child_preexec') is True else 2


if __name__=='__main__':
    raise SystemExit(main())
