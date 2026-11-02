#!/usr/bin/env python3
"""Funded large-buyer pilot worker: own process, off every copy lane. Does nothing unless MEMECOIN_LARGE_BUYER_ENABLED=1."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.services.memecoin_large_buyer import run  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", type=Path, required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--desk-dir", type=Path, required=True)
    ap.add_argument("--metadata", type=Path, help="Existing off-path verified PumpSwap mint cache; no inline RPC")
    args = ap.parse_args()
    raise SystemExit(run(arm_path=args.arm, ledger_path=args.ledger, desk_dir=args.desk_dir, metadata_path=args.metadata, log=lambda message: print(message, flush=True)))
