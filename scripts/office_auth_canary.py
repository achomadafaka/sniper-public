#!/usr/bin/env python3
"""Run only after Zeta ACK and Alpha go, in the pinned canary supervisor unit/cgroup.

The launcher configuration must authenticate this exact entrypoint for the canary phase.
Standalone or unpinned invocations are refused by the launcher; never weaken its peer check.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sniper.office.auth_canary import run
from sniper.office.clock import corrected_now
from sniper.office.supervisor_client import build


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--config-sha256',required=True)
    parser.add_argument('--clock-file',type=Path,required=True)
    parser.add_argument('--provider',choices=('claude','codex'),required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--deadline-s',type=float,default=30.)
    parser.add_argument('--run-one-call',action='store_true')
    args=parser.parse_args(argv)
    if not args.run_one_call:
        print(json.dumps({'state':'refused','reason':'canary_not_requested','counts_as_evidence':False}))
        return 2
    clock=lambda:corrected_now(args.clock_file,host_now=time.time())
    transports=build(args.config,expected_sha256=args.config_sha256,clock=clock)
    report=run(args.provider,args.model,transport=transports[args.provider],clock=clock,deadline_s=args.deadline_s)
    print(json.dumps(report,allow_nan=False))
    return 0 if report['state']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
