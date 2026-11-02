#!/usr/bin/env python3
"""Crew mail into Office seats, host-side: labelled crew, never the operator. A rerun mails again."""
import argparse,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import seat_events as E


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',default=str(Path(__file__).resolve().parents[1]/'data/office/office.db'))
    parser.add_argument('--role',required=True,help='sending crew role');parser.add_argument('--to',required=True,help='seat roles, comma separated')
    parser.add_argument('--file',required=True,help='UTF-8 message file')
    args=parser.parse_args()
    text=Path(args.file).read_text(encoding='utf-8')
    conn=sqlite3.connect(args.store,timeout=5,isolation_level=None)
    for role in args.to.split(','):
        print(role,E.crew_dm(conn,role=args.role,to_role=role,text=text,now=time.time()))


if __name__=='__main__':
    main()
