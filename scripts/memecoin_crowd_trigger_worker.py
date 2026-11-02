#!/usr/bin/env python3
"""Standalone pilot trigger reader. Stored tape only; no Office jobs, paper grades or order writes."""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import large_buyer_arm as A

CYCLE_BUDGET_S=1.0
YIELD_AFTER_S=.6


def run(*,db,desk_dir,heartbeat,period_s=.5,until=None,clock=time.time,sleep=time.sleep):
    if type(period_s) not in (int,float) or not math.isfinite(period_s) or not 0<period_s<=1:
        raise ValueError('period must be finite and in (0,1] seconds')
    if until is not None and (type(until) not in (int,float) or not math.isfinite(until)):
        raise ValueError('until must be finite host UTC seconds')
    db,desk_dir,heartbeat=Path(db),Path(desk_dir),Path(heartbeat)
    db.parent.mkdir(parents=True,exist_ok=True);heartbeat.parent.mkdir(parents=True,exist_ok=True)
    conn=tape=None
    try:
        conn=A.open_db(db)
        tape=sqlite3.connect((desk_dir/'pumpswap/tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        for name,columns in [('swaps_ps_received',('received_at',)),('swaps_ps_pool_slot',('pool','slot'))]:
            if tuple(r[2] for r in tape.execute('PRAGMA index_info('+name+')'))!=columns:
                raise ValueError('required tape index unavailable: '+name)
        while until is None or clock()<until:
            started=clock()
            monotonic_started=time.monotonic()
            deadline=monotonic_started+CYCLE_BUDGET_S
            def yielding():return time.monotonic()-monotonic_started>=YIELD_AFTER_S
            def expired():return time.monotonic()>=deadline or until is not None and clock()>=until
            conn.execute('PRAGMA busy_timeout='+str(int(CYCLE_BUDGET_S*1000)))
            conn.set_progress_handler(expired,1000)
            tape.set_progress_handler(expired,1000)
            try:
                result=dict(state='known',**A.ingest(conn,tape,started,detected_now=clock,should_stop=expired,should_yield=yielding))
                if result['partial']:result['state']='partial'
            except (sqlite3.Error,ValueError) as exc:
                result=dict(state='unknown',reason=type(exc).__name__+': '+str(exc)[:200])
            finally:
                tape.set_progress_handler(None,0)
                conn.set_progress_handler(None,0)
            finished=clock()
            payload=dict(ts=finished,mode='trigger_only',cohort='large-buyer-crowd-v1',period_s=period_s,elapsed_s=finished-started,**result)
            temporary=heartbeat.with_name(heartbeat.name+'.'+str(os.getpid())+'.tmp')
            temporary.write_text(json.dumps(payload,allow_nan=False));os.replace(temporary,heartbeat)
            sleep(max(0,period_s-(clock()-started)))
    finally:
        if tape is not None:tape.close()
        if conn is not None:conn.close()
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--desk-dir',type=Path,required=True)
    parser.add_argument('--heartbeat',type=Path,required=True)
    parser.add_argument('--period-s',type=float,default=.5)
    parser.add_argument('--until',type=float)
    return run(**vars(parser.parse_args()))


if __name__=='__main__':
    raise SystemExit(main())
