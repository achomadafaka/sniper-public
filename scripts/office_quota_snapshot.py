#!/usr/bin/env python3
"""One host-only quota read into a dedicated measurement DB; never calls a model.

Run before/after the Head proof with --db, --provider and --account-alias.
No cron is installed. Each invocation prunes at most 1000 rows/table older than
30 days. Credentials and provider account IDs never enter recorded snapshots.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.office_quota_read import poll_codex
from sniper.office import quota_measurement as Q
from sniper.persistence.db_core import get_readonly_connection
from sniper.util.clock import real_utc_now


def now():
    return real_utc_now()


def poll_claude(credentials, *, open_url=urllib.request.urlopen):
    try:
        with Path(credentials).open('rb') as fh:
            raw=fh.read(65537)
        if len(raw)>65536:
            return None,'invalid'
        token=json.loads(raw).get('claudeAiOauth',{}).get('accessToken')
        if not isinstance(token,str) or not token:
            return None,'auth'
        request=urllib.request.Request('https://api.anthropic.com/api/oauth/usage',headers={
            'Authorization':'Bearer '+token,'anthropic-beta':'oauth-2025-04-20','Accept':'application/json'})
        with open_url(request,timeout=10) as response:
            raw=response.read(100001)
        if len(raw)>100000:
            return None,'invalid'
        doc=json.loads(raw)
        return (doc,None) if isinstance(doc,dict) else (None,'invalid')
    except urllib.error.HTTPError as exc:
        return None,'rate_limited' if exc.code==429 else 'auth' if exc.code in (401,403) else 'transport'
    except TimeoutError:
        return None,'timeout'
    except (OSError,ValueError,AttributeError,TypeError):
        return None,'transport'



def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--provider',choices=('claude','codex'),required=True)
    parser.add_argument('--account-alias',required=True)
    parser.add_argument('--claude-credentials',type=Path,default=Path.home()/'.claude/.credentials.json')
    args=parser.parse_args(argv)
    # Refuse the Office/funded ledger before opening any existing file for write.
    if args.db.exists():
        conn=get_readonly_connection(str(args.db))
        try:
            conn.execute('PRAGMA busy_timeout=25')
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 16")}
            if tables-{'quota_wakes','quota_account_snapshots','seat_park_clears','quota_model_requests','quota_model_usage'}:
                raise ValueError('dedicated_quota_database_required')
        finally:conn.close()
    raw,error=poll_claude(args.claude_credentials) if args.provider=='claude' else poll_codex()
    at=now()
    args.db.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(args.db,timeout=.25)
    try:
        Q.ensure_schema(conn)
        row=Q.record_account_snapshot(conn,snapshot_id=uuid.uuid4().hex,provider=args.provider,
            account_alias=args.account_alias,observed_at=at,raw=raw,error=error)
        Q.prune(conn,now=at)
    finally:conn.close()
    print(json.dumps(row,sort_keys=True,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
