#!/usr/bin/env python3
"""The Solana cohort stream (spec 2026-10-09 Solana pre-signal feed, step 1). Its own process.

DEFAULT OFF: exits 0 while MEMECOIN_SOL_COHORT_HELIUS_URL is unset, keyless, or the same key as any
other configured URL. Measurement only: it writes data/memecoin_sol_cohort.db and only READS the
funded ledger. See sniper/services/memecoin_sol_cohort.py.
"""
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.util.envfiles import load_env_files  # noqa: E402
from sniper.util.state_paths import state_root  # noqa: E402


def main():
    load_env_files(str(state_root()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from sniper.services.memecoin_sol_cohort import run
    return run(os.environ)


if __name__ == "__main__":
    sys.exit(main())
