from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from sniper.analysis.winning_wallet_thesis.batch import InMemoryProfileStore, run_wallet_batch
from sniper.analysis.winning_wallet_thesis.intake import load_curated_candidates


def _dry_run_profile(wallet: str) -> dict:
    return {"wallet": wallet, "headline": "dry_run_placeholder"}


def _real_profile(wallet: str) -> dict:
    from sniper.analysis.winning_wallet_thesis.cli import run_extractor
    return asdict(run_extractor(wallet))


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch winning-wallet thesis extractor (dry-run default).")
    parser.add_argument("--candidates", required=True, help="JSON list of curated active winners")
    parser.add_argument("--max-wallets", type=int, default=None)
    parser.add_argument("--real", action="store_true",
                        help="run the real extractor (cached fetches); default is dry-run placeholder")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="(default) do not run the real extractor")
    args = parser.parse_args()
    candidates = load_curated_candidates(args.candidates)
    store = InMemoryProfileStore()
    run_profile = _real_profile if args.real else _dry_run_profile
    counts = run_wallet_batch(candidates, run_profile=run_profile, store=store,
                              max_wallets=args.max_wallets)
    print(json.dumps(counts, sort_keys=True))
