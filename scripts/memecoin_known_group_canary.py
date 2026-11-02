#!/usr/bin/env python3
"""Default-OFF local known-group producer. Only the operator may run --confirm."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enabled',action='store_true',help='run the data-only local observer')
    parser.add_argument('--show-policy',action='store_true')
    parser.add_argument('--register',action='store_true',help='register the exact policy with admission OFF')
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument('--enable-admission',action='store_true')
    authority.add_argument('--stop-admission',action='store_true')
    parser.add_argument('--confirm',action='store_true',help='operator-only authority for the exact --policy-hash')
    parser.add_argument('--policy-hash')
    parser.add_argument('--funded-db')
    parser.add_argument('--capture-db')
    parser.add_argument('--desk-dir')
    parser.add_argument('--watch-db')
    parser.add_argument('--ticks',type=int,default=1,help='0 runs continuously')
    args = parser.parse_args(argv)
    if not any((args.enabled,args.show_policy,args.register,args.enable_admission,args.stop_admission)):
        print('Disabled; no credential reads, provider calls or ledger writes.')
        return 0
    from sniper.services import memecoin_known_group_canary as policy
    if args.show_policy:
        print(json.dumps(dict(policy=policy.POLICY,policy_hash=policy.POLICY_HASH,admission_default=False),sort_keys=True))
        return 0
    if not args.funded_db:
        parser.error('--funded-db must name the existing funded ledger')
    if (args.enable_admission or args.stop_admission) and (not args.confirm or args.policy_hash != policy.POLICY_HASH):
        parser.error('only the operator may authorize --confirm with the exact --policy-hash')
    if args.enabled and (args.ticks < 0 or not all((args.capture_db,args.desk_dir,args.watch_db))):
        parser.error('--enabled requires --capture-db, --desk-dir, --watch-db and --ticks >=0')
    from sniper.persistence import db_memecoin as db
    from sniper.util.clock import real_utc_now_iso
    conn = db.connect_known_group_ledger(args.funded_db)
    try:
        if args.register:
            policy.register(conn,now_iso=real_utc_now_iso())
        if args.enable_admission or args.stop_admission:
            policy.set_admission(conn,enabled=args.enable_admission,confirm=args.confirm,
                                 policy_hash=args.policy_hash,now_iso=real_utc_now_iso())
        if not args.enabled:
            print(json.dumps(dict(cohort=policy.COHORT_ID,policy_hash=policy.POLICY_HASH,
                admission_refusal=db._known_group_policy_refusal(conn))))
            return 0
        from sniper.services.memecoin_known_group_tail import KnownGroupTail
        from sniper.office.wallet_watch import expand_known_group_watch
        from sniper.settings.known_group import KNOWN_GROUP_TAIL_INTERVAL_S
        from sniper.util.atomic import atomic_write_json
        desk = Path(args.desk_dir).resolve()
        capture = Path(args.capture_db).resolve()
        if not capture.is_file() or not Path(args.watch_db).is_file() or not (desk/'universe.heartbeat').is_file():
            parser.error('existing capture, research watch and heartbeat files are required')
        with (desk/'known-group-tail.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error('known-group observer already running')
            worker = KnownGroupTail(conn,capture_path=capture,desk_dir=desk,watch_path=args.watch_db)
            # Research exports may wait on their own writer; they never block source-sell processing.
            with ThreadPoolExecutor(max_workers=1,thread_name_prefix='known-group-watch') as pool:
                future = None
                turns = 0
                watch_at = 0.0
                watch_result = None
                while args.ticks == 0 or turns < args.ticks:
                    started = time.monotonic()
                    status = worker.tick()
                    now = time.time()
                    if future is not None and future.done():
                        try:
                            watch_result = future.result()
                        except Exception:
                            logging.exception('known-group research watch expansion failed')
                            watch_result = dict(state='unknown',reason='watch_expansion_failed')
                        future = None
                    if future is None and now-watch_at >= policy.POLICY['sell_freshness_s']:
                        tail = db.known_group_tail_state(worker.observer,str(capture))
                        state = json.loads(tail['state_json']) if tail else {}
                        try:
                            services = policy._known_services()
                            ready = set(policy.APPROVED_ROOTS)
                            ready.update(owner for owner,path in state.get('funding',{}).items()
                                         if policy._service_path_allowed(path,services))
                            for candidate in state.get('candidates',{}).values():
                                path = (candidate['prepared']['funding_path'] if candidate.get('prepared')
                                        else state.get('funding',{}).get(candidate['fact']['developer']))
                                if not policy._service_path_allowed(path,services):
                                    continue
                                ready.add(candidate['fact']['developer'])
                                ready.update(member['address'] for member in candidate.get('genesis',[]))
                            addresses = sorted(ready-services['addresses'])
                            future = pool.submit(expand_known_group_watch,args.watch_db,desk,addresses,now_host=now)
                        except (OSError,ValueError):
                            logging.exception('known-group service catalogue prevents research expansion')
                            watch_result = dict(state='unknown',reason='known_group_service_catalogue_unknown')
                        watch_at = now
                    status.update(ts=now,policy_hash=policy.POLICY_HASH,watch_result=watch_result,
                                  tick_ms=(time.monotonic()-started)*1000)
                    atomic_write_json(desk/'known-group-tail.heartbeat',status)
                    print(json.dumps(status,sort_keys=True),flush=True)
                    turns += 1
                    if args.ticks == 0 or turns < args.ticks:
                        time.sleep(max(0.0,KNOWN_GROUP_TAIL_INTERVAL_S-(time.monotonic()-started)))
        return 0
    finally:
        if 'worker' in locals():
            worker.close()
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
