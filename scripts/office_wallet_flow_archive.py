#!/usr/bin/env python3
"""Prepare cold research packs read-only; the capture producer alone applies replacements."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office.wallet_flow_archive import archive_rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--before-host',type=float,help='Host-epoch receive cutoff; default keeps newest hour active')
    parser.add_argument('--limit',type=int,default=500,help='Work batch; remaining rows are retained for later calls')
    args=parser.parse_args();base=Path(args.state_root)/'data/desk/pumpswap'
    print(json.dumps(archive_rows(base/'tape.db',base/'wallet-flow-archive',before=args.before_host if args.before_host is not None else time.time()-3600,limit=args.limit),allow_nan=False))


if __name__=='__main__':main()
