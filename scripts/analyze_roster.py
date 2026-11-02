#!/usr/bin/env python3
"""Deep-analyze a roster of wallets through winning_wallet_thesis, persisting a thesis profile for
each. This is the REQUIRED first step before grading: a Live Wallet Signals grade must be backed by
a deep analyzer run (grade_roster gates on a persisted profile — no bypass/archetype-only scores).

Run this on discovered candidates, THEN grade_roster, THEN poll_live_positions. Per-wallet errors are
isolated (one bad wallet doesn't abort the batch). Read-only on analyzer verdicts; writes profiles to
runs/wallet_thesis_profiles/. Usage: python3 scripts/analyze_roster.py 0xWALLET1 0xWALLET2 ...
"""
from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.analysis.winning_wallet_thesis.cli import run_extractor          # noqa: E402
from sniper.analysis.winning_wallet_thesis.store import persist_profile, STORE_DIR  # noqa: E402


def _already_analyzed() -> set:
    import glob, json
    out = set()
    for p in glob.glob(os.path.join(STORE_DIR, "*.json")):
        try:
            out.add((json.load(open(p)).get("wallet") or "").lower())
        except Exception:  # noqa: BLE001
            pass
    return out


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    wallets = [w.lower() for w in args]
    if not wallets:
        print("usage: analyze_roster.py 0xWALLET ...", file=sys.stderr)
        return 2
    have = _already_analyzed()
    todo = [w for w in wallets if w not in have]
    print(f"deep-analyzing {len(todo)}/{len(wallets)} ({len(wallets) - len(todo)} already have profiles)",
          file=sys.stderr)
    done = 0
    for w in todo:
        try:
            profile = run_extractor(w, refresh=False, persist_store=True)
            persist_profile(profile)
            done += 1
            print(f"  [{w[:10]}] analyzed + persisted", file=sys.stderr)
        except Exception:  # noqa: BLE001 — isolate one wallet's failure
            print(f"  [{w[:10]}] analysis FAILED:", file=sys.stderr)
            traceback.print_exc()
    print(f"analyzed {done}/{len(todo)} new wallets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
