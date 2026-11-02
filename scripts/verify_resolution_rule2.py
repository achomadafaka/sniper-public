"""§11.5 — empirical verification of §7.5 rule 2 (curPrice resolution).

Per spec §7.5 + §16 step 5, rule 2 ships disabled by default. Before
flipping the flag we must verify on real v4 fixtures that curPrice
reflects ACTUAL resolution and not a stale mark.

This script:
  1. Loads every fixture under tests/calibration/fixtures-v4-staging/
  2. Runs derive_selected_token_resolution per closed position (rules 1+3+4 only).
  3. For each position where curPrice is near 0 or 1 AND a redemption
     exists, checks whether the curPrice prediction agrees with the
     redemption ground truth. Agreement rate quantifies how safe rule 2 is.
  4. Reports: per-wallet and aggregate resolution-source distribution,
     curPrice-vs-redemption agreement rate, and the share of "no_resolution_signal"
     positions rule 2 would have resolved if enabled.

NO live API calls. Read-only.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_dossier.derivers import (  # noqa: E402
    RESOLVE_BY_CURPRICE_ENABLED,
    derive_selected_token_resolution,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures-v4-staging"
# Spec §7.5 rule 2: curPrice > 1 - threshold → won; curPrice < threshold → lost.
# Standard threshold matches RESOLUTION_WIN_THRESHOLD in domain config.
CURPRICE_THRESHOLD = 0.03


def _outcome_index_to_indexset(oi):
    try:
        oi_int = int(oi)
    except (TypeError, ValueError):
        return None
    return str(1 << oi_int) if oi_int >= 0 else None


def _payout(redemption):
    try:
        return int(redemption.get("payout") or 0)
    except (TypeError, ValueError):
        return 0


def _redemption_outcome_for_position(closed_position, redemptions):
    """Replicate the §7.5 rule 1 truth lookup. Returns 'won'/'lost' or None."""
    if not redemptions:
        return None
    cid = (closed_position.get("conditionId") or "").lower()
    our_index_set = _outcome_index_to_indexset(closed_position.get("outcomeIndex"))
    if not our_index_set:
        return None
    matches = [
        r for r in redemptions
        if (r.get("condition") or "").lower() == cid
        and our_index_set in (r.get("indexSets") or [])
    ]
    if not matches:
        return None
    # Latest by timestamp.
    latest = max(matches, key=lambda r: int(r.get("timestamp") or 0))
    sets = list(latest.get("indexSets") or [])
    payout = _payout(latest)
    if sets == [our_index_set]:
        return "won" if payout > 0 else "lost"
    # Paired indexSet (e.g. ["1","2"]): only definitive when wallet
    # held only one side. For an agreement-rate test that's the
    # common case; if wallet held both sides we skip (ambiguous).
    return "won" if payout > 0 else "lost"


def _curprice_prediction(curprice):
    """Spec §7.5 rule 2: 'won' if curPrice > 1-threshold, 'lost' if < threshold."""
    if curprice is None:
        return None
    try:
        cp = float(curprice)
    except (TypeError, ValueError):
        return None
    if cp >= 1.0 - CURPRICE_THRESHOLD:
        return "won"
    if cp <= CURPRICE_THRESHOLD:
        return "lost"
    return None  # not in the rule-2 resolution band


def run() -> int:
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    print(f"# §11.5 verification — {len(fixtures)} v4 fixtures")
    print(f"# RESOLVE_BY_CURPRICE_ENABLED = {RESOLVE_BY_CURPRICE_ENABLED}")
    print(f"# curPrice resolution threshold = {CURPRICE_THRESHOLD}")
    print()

    overall_sources: Counter[str] = Counter()
    overall = {
        "n_positions": 0,
        "n_with_curprice_in_band": 0,
        "n_curprice_with_redemption": 0,
        "n_curprice_redemption_agree": 0,
        "n_curprice_redemption_disagree": 0,
        "n_rule2_resolvable_unknowns": 0,
        "disagreements_sample": [],
    }
    per_wallet = []

    for fx in fixtures:
        data = json.loads(fx.read_text())
        cps = data.get("closed_positions") or []
        ops = data.get("open_positions") or []
        reds = data.get("redemptions") or []
        # Pre-compute which conditions the wallet holds multiple asset_ids for.
        assets_by_cid: dict[str, set[str]] = defaultdict(set)
        for c in cps:
            cid = (c.get("conditionId") or "").lower()
            assets_by_cid[cid].add(str(c.get("asset") or ""))

        wallet_sources: Counter[str] = Counter()
        wallet_stats = {
            "wallet": fx.stem.split("_")[0],
            "n_positions": len(cps),
            "n_curprice_in_band": 0,
            "n_curprice_redemption_match": 0,
            "n_curprice_redemption_total": 0,
            "n_curprice_resolves_unknown": 0,
            "disagreements": [],
        }
        for c in cps:
            cid = (c.get("conditionId") or "").lower()
            holds_other = len(assets_by_cid[cid]) > 1
            res = derive_selected_token_resolution(
                closed_position=c,
                redemptions=reds,
                open_positions=ops,
                binary_market_n_outcomes=(2 if len(assets_by_cid[cid]) == 2 else None),
                wallet_holds_other_outcome_for_condition=holds_other,
                enable_curprice_rule=False,
            )
            wallet_sources[res.source] += 1
            overall_sources[res.source] += 1
            overall["n_positions"] += 1

            cp_pred = _curprice_prediction(c.get("curPrice"))
            if cp_pred is None:
                continue
            wallet_stats["n_curprice_in_band"] += 1
            overall["n_with_curprice_in_band"] += 1

            ground = _redemption_outcome_for_position(c, reds)
            if ground is not None and not holds_other:
                # Only count agreement when wallet held one side (single-side
                # paired-redemption payout is definitive).
                wallet_stats["n_curprice_redemption_total"] += 1
                overall["n_curprice_with_redemption"] += 1
                if ground == cp_pred:
                    wallet_stats["n_curprice_redemption_match"] += 1
                    overall["n_curprice_redemption_agree"] += 1
                else:
                    overall["n_curprice_redemption_disagree"] += 1
                    if len(overall["disagreements_sample"]) < 10:
                        overall["disagreements_sample"].append({
                            "wallet": wallet_stats["wallet"],
                            "condition": cid,
                            "asset": c.get("asset"),
                            "curPrice": c.get("curPrice"),
                            "curprice_pred": cp_pred,
                            "redemption_outcome": ground,
                        })
            # Would rule 2 have resolved a position currently in "no_resolution_signal"?
            if res.source == "no_resolution_signal":
                wallet_stats["n_curprice_resolves_unknown"] += 1
                overall["n_rule2_resolvable_unknowns"] += 1

        per_wallet.append((wallet_stats, wallet_sources))

    # ---- per-wallet report ----
    for stats, sources in per_wallet:
        print(f"## wallet {stats['wallet']}  n_positions={stats['n_positions']}")
        for src, n in sorted(sources.items(), key=lambda kv: -kv[1]):
            print(f"   {src:55} {n}")
        if stats["n_curprice_redemption_total"]:
            rate = stats["n_curprice_redemption_match"] / stats["n_curprice_redemption_total"]
            print(
                f"   curPrice⇔redemption agree rate: {stats['n_curprice_redemption_match']}/"
                f"{stats['n_curprice_redemption_total']} = {rate:.4f}"
            )
        else:
            print("   curPrice⇔redemption agree rate: no overlap (no testable cases)")
        print(
            f"   rule 2 would have resolved {stats['n_curprice_resolves_unknown']} "
            f"of currently-unknown positions"
        )
        print()

    # ---- aggregate ----
    print("# ========== aggregate ==========")
    print(f"  fixtures:                {len(fixtures)}")
    print(f"  total positions:         {overall['n_positions']}")
    print()
    print("  resolution_source distribution (rules 1+3+4 only, flag OFF):")
    for src, n in sorted(overall_sources.items(), key=lambda kv: -kv[1]):
        share = n / overall["n_positions"] if overall["n_positions"] else 0
        print(f"    {src:55} {n:>6}  ({share:.4f})")
    print()
    print(f"  positions with curPrice in resolution band [0, {CURPRICE_THRESHOLD}] ∪ "
          f"[{1-CURPRICE_THRESHOLD}, 1]:  {overall['n_with_curprice_in_band']}")
    print(f"  ... of those, also covered by a single-side redemption: "
          f"{overall['n_curprice_with_redemption']}")
    if overall["n_curprice_with_redemption"]:
        agree = overall["n_curprice_redemption_agree"]
        total = overall["n_curprice_with_redemption"]
        print(f"      curPrice prediction agrees with redemption: {agree}/{total} = "
              f"{agree/total:.4f}")
        print(f"      disagreements:                              "
              f"{overall['n_curprice_redemption_disagree']}")
        if overall["disagreements_sample"]:
            print("      sample disagreements:")
            for d in overall["disagreements_sample"]:
                print(
                    f"        wallet={d['wallet'][:10]}…  cond={d['condition'][:14]}…  "
                    f"asset={d['asset']}  curPrice={d['curPrice']}  "
                    f"pred={d['curprice_pred']}  redemption={d['redemption_outcome']}"
                )
    print()
    print(f"  rule-2 candidate gain (would resolve currently-unknown positions): "
          f"{overall['n_rule2_resolvable_unknowns']}")
    print()
    # Final verdict line for parseability.
    if overall["n_curprice_with_redemption"] >= 50:
        agree_rate = (
            overall["n_curprice_redemption_agree"] / overall["n_curprice_with_redemption"]
        )
        verdict = "ENABLE" if agree_rate >= 0.999 else (
            "CAUTION" if agree_rate >= 0.95 else "KEEP_OFF"
        )
    else:
        verdict = "INSUFFICIENT_OVERLAP"
    print(f"# verdict: {verdict}  "
          f"(spec: 'more unknown is preferable to fake certainty')")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
