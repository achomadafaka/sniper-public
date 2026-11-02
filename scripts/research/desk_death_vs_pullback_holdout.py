#!/usr/bin/env python3
"""Death-vs-pullback HOLDOUT grader (prereg c74aeaa7b section 5). k per indicator is FIXED from discovery (data/research/death-v2/read.txt:
SELL_BURST 2, EXODUS 3, STRUCT_BREAK 2, ONE_SWAP_DROP 2, TOP_DUMP 3), never re-chosen on H. usage: EVENTS_H.jsonl"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from desk_death_vs_pullback_read import INDS, boot_lo, flags, load, score

K_D = {"SELL_BURST": "2", "EXODUS": "3", "STRUCT_BREAK": "2", "ONE_SWAP_DROP": "2", "TOP_DUMP": "3"}
SIGN_D = {"SELL_BURST": 1, "EXODUS": 1, "STRUCT_BREAK": 1, "ONE_SWAP_DROP": 1, "TOP_DUMP": 1, "CREATOR_SELL": 1, "ANY": 1, "TWO_OF": 1}   # D benefit sign, all positive
cs = lambda e: bool(e["ind"]["CREATOR_SELL"]["0"])
RULES = {n: (lambda e, n=n: flags(e, n, K_D[n])) for n in INDS}
RULES["CREATOR_SELL"] = cs
RULES["ANY"] = lambda e: any(flags(e, n, K_D[n]) for n in INDS) or cs(e)
RULES["TWO_OF"] = lambda e: (sum(flags(e, n, K_D[n]) for n in INDS) + cs(e)) >= 2


def main(path):
    rows = load([path])
    out = {}
    for adverse in (False, True):
        evs = [e for e in rows if e["drop"] == 0.9 and e["adverse"] == adverse and not e.get("grad")]
        F = np.array([e["F"] for e in evs])
        print("\n== %s ordering: events %d coins %d; B_all benefit %+.4f; CRASH rate %.3f" % (
            "adverse" if adverse else "optimistic", len(evs), len({e["mint"] for e in evs}), float(-F.mean()), float((F <= -0.3).mean())))
        for name, fn in RULES.items():
            r = score(evs, fn)
            ok = (r["coins_flagged"] >= 100 and r["lo"] > 0 and r["dB_all_lo"] > 0 and r["share"] <= 0.5
                  and (r["F_unflagged"] is not None and r["F_unflagged"] >= r["F_all"]) and r["benefit"] > 0)
            out.setdefault(name, []).append(ok)
            print("%-13s flag%% %5.1f coins %4d benefit %+.4f lo %+.4f vsB_all lo %+.4f F_unfl %+.4f (F_all %+.4f) %s" % (
                name, 100 * r["share"], r["coins_flagged"], r["benefit"], r["lo"], r["dB_all_lo"], r["F_unflagged"] or 0, r["F_all"], "PASS-cond" if ok else "fail"))
    print("\nPASS (both orderings):", [n for n, v in out.items() if all(v)] or "none")


if __name__ == "__main__":
    main(sys.argv[1])
