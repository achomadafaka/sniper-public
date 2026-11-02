#!/usr/bin/env python3
"""Add a no-order seat to the open explore epoch: roster row, logged first. Idempotent."""
import argparse,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import seat_events as E,v2_explore as X


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--store',required=True);parser.add_argument('--role',required=True,choices=X.ADDED_ROLES);parser.add_argument('--release-sha',required=True)
    args=parser.parse_args()
    conn=sqlite3.connect(args.store,timeout=5,isolation_level=None);E.ensure_schema(conn)
    print(X.add_seat(conn,role=args.role,release_sha=args.release_sha,now=time.time()))


if __name__=='__main__':main()
