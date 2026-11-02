#!/usr/bin/env python3
"""Anonymous Dex page worker. No activation or credentials; sheets read its local store.

Run explicitly: python3 scripts/memecoin_desk_dex_token_worker.py --desk-dir data/desk --loop
Three challenged probes disable the durable worker; operator re-enablement is deliberately manual.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sniper.services import memecoin_desk_dex_token as D
from sniper.util.state_paths import require_state_root_outside_release


def browser_capture(candidate):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context(service_workers='block')
            # Anonymous context: never load saved browser credentials or call a private API.
            page=context.new_page()
            response=page.goto('https://dexscreener.com/solana/'+candidate['pool'],
                               wait_until='domcontentloaded',timeout=8000)
            if response and response.status in (401,403,429):
                return {'state':'unknown','reason':'challenged'}
            if response is None or response.status>=400:
                return {'state':'unknown','reason':'read_error'}
            text=page.locator('body').inner_text(timeout=1000)[:500000]
            # A click without an explicit selected state cannot prove the stats' period.
            selected=page.locator('[aria-selected="true"], [aria-pressed="true"], [data-state="active"]').all_text_contents()
            links=page.locator('a[href]').evaluate_all('(nodes) => nodes.map(n => n.href)')
            mints=[candidate['mint']] if any(u.split('?')[0].rstrip('/').endswith('/token/'+candidate['mint']) for u in links) else []
            return D.page_claims(candidate,url=page.url,text=text,mints=mints,
                                 period_24h=any(t.strip().upper()=='24H' for t in selected))
        finally:
            browser.close()


def fetch(candidate):
    """Hard wall deadline includes browser startup; kill the whole browser process group."""
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--fetch',candidate['mint'],candidate['pool']],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        out,_=proc.communicate(timeout=D.DEFINITION['page_deadline_s'])
        payload=json.loads(out) if proc.returncode==0 and len(out)<=10000 else {'state':'unknown','reason':'read_error'}
    except (subprocess.TimeoutExpired,ValueError):
        payload={'state':'unknown','reason':'read_error'}
    finally:
        # Also reap descendants left behind by a failed browser/driver.
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
    return payload,time.time()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--desk-dir',type=Path)
    p.add_argument('--clock-file',type=Path)
    p.add_argument('--loop',action='store_true')
    p.add_argument('--fetch',nargs=2,metavar=('MINT','POOL'),help=argparse.SUPPRESS)
    a=p.parse_args(argv)
    if a.fetch:
        try: out=browser_capture(dict(network_id='solana',mint=a.fetch[0],pool=a.fetch[1]))
        except Exception: out={'state':'unknown','reason':'read_error'}
        print(json.dumps(out));return 0
    state=require_state_root_outside_release()
    desk_dir=a.desk_dir or state/'data/desk'
    clock_file=a.clock_file or state/'data/clock_offset.json'
    with D.Worker(desk_dir) as worker:
        while True:
            now=time.time()
            try:
                clock=json.loads(clock_file.read_text());offset=clock['offset_s'];sample_at=clock['at']
                if (not D._number(offset) or not D._number(sample_at) or not 0<=now-sample_at<=3600
                        or abs(offset)>172800): offset=None
            except (OSError,ValueError,KeyError,TypeError): offset=None
            status=worker.step(now=now,offset_s=offset,fetch=fetch)
            print(json.dumps(dict(host_at=now,status=status)),flush=True)
            if not a.loop: return 0
            time.sleep(D.DEFINITION['min_global_interval_s'])


if __name__=='__main__':
    raise SystemExit(main())
