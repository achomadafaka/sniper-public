"""Would a lower take-profit have made money on the positions the source sold on us?

Operator question, 2026-09-28 ~08:3xZ. EXPLORATORY: asked after the losses were seen, so no number
here is a pre-registered result (stage 2, e86feb94, found that no overlay survives its test split).

Rule T(x): exit at the first executable whole-bag quote whose return r >= x%. A lower TP only ADDS an
earlier exit, so it is exactly evaluable on the observed path up to our actual exit
(docs/superpowers/specs/2026-08-26-memecoin-exit-rail-replay-design.md). The path, population and
fill convention come from shape_overlay_replay.load(): funded, solana + 4663, closed since 09-13,
no manual / partial / de-risk fills, >= 2 exit_decision points; r = unit_price / entry_price - 1.
  Fill        the NEXT quote after the trigger (offset 1, stage 2's convention), and the trigger quote
              itself (offset 0, optimistic). rule P&L = entry_notional * r_fill / 100; a trigger on the
              last point, or no trigger, leaves the actual exit (delta 0).
  Cohorts     A  positions closed by the source selling (exit_reason mirror / mirror_source_sell);
              B  every position of the wallets that sold on us at least once (a per-wallet TP);
              C  every position (a roster-wide TP).
              A cannot be chosen in advance: a TP applies before anyone knows the source will sell.
              B and C carry the cost on the winners the lower TP would have cut short.
  CI          bootstrap 95% of the mean per-position delta (10k draws, seed 1).
Quotes are seconds apart, so a peak between two quotes is missed: T(x) is a LOWER bound on how often a
TP at x would have fired. Ledger read-only, no network.

    python3 scripts/research/source_sell_tp_replay.py
    python3 scripts/research/source_sell_tp_replay.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shape_overlay_replay import boot_ci, load  # noqa: E402

LEVELS = (5, 10, 15, 20, 25, 30)
MIRROR = ("mirror", "mirror_source_sell")


def tp_pnl(path, x, notional, offset=1):
    """P&L if T(x) fires, filled `offset` quotes after the trigger; None when the actual exit stands."""
    for i, (_t, r) in enumerate(path):
        if r >= x:
            if i + offset >= len(path):
                return None
            return float(notional) * path[i + offset][1] / 100.0
    return None


def deltas(rows, x, offset):
    out, fired = [], 0
    for r in rows:
        v = tp_pnl(r["path"], x, r["notional"], offset)
        if v is None:
            out.append(0.0)
        else:
            fired += 1
            out.append(v - r["pnl"])
    return out, fired


def report(label, rows):
    print("\n%s: n=%d, actual P&L $%+.2f" % (label, len(rows), sum(r["pnl"] for r in rows)))
    for offset in (1, 0):
        for x in LEVELS:
            d, fired = deltas(rows, x, offset)
            lo, hi = boot_ci(d)
            print("  TP %2d%% fill %-9s fired %3d  delta sum %+9.2f  mean %+7.3f  CI[%s, %s]  better %d worse %d"
                  % (x, "next quote" if offset else "trigger", fired, sum(d), sum(d) / len(d) if d else 0.0,
                     "%+.3f" % lo if lo is not None else "na", "%+.3f" % hi if hi is not None else "na",
                     sum(1 for v in d if v > 0), sum(1 for v in d if v < 0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load()
    sold = [r for r in rows if r["reason"] in MIRROR]
    wallets = {r["handle"] for r in sold}
    report("A  closed by the source selling (mirror / mirror_source_sell)", sold)
    per = collections.defaultdict(list)
    for r in sold:
        per[r["handle"]].append(r)
    print("\n  per wallet in A: n, actual P&L, max run-up seen before our exit (p50 over its positions), TP 10%% and 20%% deltas (next quote)")
    for h, rs in sorted(per.items(), key=lambda kv: sum(r["pnl"] for r in kv[1])):
        peaks = sorted(max(p[1] for p in r["path"]) for r in rs)
        print("    %-18s n=%2d  P&L %+8.2f  peak p50 %+6.1f%%  TP10 %+8.2f  TP20 %+8.2f" % (
            h, len(rs), sum(r["pnl"] for r in rs), peaks[len(peaks) // 2],
            sum(deltas(rs, 10, 1)[0]), sum(deltas(rs, 20, 1)[0])))
    report("B  every position of the %d wallets that sold on us" % len(wallets),
           [r for r in rows if r["handle"] in wallets])
    report("C  every position (roster-wide)", rows)


def selftest():
    path = [(0, 0.0), (5, 12.0), (8, 7.0), (20, -30.0)]
    assert tp_pnl(path, 10, 100.0) == 7.0 and tp_pnl(path, 10, 100.0, 0) == 12.0
    assert tp_pnl(path, 50, 100.0) is None
    assert tp_pnl([(0, 0.0), (1, 15.0)], 10, 100.0) is None          # trigger on the last point
    d, fired = deltas([{"path": path, "notional": 100.0, "pnl": -30.0}], 10, 1)
    assert fired == 1 and d == [37.0]
    print("selftest ok")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
