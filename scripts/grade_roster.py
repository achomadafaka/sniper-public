#!/usr/bin/env python3
"""Scheduled PROFILER for the Live Wallet Signals roster (the explainable filter — NOT a grader/score).
For each rostered wallet it composes the archetype classification (heavy/live — this is the SLOW
scheduled path) + the stored thesis profile + the wallet's position-level Q75 into an archetype-led
signal PROFILE and upserts it into `wallet_signal_profiles`. Gated: only wallets that went through the
deep analyzer get a profile (others are pruned). The live board reads those profiles instantly.
Read-only on analyzer verdicts; no insider_score. Cron: e.g. `30 5 * * * python3 scripts/grade_roster.py`."""
from __future__ import annotations

import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.analysis.wallet_grade.grade import build_profile   # noqa: E402
from sniper.analysis.wallet_stakes import position_stakes, q75_index  # noqa: E402

ANATOMY = os.path.join(ROOT, "runs", "wallet_anatomy")


def _q75(activity: list) -> float:
    """75th-percentile of the wallet's ALL-MARKET position stakes (shared nearest-rank index). Was a
    sports-only, off-by-one index that returned the MAX for small books and 0 for non-sports wallets."""
    s = position_stakes(activity or [])
    return s[q75_index(len(s))] if s else 0.0


def build_grade(wallet: str, *, thesis: dict, archetype_report: dict, activity: list,
                analyzed_ts: int = 0) -> dict:
    """Pure composition: Q75 from activity + archetype-led build_profile(). Returns a wallet_signal_profiles row."""
    g = build_profile(thesis or {}, archetype_report or {}, q75_usdc=_q75(activity))
    g["wallet"] = wallet.lower()
    g["analyzed_ts"] = int(analyzed_ts or 0)
    return g


def _stored_thesis_wallets() -> dict:
    """wallet -> stored thesis profile dict (the analyzed roster)."""
    from sniper.analysis.winning_wallet_thesis.store import STORE_DIR
    out = {}
    for path in glob.glob(os.path.join(STORE_DIR, "*.json")):
        try:
            p = json.load(open(path))
        except Exception:  # noqa: BLE001
            continue
        w = (p.get("wallet") or "").lower()
        if w:
            out[w] = p
    return out


def _cached_activity(wallet: str) -> list:
    ap = os.path.join(ANATOMY, f"{wallet.lower()}_activity.json")
    if os.path.exists(ap):
        try:
            return json.load(open(ap))
        except Exception:  # noqa: BLE001
            return []
    return []


def main(argv=None) -> int:
    import db
    from scripts.wallet_archetype_extractor import extract_wallet_archetype
    requested = [w.lower() for w in (argv or sys.argv[1:])]
    thesis_by = _stored_thesis_wallets()
    if not requested:
        requested = sorted(thesis_by.keys())        # default roster = analyzed wallets
    # GATE: a grade must be backed by a deep analyzer thesis profile. A wallet that hasn't been
    # through winning_wallet_thesis is NOT graded — no scores from a bypass path. Run the analyzer
    # first: `python3 -m sniper.analysis.winning_wallet_thesis.cli <wallet>`.
    wallets = [w for w in requested if w in thesis_by]
    skipped = [w for w in requested if w not in thesis_by]
    if skipped:
        print(f"SKIPPED {len(skipped)} wallet(s) with NO analyzer thesis profile — grade requires the "
              f"deep analyzer first (not a bypass archetype-only score). e.g. {skipped[:3]}", file=sys.stderr)
    conn = db.get_connection()
    from sniper.persistence.db_wallet_signals import prune_profiles, upsert_profile
    pruned = prune_profiles(conn, thesis_by.keys())
    if pruned:
        print(f"pruned {pruned} stale profile(s) without analyzer thesis profile", file=sys.stderr)
    graded = 0
    for w in wallets:
        try:
            arch = extract_wallet_archetype(w)        # live (scheduled path)
        except Exception as e:  # noqa: BLE001
            print(f"  [{w[:10]}] archetype fetch failed: {type(e).__name__}", file=sys.stderr)
            continue
        thesis = thesis_by.get(w, {})
        g = build_grade(w, thesis=thesis, archetype_report=arch, activity=_cached_activity(w),
                        analyzed_ts=int(thesis.get("run_ts") or 0) if isinstance(thesis, dict) else 0)
        upsert_profile(conn, g, now_ts=int(time.time()))
        graded += 1
        print(f"  [{w[:10]}] {g['archetype']} ({g['archetype_confidence']}) "
              f"directional={g['directional_profile']} edge_read={g['in_sample_edge_read']}", file=sys.stderr)
    print(f"profiled {graded}/{len(wallets)} wallets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
