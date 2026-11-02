#!/usr/bin/env python3
"""Print an UNAPPROVED look-v2 candidate; never write/register/approve a seal.

Canonical bytes go to stdout with no added newline. SHA256 and draft metadata go
on stderr. Gamma separately reviews and commits those exact bytes before any look.
Default limits are the explicit look ceilings; pass the same limits on replay.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sniper.office import eval_report as R,store as S


def prepare(office_path,ledger_path,*,desk_path,now,epoch_id='office-paper-eval-1',limits=None,sol_usd=None,sol_at=None):
    selected=dict(R.LOOK_CEILINGS,**(limits or {}))
    deadline=time.monotonic()+selected['deadline_s']
    def check():
        if time.monotonic()>deadline:raise ValueError('preparation_deadline')
    # No external record is supplied. This path cannot calculate economics.
    out=R.build_report(office_path,ledger_path,desk_path=desk_path,now=now,epoch_id=epoch_id,
                       mode='look',limits=selected,sol_usd=sol_usd,sol_at=sol_at)
    if (out.get('economics')!={'state':'withheld','reason':'look_record_required'} or
        not out.get('coverage',{}).get('required_complete')):
        raise ValueError('preparation_required_inputs_unknown')
    snapshot=out['descriptive_snapshot'];R.quarantine(snapshot,check=check)
    R.quarantine(out['safety_snapshot'],check=check)
    record={'version':'office-eval-look-v2','epoch_id':epoch_id,'look_at':now,
        'report_script_sha256':R.script_sha256(),'input_heads':out['input_heads'],
        'eligible_counts':out['eligibility'],'limits':out['limits'],'fx':{'sol_usd':sol_usd,'sol_at':sol_at},
        'receipt_seq_cut':out['receipt_seq_cut'],'safety_snapshot':out['safety_snapshot'],
        'descriptive_snapshot':snapshot,'descriptive_snapshot_sha256':R.digest(snapshot)}
    raw=S.canonical_json(record);check()
    if len(raw)>4*1024*1024:raise ValueError('preparation_record_budget')
    return {'state':'draft_unapproved','record_bytes':raw,'sha256':hashlib.sha256(raw).hexdigest(),
            'eligible_counts':out['eligibility'],'limits':out['limits']}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('office','ledger','desk'):p.add_argument('--'+name,required=True)
    p.add_argument('--now',type=float,required=True,help='explicit corrected UTC look cut')
    p.add_argument('--epoch-id',default='office-paper-eval-1')
    p.add_argument('--limits-json',help='look-limit overrides; default is the bounded look ceilings')
    p.add_argument('--sol-usd',type=float);p.add_argument('--sol-at',type=float)
    a=p.parse_args()
    try:
        out=prepare(a.office,a.ledger,desk_path=a.desk,now=a.now,epoch_id=a.epoch_id,
                    limits=json.loads(a.limits_json) if a.limits_json else None,sol_usd=a.sol_usd,sol_at=a.sol_at)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print(json.dumps({'state':'unknown','reason':str(exc)[:160],'approved':False}),file=sys.stderr)
        return 2
    sys.stdout.buffer.write(out['record_bytes'])
    print(json.dumps({k:v for k,v in out.items() if k!='record_bytes'},sort_keys=True,allow_nan=False),file=sys.stderr)
    return 0


if __name__=='__main__':raise SystemExit(main())
