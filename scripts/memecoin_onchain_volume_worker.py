#!/usr/bin/env python3
"""Run the separate volume observer for one enabled chain.

PER CHAIN SINCE 2026-09-22. The worker used to hard-code 4663: one chain, one lock file, one URL
env var. Base (8453) carries its memecoin liquidity on the same Uniswap V4 PoolManager events, so
the observer generalises by naming rather than by new logic.

Pass the chain id as the first argument. Without one it runs 4663, so an existing crontab line and
watchdog keep working unchanged. Each chain takes its OWN lock, so two chains run as two processes
and neither blocks the other.
"""
import fcntl
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import state_root, configure_memecoin_state_env


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # THE FIRST ARGUMENT IS A CHAIN ID OR NOTHING. A bare `main()` under a test harness sees the
    # harness's own argv (the test path, '-q', ...) and read the test path as a chain, so the worker
    # returned 0 without running (tests/test_memecoin_volume_writer.py, red since the per-chain
    # change). Only a digit string names a chain; anything else keeps the 4663 default.
    first = argv[0].strip() if argv else ''
    chain = first if first.isdigit() else '4663'
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    os.environ["DB_PATH"] = str((state_root() / os.environ["DB_PATH"]).resolve())
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_volume_writer import run, manager_for
    if chain not in cfg.MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS:
        return 0
    manager_for(chain)        # refuse a chain with no verified PoolManager, before any connection
    url=os.getenv('MEMECOIN_ONCHAIN_M5_WS_URL_%s' % chain,'').strip()
    if not url:
        raise SystemExit('volume observer blocked: dedicated WS URL absent for chain %s' % chain)
    # PER-CHAIN LOCK. One shared lock let the second chain exit silently as a duplicate.
    #
    # 4663 KEEPS THE LEGACY PATH. The running observer holds
    # `memecoin_onchain_volume.worker.lock`, and a rolling deploy that renamed it would take a
    # DIFFERENT lock and run a second 4663 observer beside the live one.
    lock_path=state_root()/('data/memecoin_onchain_volume.worker.lock' if chain == '4663'
                            else 'data/memecoin_onchain_volume.worker.%s.lock' % chain)
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        run(chain,url)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
