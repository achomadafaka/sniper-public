#!/usr/bin/env python3
"""Office process entry point. A crew-owned runtime factory supplies the reviewed tools and policy."""
import argparse
from dataclasses import asdict
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from sniper.office.supervisor import StaleGeneration
from sniper.office.runtime_steps import RuntimeRefused


def main(argv=None,*,runtime_factory=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-factory',default='sniper.office.runtime:build')
    parser.add_argument('--store',type=Path,default=Path('data/office/office.db'))
    parser.add_argument('--ledger',type=Path,default=Path('data/office/ledger.db'))
    parser.add_argument('--config',type=Path,default=Path('config/office/runtime.json'))
    parser.add_argument('--guard',type=Path,default=Path('data/office/guard.json'))
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-s',type=float,default=1.)
    args=parser.parse_args(argv)
    if not 0<args.poll_s<=60: parser.error('poll-s must be in (0,60]')
    if runtime_factory is None:
        module,sep,name=args.runtime_factory.partition(':')
        if not sep or not module.startswith('sniper.office.') or not name.isidentifier():
            parser.error('runtime factory must be crew-owned sniper.office module:function')
        try: runtime_factory=getattr(importlib.import_module(module),name)
        except (ImportError,AttributeError):
            print(json.dumps({'state':'refused','reason':'runtime_not_installed'}));return 2
    try:
        runtime=runtime_factory(store_path=args.store,ledger_path=args.ledger,
                                config_path=args.config,guard_path=args.guard)
    except RuntimeRefused as exc:
        print(json.dumps({'state':'paused','reason':str(exc)}),flush=True)
        return 75  # systemd wake timer retries readiness without Restart= churn
    supervisor=runtime['supervisor']
    deadline=runtime['deadline_s'];max_steps=runtime['max_steps']
    if deadline+args.poll_s>=supervisor.lease.ttl_s:
        raise ValueError('lease_must_cover_one_turn_deadline')
    stop=threading.Event()
    previous={}
    gen=None
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig]=signal.signal(sig,lambda *_:stop.set())
    try:
        process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
        gen=supervisor.lease.acquire(f'office-{os.getpid()}-{process_start}')
        supervisor.reconcile(gen=gen)
        if 'initialize' in runtime: runtime['initialize'](gen)
        while not stop.is_set():
            try:
                due=runtime['tasks']()
                supervisor.reconcile_turns(gen=gen)
            except ValueError as exc:
                # Unready host (guard, clock, caps, pins) pauses turns; never a crash loop.
                # Nothing is renewed or written while paused: an unknown clock cannot stamp a lease or heartbeat.
                print(json.dumps({'state':'paused','reason':str(exc)}),flush=True)
                if args.once: break
                stop.wait(args.poll_s);continue
            for task in due:
                if stop.is_set(): break
                supervisor.lease.renew(gen)
                result=supervisor.run_turn(task['agent_id'],gen=gen,deadline_s=deadline,
                                           max_steps=max_steps,turn_id=task['turn_id'])
                if 'flush_observations' in runtime:runtime['flush_observations']()
                print(json.dumps(asdict(result)),flush=True)
            if 'flush_observations' in runtime:runtime['flush_observations']()
            supervisor.lease.renew(gen)
            # Heartbeat writes are isolated Office state; never writes to the desk or funded ledger.
            from sniper.office.supervisor import _put
            with supervisor.lease.guard(gen):
                _put(supervisor.conn,'heartbeats','supervisor',
                     {'at':supervisor.clock(),'generation':gen,'frontiers':supervisor.progress_frontiers(),
                      'observations':runtime['observation_health']() if 'observation_health' in runtime else None})
            if args.once: break
            stop.wait(args.poll_s)
        return 0
    except StaleGeneration:
        print(json.dumps({'state':'refused','reason':'stale_supervisor_lease'}),flush=True)
        return 3
    finally:
        if gen is not None:
            try: supervisor.lease.release(gen)
            except (StaleGeneration,ValueError): pass  # unknown clock: the lease expires by itself
        for sig,handler in previous.items(): signal.signal(sig,handler)
        if 'close' in runtime:runtime['close']()


if __name__=='__main__': raise SystemExit(main())
