#!/usr/bin/env python3
"""Finite, default-off Office research wallet collector. No funded/Office service changes."""
import argparse
import fcntl
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--enabled',action='store_true')
    ap.add_argument('--state-root',default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--research-env-file',help='Existing approved research credentials; never printed')
    ap.add_argument('--max-calls',type=int,default=5)
    ap.add_argument('--metadata-repair-slots',type=int,default=0,help='Explicit finite research canary: at most 10 old missing slots, 0 disables')
    ap.add_argument('--ticks',type=int,default=1)
    ap.add_argument('--interval',type=float,default=2)
    args=ap.parse_args(argv)
    if not args.enabled:
        print('Disabled; no credential reads, provider calls or database writes.')
        return 0
    import math
    if not args.research_env_file or not 1<=args.max_calls<=100 or not 0<=args.metadata_repair_slots<=10 or args.ticks<0 or not math.isfinite(args.interval) or args.interval<=0:
        ap.error('enabled needs --research-env-file, max-calls 1..100, ticks >=0 (0 runs continuously), interval >0')
    from sniper.office.chain_data import ChainData,research_url_of
    from sniper.office.wallet_network import WalletNetwork
    from sniper.office.wallet_watch_collect import Collector
    from sniper.office.wallet_family import materialize_candidates
    from sniper.office.wallet_campaign import materialize_campaigns
    from sniper.office.wallet_group_leads import scan_group_leads
    from sniper.office.wallet_link_backfill import expand_existing_links
    from sniper.office import wallet_watch
    from sniper.office.wallet_metadata_jobs import poll as metadata_poll
    from sniper.util.atomic import atomic_write_json
    root=Path(args.state_root).resolve(); office=root/'data/office'; desk=root/'data/desk'
    # Refuse an accidental new state tree; activation must point at existing Office state.
    if not (office/'wallet_network.db').is_file() or not (office/'v2/chain.db').is_file():
        ap.error('existing Office wallet_network.db and v2/chain.db required')
    with (office/'wallet_watch_collect.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps(dict(state='unknown',reason='collector_already_running')));return 1
        chain=ChainData(office/'v2/chain.db',research_url_of(args.research_env_file),page=None)
        net=WalletNetwork(office/'wallet_network.db',chain_db=chain.path,chain=chain,busy_timeout=0)
        collector=Collector(net,desk)
        def remote_poll():
            # Descriptors only cross threads; every task constructs its own readers/connections.
            remote_chain=ChainData(office/'v2/chain.db',research_url_of(args.research_env_file),page=None)
            remote_net=WalletNetwork(office/'wallet_network.db',chain_db=remote_chain.path,chain=remote_chain)
            regular,jobs,metadata=metadata_poll(remote_chain,Collector(remote_net,desk),office/'v2/receipt_jobs.db',office/'v2/metadata_jobs.db',desk/'pumpswap/tape.db',slots=args.metadata_repair_slots,max_calls=args.max_calls)
            return regular,time.time(),jobs,metadata
        def enrich():
            background_net=WalletNetwork(office/'wallet_network.db',chain_db=chain.path,busy_timeout=.1)
            links=expand_existing_links(background_net,desk,time.time())
            groups=scan_group_leads(background_net,time.time(),limit=1000)
            summary={k:v for k,v in groups.items() if k!='items'}
            summary['changed']=groups.get('inserted',groups.get('changed',0))
            return links,summary,time.time()
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='wallet-research') as pool, chain.stream_store_lifetime():
            future=None
            enrichment=None;enrichment_at=None
            links=dict(state='unknown',reason='not_scanned',added=0);groups=dict(state='unknown',reason='not_scanned',changed=0)
            result=dict(state='unknown',reason='not_polled',wallets=[])
            result_at=None
            receipt_jobs=dict(state='unknown',reason='not_polled',pending=None,calls_attempted=0);receipt_job_asof=None
            metadata=dict(state='known',reason='metadata_recovery_disabled' if args.metadata_repair_slots==0 else 'not_polled',calls_attempted=0);metadata_asof=None
            tick=0;ingest_turns=9
            while args.ticks==0 or tick<args.ticks:
                started=time.monotonic()
                if enrichment is not None:
                    if not enrichment.done():
                        # Keep the last actual results and their time; a scheduler heartbeat is not new evidence.
                        status=dict(status,ts=time.time(),local_scheduler=dict(state='deferred',reason='enrichment_writer_active'),
                                    enrichment_in_flight=True,poll_in_flight=future is not None and not future.done())
                        atomic_write_json(office/'wallet_watch_collect.heartbeat',status)
                        print(json.dumps(dict(ts=status['ts'],local_scheduler=status['local_scheduler'],
                                              local_result_at=status['local_result_at'],remote_writer_contention='possible')),flush=True)
                        time.sleep(args.interval)
                        continue
                    try:links,groups,enrichment_at=enrichment.result()
                    except Exception as exc:
                        links=dict(state='unknown',reason='enrichment_failed',error_type=type(exc).__name__,added=0)
                        groups=dict(state='unknown',reason='enrichment_failed',changed=0)
                        enrichment_at=time.time()
                    enrichment=None
                stages={};stage=time.monotonic()
                flows=collector.consume_flows(limit=499)
                stages['flows']=time.monotonic()-stage;stage=time.monotonic()
                setup=collector.consume_setups()
                stages['setups']=time.monotonic()-stage;stage=time.monotonic()
                if args.ticks==1:
                    links,groups,enrichment_at=enrich()
                stages['enrichment_inline']=time.monotonic()-stage;stage=time.monotonic()
                export_conn=None
                try:
                    export=flows.get('watch_export',{})
                    if args.ticks==1 or export.get('state')!='known':
                        export_conn=net._conn()
                        exported_at=time.time()
                        wallet_watch._sync(export_conn,desk,exported_at)
                        export=dict(state='known',asof=exported_at)
                except (sqlite3.Error,OSError,ValueError,TypeError):
                    export=dict(state='unknown',reason='watch_export_unavailable')
                finally:
                    if export_conn is not None:export_conn.close()
                stages['export']=time.monotonic()-stage;stage=time.monotonic()
                campaign=materialize_campaigns(net,desk,time.time())
                campaign_status=dict(state=campaign['state'],reason=campaign.get('reason'),changed=campaign.get('changed',0))
                stages['campaigns']=time.monotonic()-stage;stage=time.monotonic()
                family=materialize_candidates(net,desk,time.time())
                family_status=dict(state=family['state'],reason=family.get('reason'),candidates=len(family.get('items',[])),
                                   early=sum(r.get('early') is True for r in family.get('items',[])),truncated=family.get('truncated',False))
                stages['families']=time.monotonic()-stage
                if args.ticks==1:
                    new_result,new_jobs,metadata=metadata_poll(chain,collector,office/'v2/receipt_jobs.db',office/'v2/metadata_jobs.db',desk/'pumpswap/tape.db',slots=args.metadata_repair_slots,max_calls=args.max_calls)
                    metadata_asof=time.time()
                    if new_jobs is not None:receipt_jobs=new_jobs;receipt_job_asof=metadata_asof
                    if new_result is not None:result,result_at=new_result,receipt_job_asof
                else:
                    if future is not None and future.done():
                        try:
                            new_result,completed_at,new_jobs,metadata=future.result()
                            metadata_asof=completed_at
                            if new_jobs is not None:receipt_jobs=new_jobs;receipt_job_asof=completed_at
                            if new_result is not None:result,result_at=new_result,completed_at
                        except Exception as exc:
                            result=dict(state='unknown',reason='poll_failed',error_type=type(exc).__name__,wallets=[])
                            result_at=time.time()
                        future=None
                    if future is None:
                        future=pool.submit(remote_poll)
                ingest_turns+=1
                if args.ticks!=1 and ingest_turns>=10:
                    enrichment=pool.submit(enrich);ingest_turns=0
                status=dict(ts=time.time(),local_result_at=time.time(),local_scheduler=dict(state='ran',ingest_turns=ingest_turns,enrichment_due_in=10-ingest_turns),remote_writer_contention='possible',pid=os.getpid(),poll=result,poll_result_at=result_at,receipt_jobs=receipt_jobs,receipt_job_asof=receipt_job_asof,metadata_recovery=metadata,metadata_recovery_asof=metadata_asof,
                            poll_in_flight=future is not None and not future.done(),flows=flows,setups=setup,watch_export=export,campaigns=campaign_status,links=links,groups=groups,enrichment_result_at=enrichment_at,enrichment_in_flight=enrichment is not None and not enrichment.done(),families=family_status,local_stage_s=stages,local_elapsed_s=time.monotonic()-started)
                atomic_write_json(office/'wallet_watch_collect.heartbeat',status)
                if args.ticks!=1:
                    summary={k:v for k,v in result.items() if k not in ('wallets','watchset')}
                    print(json.dumps(dict(ts=status['ts'],poll=summary,poll_result_at=result_at,receipt_jobs=receipt_jobs,receipt_job_asof=receipt_job_asof,metadata_recovery=metadata,metadata_recovery_asof=metadata_asof,
                                          poll_in_flight=status['poll_in_flight'],flows=flows,setups=setup,watch_export=export,campaigns=campaign_status,links=links,groups=groups,enrichment_result_at=enrichment_at,families=family_status,local_stage_s=stages,local_elapsed_s=status['local_elapsed_s'])),flush=True)
                else:
                    print(json.dumps(status),flush=True)
                tick+=1
                if args.ticks==0 or tick<args.ticks:time.sleep(args.interval)
    return 0


if __name__=='__main__':raise SystemExit(main())
