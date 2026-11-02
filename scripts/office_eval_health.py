#!/usr/bin/env python3
"""Bounded post-start evidence; no startup, provider call, schema bootstrap or safety sample.

Pass an independently recorded corrected receipt boundary. ps UTC start and stored
corrected receipt clocks are shown separately; this script does not reconstruct an
old offset or assert loaded Python modules from a current symlink.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sniper.office.eval_report import Reader, DEFAULT_LIMITS, _object, _receipt, _number
from sniper.office.planned_validator import process_identity,require_process
from sniper.office import ledger as L,judge_decisions as J


def utc_lstart(pid):
    env=dict(os.environ,TZ='UTC',LC_ALL='C')
    value=subprocess.run(['ps','-p',str(pid),'-o','lstart='],env=env,capture_output=True,text=True,timeout=.25,check=True).stdout.strip()
    return datetime.strptime(value,'%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc).isoformat()


def release_identity(path):
    """Git identity with optional index refresh disabled; never executes candidate code."""
    env=dict(os.environ,GIT_OPTIONAL_LOCKS='0')
    def git(*args):
        return subprocess.run(['git','--no-optional-locks','-C',str(path),*args],env=env,
            capture_output=True,text=True,timeout=.25,check=True).stdout.strip()
    return {'sha':git('rev-parse','HEAD'),'tracked_clean':not git('status','--porcelain','--untracked-files=no')}


def inspect(office_path,ledger_path,*,pid,expected_release,current_link,expected_argv,expected_exe,
            expected_uid,now,receipt_boundary,epoch_id='office-paper-eval-1'):
    if not _number(now) or not _number(receipt_boundary) or not 0<=receipt_boundary<=now:
        raise ValueError('health_clock_arguments')
    r=Reader(DEFAULT_LIMITS)
    out={'state':'unknown','epoch_id':epoch_id,'as_of':now,'loaded_modules_proven':False,
         'receipt_boundary':receipt_boundary,'boundary_basis':'caller_supplied_corrected_utc',
         'epoch':{'state':'unknown'},'agents':{'state':'unknown'},'safety':{'state':'unknown'},
         'receipts':{'first_judge':{'state':'unknown'},'first_settle':{'state':'unknown'}}}
    try:
        expected=Path(expected_release).absolute()
        first=process_identity(pid)
        entry=1
        while entry<len(expected_argv) and expected_argv[entry] in ('-I','-B','-u'):entry+=1
        entrypoint=expected_argv[entry] if entry<len(expected_argv) else ''
        direct=str(expected/'scripts/office_supervisor.py');alias=str(Path(current_link)/'scripts/office_supervisor.py')
        if entrypoint not in (direct,alias):raise ValueError('unexpected_entrypoint')
        require_process(first,uid=expected_uid,birth_ticks=first['birth_ticks'],script=entrypoint,
                        exe=expected_exe,expected_argv=expected_argv)
        started=utc_lstart(pid)
        if datetime.fromisoformat(started).timestamp()>now:raise ValueError('process_start_future')
        out['process']={**first,'utc_lstart':started,'identity_basis':'rechecked_proc_birth_uid_exe_argv'}
        current=Path(current_link).resolve(strict=True)
        code=release_identity(expected)
        code_ok=code['sha']==expected.name and code['tracked_clean'] is True
        out['release']={'expected':str(expected),'code_identity':code,'current_resolved':str(current),'entrypoint_basis':'immutable_release' if entrypoint==direct else 'current_symlink','state':'known' if current==expected and code_ok else 'unknown',
                        'basis':'immutable_entrypoint_argv_and_current_link; loaded modules unproven'}
        o=r.open(office_path);l=r.open(ledger_path)
        controls=r.read(o,'office.office_epoch_control','SELECT * FROM office_epoch_control WHERE id=1')
        active=_object(controls[0]['payload_json']).get('active_epoch') if len(controls)==1 else None
        epochs=r.read(o,'office.epochs','SELECT * FROM epochs WHERE id=?',(epoch_id,))
        out['epoch']={'state':'known' if active==epoch_id and len(epochs)==1 else 'unknown','active_epoch':active}
        agents=r.read(o,'office.agents','SELECT * FROM agents ORDER BY id')
        values=[_object(a['payload_json']) for a in agents]
        roles=sorted((a.get('role'),a.get('family')) for a in values if a.get('state')=='active')
        expected_roles=sorted([('Head','claude'),('Scout','claude'),('Judge','claude'),('Judge','gpt'),('Postmortem','claude')])
        valid=(roles==expected_roles and len(values)==5 and all(type(a.get('generation')) is int and a['generation']>0 for a in values))
        out['agents']={'state':'known' if valid else 'unknown','active_count':len(roles),'roster':[
            {'id':a['id'],'role':v.get('role'),'family':v.get('family'),'state':v.get('state'),'generation':v.get('generation')}
            for a,v in zip(agents,values)]}
        rows=r.read(o,'office.judge_receipts',"SELECT * FROM receipts INDEXED BY receipts_judge_decide_seq "
            "WHERE json_extract(payload_json,'$.kind')='judge.decide' ORDER BY seq")
        judges=[]
        actors={a['id']:v for a,v in zip(agents,values)}
        rejected_identity=0
        for row in rows:
            p=_receipt(row)
            if p.get('epoch_id')==epoch_id and _number(p.get('known_at')) and receipt_boundary<=p['known_at']<=now:
                actor=actors.get(p.get('actor_id'),{})
                family={'judge_claude':'claude','judge_gpt':'gpt'}.get(p.get('slot'))
                if (actor.get('role')!='Judge' or actor.get('state')!='active' or family is None or
                    actor.get('family')!=family or actor.get('generation')!=p.get('generation')):
                    rejected_identity+=1;continue
                args={k:p[k] for k in ('decision','candidate','order_id','public_reason')}
                J.validate(args,candidates={p['candidate']},open_orders={p['order_id']})
                if J.decision_id(epoch_id,args)!=p['decision_id']:raise ValueError('judge_receipt_identity')
                judges.append((p,row['row_hash']))
        judges.sort(key=lambda item:(item[0]['known_at'],item[1]))
        out['receipts']['rejected_identity_count']=rejected_identity
        settles=[]
        for p,h in judges:
            did=p['decision_id'];keys=['judge.settle:'+did]
            if p.get('decision')=='EXIT':keys=['judge.settle:'+did+':'+p['slot']]
            for key in keys:
                rows=r.read(o,'office.settle:'+key,'SELECT * FROM receipts WHERE cache_key=? ORDER BY seq',(key,))
                for row in rows:
                    q=_receipt(row)
                    if (q.get('kind')=='judge.settle' and q.get('decision_id')==did and
                        q.get('epoch_id',epoch_id)==epoch_id and _number(q.get('known_at')) and
                        max(receipt_boundary,p['known_at'])<=q['known_at']<=now):settles.append((q,row['row_hash']))
        settles.sort(key=lambda item:(item[0]['known_at'],item[1]))
        for key,entries in (('first_judge',judges),('first_settle',settles)):
            if entries:
                p,h=entries[0]
                out['receipts'][key]={'state':'known','known_at':p['known_at'],'row_hash':h,'decision_id':p['decision_id'],
                                      'result_state':p.get('result',{}).get('state')}
        safety=r.read(l,'ledger.epoch_entry_safety','SELECT * FROM epoch_entry_safety WHERE epoch_id=?',(epoch_id,))
        if safety:
            out['safety']={k:v for k,v in L._public_safety(safety[0]).items() if k in
                {'state','reason','halt_kind','halted_at','last_at','unpriced_count','demoted_order_ids','repromotions'}}
        second=process_identity(pid)
        if first!=second:raise ValueError('process_changed_during_snapshot')
        r.check()
        if (not r.missing and current==expected and code_ok and valid and out['epoch']['state']=='known' and
            out['safety']['state']!='unknown' and judges and settles):out['state']='known'
        out['scope']='process and stored receipt evidence only; no claim of fills, usefulness, latency or organic availability'
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error,subprocess.SubprocessError,AttributeError,IndexError,UnicodeError,RecursionError) as exc:
        out['state']='unknown';out['reason']=str(exc)[:160]
    finally:
        out['coverage']={'complete':not r.missing,'required_missing':sorted(set(r.missing)),'input_heads':r.heads,
                         'limits':dict(DEFAULT_LIMITS)}
        r.close()
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('office','ledger','expected-release','current-link','expected-exe','expected-argv-json'):p.add_argument('--'+name,required=True)
    p.add_argument('--pid',type=int,required=True);p.add_argument('--expected-uid',type=int,required=True)
    p.add_argument('--now',type=float,required=True);p.add_argument('--receipt-boundary',type=float,required=True)
    p.add_argument('--epoch-id',default='office-paper-eval-1')
    a=p.parse_args()
    out=inspect(a.office,a.ledger,pid=a.pid,expected_release=a.expected_release,current_link=a.current_link,
        expected_argv=json.loads(a.expected_argv_json),expected_exe=a.expected_exe,expected_uid=a.expected_uid,
        now=a.now,receipt_boundary=a.receipt_boundary,epoch_id=a.epoch_id)
    print(json.dumps(out,sort_keys=True,allow_nan=False));return 0 if out['state']=='known' else 2


if __name__=='__main__':raise SystemExit(main())
