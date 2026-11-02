#!/usr/bin/env python3
"""Disabled-by-default, finite read-only Solana research intake; no funded daemon registration."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--enabled',action='store_true')
    ap.add_argument('--manifest',required=True)
    ap.add_argument('--research-rpc-url')
    ap.add_argument('--max-calls',type=int)
    ap.add_argument('--max-wallets',type=int)
    ap.add_argument('--min-call-s',type=float)
    ap.add_argument('--db',required=True)
    ap.add_argument('--quote-db')
    ap.add_argument('--quote-url', help='isolated unsigned Jupiter-compatible research endpoint')
    ap.add_argument('--state-root',default='/home/bigpapi/Desktop/Sniper')
    args=ap.parse_args(argv)
    manifest=json.loads(Path(args.manifest).read_text())
    if not args.enabled:
        print('Disabled; no provider calls or database writes.')
        return 0
    from sniper.services.wallet_observation import (validate_manifest,validate_budget,collect_tick,
        schedule_research_quotes,run_research_quote_tick,observation_owners)
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_wallet_observation import open_observation_db,open_research_quote_db,freeze_manifest
    from sniper.services.memecoin_solana_blocktime import build_read_rpc_call
    from sniper.util.clock import real_utc_now_iso
    root=Path(args.state_root)
    from sniper.util.envfiles import load_env_files
    load_env_files(root)
    from sniper.settings import memecoin as cfg
    budget=dict(manifest.get('budget') or {},rpc_url=args.research_rpc_url,max_calls=args.max_calls,
                max_wallets=args.max_wallets,min_call_s=args.min_call_s)
    validate_budget(budget,[cfg.MEMECOIN_SOLANA_RPC_URL,*getattr(cfg,'MEMECOIN_SOLANA_RPC_POOL_URLS',[])])
    if Path(args.db).resolve()==(root/'sniper_tracker.db').resolve():
        raise ValueError('research DB cannot be funded ledger')
    ledger=get_readonly_connection(str(root/'sniper_tracker.db'))
    try:
        proven=observation_owners(ledger,{r['uid'] for r in manifest.get('owners',[])})
        roster={r[0]:dict(paused=bool(r[1]),excluded=bool(r[2])) for r in ledger.execute(
            'SELECT source_user_id,paused,excluded FROM memecoin_wallets')}
        for row in manifest.get('owners',[]):
            row.update(roster.get(row['uid'],{}))
        owners=validate_manifest(manifest,proven)
        if len(owners)>args.max_wallets: raise ValueError('CLI owner cap exceeded')
    finally: ledger.close()
    rpc=build_read_rpc_call(args.research_rpc_url,min_call_s=args.min_call_s,max_429_retries=0)
    conn=open_observation_db(args.db)
    try:
        freeze_manifest(conn,manifest)
        conn.execute('CREATE TABLE IF NOT EXISTS budget_usage(day TEXT PRIMARY KEY,reads INTEGER,quotes INTEGER)')
        day=real_utc_now_iso()[:10]
        with conn: conn.execute('INSERT OR IGNORE INTO budget_usage VALUES (?,0,0)',(day,))
        def capped_rpc(method,params):
            with conn:
                used=conn.execute('SELECT reads FROM budget_usage WHERE day=?',(day,)).fetchone()[0]
                if used>=budget['max_calls_day']: raise ValueError('daily read budget exhausted')
                conn.execute('UPDATE budget_usage SET reads=reads+1 WHERE day=?',(day,))
            return rpc(method,params)
        print(json.dumps(collect_tick(conn,owners,capped_rpc,now_iso=real_utc_now_iso(),max_calls=args.max_calls)))
        if args.quote_db or args.quote_url:
            if not args.quote_db or not args.quote_url or not budget.get('quote_capacity'):
                raise ValueError('isolated quote capacity and explicit quote DB/URL required')
            from urllib.parse import urlsplit
            parsed=urlsplit(args.quote_url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.hostname in ('api.jup.ag','lite-api.jup.ag'):
                raise ValueError('shared funded Jupiter capacity is refused')
            if Path(args.quote_db).resolve() in (Path(args.db).resolve(),(root/'sniper_tracker.db').resolve()):
                raise ValueError('quote measurements require a separate research DB')
            from sniper.services import memecoin_signal_copy_quotes as cq
            import requests,time,os
            mdb=open_research_quote_db(args.quote_db)
            try:
                # Bounded forward window only; historical rows never become delayed entries.
                from datetime import timedelta
                from sniper.services.wallet_observation import _aware
                now_iso=real_utc_now_iso()
                floor=(_aware(now_iso)-timedelta(seconds=90)).isoformat()
                permitted={(o['uid'],o['address']) for o in owners}
                for row in conn.execute('SELECT * FROM observations WHERE state=? AND action=? AND source_at>=? '
                                        'ORDER BY source_at DESC LIMIT ?',('decoded','buy',floor,budget['max_active'])):
                    event=dict(zip([d[0] for d in conn.execute('SELECT * FROM observations LIMIT 0').description],row))
                    if (event['uid'],event['address']) not in permitted: continue
                    policies=manifest.get('policies') or ['control']
                    schedule_research_quotes(mdb,event,policies=policies,now_iso=real_utc_now_iso(),
                        budget=dict(remaining_calls=budget['max_quotes_day'],max_active=budget['max_active'],
                                    max_quotes_day=budget['max_quotes_day']))
                def quote(im,om,amount):
                    with conn:
                        used=conn.execute('SELECT quotes FROM budget_usage WHERE day=?',(day,)).fetchone()[0]
                        if used>=budget['max_quotes_day']: raise ValueError('daily quote budget exhausted')
                        conn.execute('UPDATE budget_usage SET quotes=quotes+1 WHERE day=?',(day,))
                    time.sleep(cq.MIN_QUOTE_INTERVAL_S)
                    headers={}
                    if os.getenv('WALLET_UNIVERSE_RESEARCH_QUOTE_KEY'):
                        headers['x-api-key']=os.environ['WALLET_UNIVERSE_RESEARCH_QUOTE_KEY']
                    response=requests.get(args.quote_url,params=dict(inputMint=im,outputMint=om,amount=amount,
                                          slippageBps=100),headers=headers,timeout=10)
                    response.raise_for_status()
                    return response.json()
                used=conn.execute('SELECT quotes FROM budget_usage WHERE day=?',(day,)).fetchone()[0]
                remaining=budget['max_quotes_day']-used
                if remaining>0:
                    print('Research quote reads:',run_research_quote_tick(mdb,quote,now_iso=real_utc_now_iso(),
                                                                         max_quotes=min(remaining,args.max_calls)))
            finally: mdb.close()
    finally: conn.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
