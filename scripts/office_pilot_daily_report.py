#!/usr/bin/env python3
"""Print a read-only, completed UTC-day discovery report. No delivery side effects."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office.pilot_daily import build, render


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root',required=True,type=Path)
    parser.add_argument('--day',required=True,help='completed UTC day YYYY-MM-DD')
    parser.add_argument('--generated-at',required=True,type=float,help='trusted corrected UTC Unix seconds')
    parser.add_argument('--pins',type=Path,help='auditor-owned pinned chain heads (read only)')
    parser.add_argument('--host-samples',type=Path)
    parser.add_argument('--intake-root',type=Path)
    parser.add_argument('--previous-report',type=Path,help='previous canonical JSON report; embedded digest must verify')
    parser.add_argument('--paper-status',type=Path,help='paper engine status; default sibling desk/desk_paper_engine.status')
    parser.add_argument('--json',action='store_true',help='canonical JSON for the next report chain')
    args=parser.parse_args()
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[1],text=True,timeout=2).strip()
    report=build(args.state_root,day=args.day,generated_at=args.generated_at,code_sha=sha,pin_path=args.pins,host_path=args.host_samples,intake_root=args.intake_root,previous_report=args.previous_report,paper_status=args.paper_status or args.state_root.parent/'desk/desk_paper_engine.status')
    print(json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False) if args.json else render(report),end='\n' if args.json else '')


if __name__=='__main__':
    main()
