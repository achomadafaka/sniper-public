#!/usr/bin/env python3
"""Local-only execution research archive/coverage. Pinned code; separate state.

--ingest accepts versioned raw episodes as discovery inputs. It never publishes
an edge or arms a wallet. Provider requests are absent. Active percoin swap state
is archived locally. --seal-dossier-baseline accepts a reviewed JSON snapshot
with canonical chain, defaults by gate family, required_gates, and a whole-position
replay policy. It does not fetch defaults or make the collector produce receipts.
--ingest-admission imports a producer's complete or failed v2 evidence receipt
for an already archived origin. Structural checks do not authenticate provenance.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from sniper.util.state_paths import state_root
from sniper.util.clock import real_utc_now_iso
from sniper.persistence import db_wallet_execution as store
from sniper.services.wallet_execution_research import collect_once,build_coverage
from sniper.research.wallet_execution.replay import replay


def default_tape_path():
    from sniper.persistence.db_ticks import ticks_db_path
    return Path(ticks_db_path())


def default_volume_path():
    return state_root() / 'data/memecoin_onchain_volume.db'


def main():
    state=state_root(); p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=state/'sniper_tracker.db')
    p.add_argument('--tape',type=Path,default=default_tape_path())
    p.add_argument('--volume-tape',type=Path,default=default_volume_path())
    p.add_argument('--db',type=Path,default=state/'data/wallet_execution_research.db')
    p.add_argument('--artifact',type=Path,default=state/'data/wallet_execution_coverage.json')
    p.add_argument('--loop',action='store_true'); p.add_argument('--period',type=float,default=15.)
    action=p.add_mutually_exclusive_group()
    action.add_argument('--seal-dossier-baseline',type=Path,help='Seal a reviewed chain-specific default snapshot; no funded writes')
    action.add_argument('--ingest-admission',type=Path,help='Import an immutable v2 admission receipt into owned research state')
    action.add_argument('--seal-round',type=Path); action.add_argument('--evaluate-round')
    p.add_argument('--release-sha',default='checkout')
    action.add_argument('--ingest',type=Path); p.add_argument('--policy',type=Path)
    args=p.parse_args()
    if args.seal_dossier_baseline or args.ingest_admission:
        from contextlib import closing
        # Independent immutable receipt; own SQLite transaction also serializes UI notes.
        with closing(store.open_store(args.db)) as db:
            with db:
                if args.ingest_admission:
                    gate=store.record_dossier_admission(db,json.loads(args.ingest_admission.read_text()),now=real_utc_now_iso())
                    result={**gate.as_dict(),'validation':'structural_only','funded_authority':False}
                else:
                    key=store.freeze_dossier_baseline(db,json.loads(args.seal_dossier_baseline.read_text()),now=real_utc_now_iso())
                    result={'baseline_id':key,'funded_authority':False}
        print(json.dumps(result),flush=True); return 2 if args.ingest_admission and not gate.eligible else 0
    args.db.parent.mkdir(parents=True,exist_ok=True)
    with (args.db.parent/'wallet_execution_research.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return 0
        with store.open_store(args.db) as db:
            if args.seal_round or args.evaluate_round:
                from datetime import datetime
                from sniper.research.wallet_execution.study import seal_round,evaluate_round
                now=datetime.fromisoformat(real_utc_now_iso()).timestamp()
                result=seal_round(db,json.loads(args.seal_round.read_text()),now=now) if args.seal_round else evaluate_round(db,args.evaluate_round,now=now)
                print(json.dumps(result),flush=True); return 0
            if args.ingest:
                episode=json.loads(args.ingest.read_text()); gate=store.ingest_episode(db,episode)
                if args.policy:
                    result=replay(episode,json.loads(args.policy.read_text())); store.record_result(db,result)
                    print(json.dumps(result),flush=True)
                else: print(json.dumps(gate.as_dict()),flush=True)
                return 0 if gate.eligible else 2
            while True:
                now=real_utc_now_iso(); report=collect_once(db,args.source,now=now,tape_path=args.tape,volume_path=args.volume_tape)
                coverage=build_coverage(db,now=now)
                coverage.update(worker_pid=os.getpid(),release_sha=args.release_sha)
                args.artifact.parent.mkdir(parents=True,exist_ok=True)
                tmp=args.artifact.with_suffix('.tmp')
                tmp.write_text(store.encoded(coverage)); os.replace(tmp,args.artifact)
                print(json.dumps(report),flush=True)
                if not args.loop: return 0 if report['source_status']=='ok' else 2
                time.sleep(max(5.,args.period))

if __name__=='__main__': raise SystemExit(main())
