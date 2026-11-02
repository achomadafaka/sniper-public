#!/usr/bin/env python3
"""Robust copy-target selection + forward-holdout control.

Ranks wallets by their OWN forward-follow record (the only key that carries signal — trading
stats don't), and proposes promote/cull actions for the copy targets. Also sets the forward
holdout freeze so post-hoc winners get judged out-of-sample.

    python3 scripts/hl_copy_select.py                 # propose (dry-run) from the follow ledger
    python3 scripts/hl_copy_select.py --apply         # apply promote/cull to hl_copy_targets
    python3 scripts/hl_copy_select.py --freeze        # stamp the forward-holdout boundary = now
    python3 scripts/hl_copy_select.py --source copy   # rank from the copy book instead of follows
Read-only unless --apply / --freeze. Paper only."""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import copy_selection as SEL  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.hyperlive.trader_intel import PHASE2_MIN_CLOSES  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def wallet_pnls(conn, *, source: str = "follow") -> dict:
    """{wallet: [net_bps, ...]} time-ordered. source 'follow' = honest counterfactual ledger
    (time_cap forces closes); 'copy' = the running mirror book."""
    if source == "copy":
        q = ("SELECT target AS wallet, pnl_bps AS v FROM hl_copy_book "
             "WHERE status!='open' AND pnl_bps IS NOT NULL ORDER BY detect_ts_ms")
    else:
        q = ("SELECT wallet, net_bps AS v FROM hl_follow_positions "
             "WHERE status!='open' AND net_bps IS NOT NULL ORDER BY detect_ts_ms")
    out: dict = {}
    for r in conn.execute(q):
        out.setdefault(r["wallet"], []).append(r["v"])
    return out


def build_proposals(wp: dict, target_wallets: set, **classify_kw):
    """-> (ranked, proposals). proposals = actionable changes for wallets that ARE copy targets."""
    ranked = SEL.rank(wp, min_n=5, **classify_kw)
    proposals = [{"wallet": r["wallet"], "action": r["action"], "median": r["median"], "n": r["n"]}
                 for r in ranked if r["wallet"] in target_wallets and r["action"] in ("promote", "cull")]
    return ranked, proposals


def apply_proposals(conn, proposals, *, ts_ms: int) -> None:
    for p in proposals:
        if p["action"] == "promote":
            conn.execute("UPDATE hl_copy_targets SET copy_tier='proven', updated_ts_ms=? WHERE wallet=?",
                         (ts_ms, p["wallet"]))
        elif p["action"] == "cull":
            conn.execute("UPDATE hl_copy_targets SET active=0, updated_ts_ms=? WHERE wallet=?",
                         (ts_ms, p["wallet"]))
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Robust copy-target selection + holdout freeze")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--source", choices=("follow", "copy"), default="follow")
    ap.add_argument("--apply", action="store_true", help="apply promote/cull to hl_copy_targets")
    ap.add_argument("--freeze", action="store_true", help="stamp forward-holdout boundary = now")
    ap.add_argument("--now-ms", type=int, default=None, help="override 'now' (testing)")
    args = ap.parse_args(argv)
    now_ms = args.now_ms if args.now_ms is not None else int(time.time() * 1000)

    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)

    if args.freeze:
        IS.set_holdout_freeze(conn, now_ms, ts_ms=now_ms)
        print(f"forward-holdout freeze set at {now_ms} — all copies opened from now are is_holdout=1")
        return 0

    targets = {t["wallet"] for t in IS.get_copy_targets(conn, active_only=False)}
    wp = wallet_pnls(conn, source=args.source)
    ranked, proposals = build_proposals(wp, targets)

    n_fdr = sum(1 for r in ranked if r.get("fdr_significant"))
    print(f"═══ COPY SELECTION (source={args.source}, {len(ranked)} wallets n>=5) ═══")
    print(f"  {'wallet':<14}{'n':>4}{'median':>9}{'mean':>9}{'win%':>6}{'stable':>7}{'p>0':>7}{'FDR':>5}{'action':>9}")
    for r in ranked:
        p = r.get("p_positive")
        print(f"  {str(r['wallet'])[:12]:<14}{r['n']:>4}{r['median']:>9.1f}{r['mean']:>9.1f}"
              f"{100 * r['win_rate']:>5.0f}%{str(r['sign_stable']):>7}"
              f"{('%.3f' % p) if p is not None else '  -':>7}{('✓' if r.get('fdr_significant') else '·'):>5}"
              f"{r['action']:>9}")
    print(f"  --> {n_fdr}/{len(ranked)} survive Benjamini-Hochberg FDR (q=0.10). "
          f"{'promote leads are multiple-comparison artifacts until this is >0.' if n_fdr == 0 else ''}")
    culls = [p for p in proposals if p["action"] == "cull"]
    promotes = [p for p in proposals if p["action"] == "promote"]
    # Promotion is owned by the phase-2 gate (copyability_phase2: n>=30, t>=2, concentration<40%).
    # Force-setting copy_tier='proven' before that bar strands a wallet in limbo — not
    # fallback-eligible, not proven-eligible — so it would STOP being copied. Hence: apply culls
    # only; promotes are informational leads that the phase-2 gate auto-promotes once mature.
    print(f"\nCULL candidates (applied with --apply): {len(culls)}")
    for p in culls:
        print(f"  CULL   {p['wallet']}  (follow median={p['median']:.1f}bps, n={p['n']})")
    print(f"phase-2 PROMOTE leads (auto-promoted by the gate at n>={PHASE2_MIN_CLOSES}/t>=2, "
          f"NOT forced here): {len(promotes)}")
    for p in promotes:
        print(f"  LEAD   {p['wallet']}  (follow median={p['median']:.1f}bps, {p['n']}/{PHASE2_MIN_CLOSES} closes to proven)")

    if args.apply and culls:
        apply_proposals(conn, culls, ts_ms=now_ms)
        print(f"\nAPPLIED {len(culls)} culls (deactivated) to hl_copy_targets.")
    elif culls:
        print("\n(dry-run — rerun with --apply to deactivate the culls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
