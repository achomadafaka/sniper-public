#!/usr/bin/env python3
"""Read-only six-surface wallet campaign evidence; never orders or fills."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sniper.office.wallet_campaign_report import build_report
from sniper.office.wallet_campaign_quotes import reconcile_cash_flow,finite

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-root',default=str(Path(__file__).resolve().parents[2]))
    p.add_argument('--as-of',type=float,help='Host-epoch cutoff for the research graph');p.add_argument('--limit',type=int,default=100)
    p.add_argument('--cash-receipts',help='Optional local JSON list of confirmed receipt objects; no API calls')
    p.add_argument('--owner');p.add_argument('--commitment',choices=['confirmed','finalized'],default='confirmed')
    p.add_argument('--fx',help='Optional JSON {usdc_per_sol,at,ref}; dated conversion only')
    p.add_argument('--cash-cut-utc',type=float,help='Explicit UTC epoch cutoff for supplied chain receipts')
    args=p.parse_args(argv)
    out=build_report(args.state_root,as_of=args.as_of,limit=args.limit)
    if args.cash_receipts:
        if not args.owner:p.error('--cash-receipts requires --owner')
        path=Path(args.cash_receipts)
        if path.stat().st_size>4*1024*1024:p.error('cash receipt file exceeds4MiB')
        cash_cut=args.cash_cut_utc
        out['cash_clock']=dict(kind='explicit_utc',as_of_utc=cash_cut)
        if cash_cut is None:
            from sniper.services.memecoin_desk_intake import clock_sample
            offset,sampled_at=clock_sample(Path(args.state_root)/'data/clock_offset.json',now=out['as_of'])
            if offset is not None:
                cash_cut=out['as_of']-offset
                out['cash_clock']=dict(kind='corrected_host',as_of_utc=cash_cut,offset_s=offset,sampled_at=sampled_at)
            else:out['cash_clock']=dict(kind='unknown',as_of_utc=None)
        if cash_cut is not None and not finite(cash_cut):p.error('cash UTC cutoff must be finite and positive')
        out['measured_cash_flow']=(reconcile_cash_flow(json.loads(path.read_text()),owner=args.owner,commitment=args.commitment,as_of=cash_cut,fx=json.loads(args.fx) if args.fx else None)
            if cash_cut is not None else dict(state='unknown',reason='cash_clock_unknown',net_cash_usdc=None))
    print(json.dumps(out,sort_keys=True,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
