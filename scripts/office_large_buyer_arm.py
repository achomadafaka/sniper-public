#!/usr/bin/env python3
"""Runs the large-buyer paper arm: reads the tape, grades its own book. No LLM, no seat, no funded path."""
import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import large_buyer_arm as A  # noqa: E402


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db',type=Path,required=True);ap.add_argument('--desk-dir',type=Path,required=True);ap.add_argument('--heartbeat',type=Path,required=True);ap.add_argument('--period-s',type=float,default=3.);ap.add_argument('--until',type=float)
    args=ap.parse_args()
    args.db.parent.mkdir(parents=True,exist_ok=True)
    conn=A.open_db(args.db)
    tape=sqlite3.connect((args.desk_dir/'pumpswap'/'tape.db').resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    while args.until is None or time.time()<args.until:
        now=time.time()
        try:
            got=A.ingest(conn,tape,now);got.update(A.grade(conn,args.desk_dir,now));got.update(A.twin_tick(conn,args.desk_dir,now))
            tmp=args.heartbeat.with_suffix('.tmp');tmp.write_text(json.dumps(dict(ts=now,**got,**A.report(conn))));os.replace(tmp,args.heartbeat)
        except (sqlite3.Error,ValueError):
            logging.getLogger(__name__).exception('large buyer tick failed')
        time.sleep(max(.1,args.period_s-(time.time()-now)))


if __name__=='__main__':
    raise SystemExit(main())
