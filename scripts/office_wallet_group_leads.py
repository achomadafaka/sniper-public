#!/usr/bin/env python3
"""Page existing wallet research evidence; no provider, watch, arming or money actions."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enabled',action='store_true')
    parser.add_argument('--state-root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--limit',type=int,default=100,help='Bounded page size; cursors retain subsequent pages')
    parser.add_argument('--pages',type=int,default=1,help='Number of resumable pages to process')
    args=parser.parse_args(argv)
    if not args.enabled:print('Disabled; no state reads, writes or provider calls.');return 0
    if not 1<=args.limit<=1000 or args.pages<1:parser.error('limit 1..1000 and positive pages required')
    from sniper.office.wallet_network import WalletNetwork
    from sniper.office.wallet_group_leads import scan_group_leads
    office=Path(args.state_root).resolve()/'data/office'
    if not (office/'wallet_network.db').is_file():parser.error('existing research graph required')
    os.nice(max(0,19-os.nice(0)))
    with (office/'wallet_group_leads.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print(json.dumps(dict(state='unknown',reason='group_scanner_already_running')));return 1
        net=WalletNetwork(office/'wallet_network.db',chain_db=office/'v2/chain.db')
        for _ in range(args.pages):
            result=scan_group_leads(net,net.clock(),limit=args.limit);print(json.dumps(result))
            if result['state']=='unknown':return 1
            if not result['has_more']:break
    return 0


if __name__=='__main__':raise SystemExit(main())
