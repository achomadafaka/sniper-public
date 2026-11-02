#!/usr/bin/env python3
"""Report exit-rail variant comparisons over the historical mark series. Read-only.

Per the pre-registered decision rule in
docs/superpowers/specs/2026-08-26-memecoin-exit-rail-replay-design.md, this replay can only
ELIMINATE variants whose pre-exit mechanics are worse than baseline beyond the band. It cannot
promote a ladder — the remainder is held past our exit and no historical data prices it. That leg is
measured forward by the post-TP collector.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sniper.analysis import memecoin_exit_rail_replay as R
from sniper.analysis import memecoin_rails_model as rm


def variants(base):
    """The candidate families from the spec, expressed relative to the live rail."""
    out = []
    for tp in (15.0, 20.0, 30.0):
        out.append(("TP %g%% (stop %g)" % (tp, base.stop_pct),
                    rm.RailsPolicy(tp_pct=tp, stop_pct=base.stop_pct, max_hold_s=base.max_hold_s)))
    for stop in (30.0, 50.0):
        out.append(("stop %g%% (TP %g)" % (stop, base.tp_pct),
                    rm.RailsPolicy(tp_pct=base.tp_pct, stop_pct=stop, max_hold_s=base.max_hold_s)))
    for frac in (0.5, 0.6, 0.75):
        out.append(("ladder %g at TP %g, trail 15/10" % (frac, base.tp_pct),
                    rm.RailsPolicy(tp_pct=base.tp_pct, tp_fraction=frac, stop_pct=base.stop_pct,
                                   trail_arm_pct=15.0, trail_pct=10.0, max_hold_s=base.max_hold_s)))
    for arm, dist in ((20.0, 15.0), (30.0, 20.0)):
        out.append(("ladder 0.5, trail %g/%g" % (arm, dist),
                    rm.RailsPolicy(tp_pct=base.tp_pct, tp_fraction=0.5, stop_pct=base.stop_pct,
                                   trail_arm_pct=arm, trail_pct=dist, max_hold_s=base.max_hold_s)))
    return out


def main(db="sniper_tracker.db"):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    cohort, excluded = R.load_cohort(conn)
    print("COHORT  era=%s  density floor %.2f marks/s" % (str(R.CURRENT_ERA), R.DENSITY_FLOOR))
    print("  eligible: %d" % len(cohort))
    for k, v in excluded.items():
        print("  excluded %-22s %d" % (k, v))
    if not cohort:
        return 1

    ratios = sorted(p.fill_over_chart for p in cohort if p.fill_over_chart)
    if ratios:
        q = lambda f: ratios[int(f * (len(ratios) - 1))]
        print("\nPRICE SPACE  entry fill / chart mid at open  n=%d" % len(ratios))
        print("  p10 %.3f  median %.3f  p90 %.3f  max %.3f   <- systematic, NOT in any band below"
              % (q(.1), q(.5), q(.9), ratios[-1]))

    print("\nBASELINE FIDELITY  simulated live rail vs what actually happened")
    print("  (manual closes excluded — an operator is not a rail)")
    for q in (R.GAP_BASIS_Q, 1.0):
        fid = R.baseline_fidelity(cohort, cadence_q=q)
        pct = (100.0 * fid["agree"] / fid["scored"]) if fid["scored"] else 0.0
        print("  censor at own-gap q=%.2f: determinate %2d/%d   agrees %2d/%-2d (%.0f%%)"
              % (q, len(cohort) - fid["censored"], len(cohort), fid["agree"], fid["scored"], pct))
        for (real, got), n in sorted(fid["confusion"].items(), key=lambda kv: -kv[1]):
            print("      %-12s -> %-14s %d" % (real, got, n))

    base = R.baseline_policy(cohort[0].policy)
    vs = variants(base)
    print("\nCOMPARISONS  variant minus baseline, paired per position, 95% bootstrap over positions")
    print("  (the interval covers SAMPLING error only — the price-space term above is systematic)")
    for q in (R.GAP_BASIS_Q, 1.0):
        print("  -- censor at own-gap q=%.2f --" % q)
        results = [R.compare(cohort, v, label=lbl, cadence_q=q) for lbl, v in vs]
        for c in sorted(results, key=lambda c: -c.mean_delta_pts):
            print("    " + str(c))
        n_diff = sum(1 for c in results if c.distinguishable)
        print("    %d of %d separate from baseline beyond the band." % (n_diff, len(results)))

    stable, orders = R.ordering_is_stable(cohort, vs)
    print("\nCENSORING SENSITIVITY  does the censor choice decide the ranking?")
    print("  ordering %s across q=%s" % ("HOLDS" if stable else "FLIPS", list(orders)))
    for q, order in orders.items():
        print("    q=%.2f best->worst: %s" % (q, " > ".join(order[:3])))

    print("\nVERDICT")
    if stable:
        print("  The ranking survives the censoring choice. Variants separated beyond the band may")
        print("  be ELIMINATED per the pre-registered rule. None may be promoted here.")
    else:
        print("  The ranking DEPENDS ON THE CENSORING CHOICE, so no variant may be eliminated.")
        print("  Switching the censor off is what produces separations, and that is also the")
        print("  setting with the lowest baseline fidelity — it lets the model resolve rails past")
        print("  holes it cannot see. Two settings of one knob disagree about the order, so the")
        print("  historical mark series cannot rank these exit variants on this book.")
    print("  Ladders showing WORSE is the PRE-EXIT BOUNDARY, not evidence against ladders: the")
    print("  remainder is held past our exit and no historical data prices it. Only the post-TP")
    print("  collector can answer that leg, and it needs forward time.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
