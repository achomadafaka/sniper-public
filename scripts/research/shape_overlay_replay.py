#!/usr/bin/env python3
"""Position shape study, stage 2: OVERLAY exit rules replayed on the exact funded exit paths.

Operator order 2026-09-27; pre-registered with Alpha before any outcome was read (split counts
only). An overlay rule only ADDS an earlier exit on top of the live rails. If it fires before our
actual exit it is priced on the executable path; otherwise the actual exit stands. Because the path
up to our exit is observed, such a rule is exactly evaluable. A rule that would hold longer than we
did is not (docs/superpowers/specs/2026-08-26-memecoin-exit-rail-replay-design.md).

  Population  funded (execution_mode 'real'), network solana (primary) and 4663 (secondary),
              opened_recorded_at >= 2026-09-13, closed, exit_reason != 'manual', no partial_exit /
              derisk / manual_close fill, >= 2 exit_decision points. Chain 56 excluded (open
              accounting question, aee0918e).
  Split       TRAIN fill < 2026-09-22T00:00Z, TEST fill >= then.
  Path        memecoin_route_series seam='exit_decision' ORDER BY id; r = unit_price/entry_price - 1;
              t from opened_recorded_at. No look-ahead: only the running max so far.
  Family      G(a,g) giveback: once running max >= a, exit when r <= running max - g,
              a in {10,15,20,30}, g in {5,10,15}.  N(t,x) no-lift: at the first point with
              t >= T, exit if running max < x, T in {15,30,60}, x in {3,8}.  E(t,y) early adverse:
              exit at the first point with t <= T and r <= -y, T in {10,30}, y in {10,15}.
  Fill        the NEXT path point after the trigger (about one quote later). rule_pnl =
              entry_notional * r_next / 100. A trigger on the last point leaves the actual exit.
  Delta       rule_pnl - realized_pnl, 0 when the rule does not fire.
  Selection   the variant with the max total delta on Solana TRAIN (ties: fewer triggers).
  Claim       "the overlay improves P&L" only if the bootstrap 95% CI lower bound of the mean
              per-position delta on Solana TEST is > 0 (10k draws, seed 1), for that one variant.

Ledger read-only (read-only URI). No network.
    python3 scripts/research/shape_overlay_replay.py
    python3 scripts/research/shape_overlay_replay.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import contextlib
import random
import sqlite3
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SINCE = "2026-09-13T00:00:00"
SPLIT = "2026-09-22T00:00:00"
VARIANTS = ([("G", a, g) for a in (10, 15, 20, 30) for g in (5, 10, 15)]
            + [("N", t, x) for t in (15, 30, 60) for x in (3, 8)]
            + [("E", t, y) for t in (10, 30) for y in (10, 15)])


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def trigger(path, rule):
    """path: [(t_s, r_pct)] in quote order. Index of the point where `rule` fires, or None."""
    kind, p1, p2 = rule
    run_max = None
    for i, (t, r) in enumerate(path):
        run_max = r if run_max is None else max(run_max, r)
        if kind == "G":
            if run_max >= p1 and r <= run_max - p2:
                return i
        elif kind == "N":
            if t >= p1:
                return i if run_max < p2 else None
        elif kind == "E":
            if t > p1:
                return None
            if r <= -p2:
                return i
    return None


def rule_pnl(path, rule, entry_notional, offset=1):
    """P&L if the rule fires and fills `offset` points after the trigger; None when it leaves the
    actual exit (no trigger, or no point that far after it)."""
    i = trigger(path, rule)
    if i is None or i + offset >= len(path):
        return None
    return float(entry_notional) * path[i + offset][1] / 100.0


def boot_ci(values, draws=10_000, seed=1):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def load():
    out = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        positions = c.execute(
            "SELECT p.id, p.network_id, p.source_handle, p.entry_price, p.entry_notional, "
            "p.realized_pnl, p.exit_reason, p.opened_recorded_at FROM memecoin_positions p "
            "WHERE p.execution_mode='real' AND p.is_open=0 AND p.opened_recorded_at >= ? "
            "AND p.network_id IN ('solana', '4663') AND COALESCE(p.exit_reason, '') != 'manual' "
            "AND NOT EXISTS (SELECT 1 FROM memecoin_fills f WHERE f.position_id=p.id "
            "AND f.kind IN ('partial_exit', 'derisk', 'manual_close')) ORDER BY p.id",
            (SINCE,)).fetchall()
        for pid, chain, handle, entry, notional, pnl, reason, fill_at in positions:
            if not entry or entry <= 0 or pnl is None:
                continue
            f0 = ts(fill_at)
            path = [(ts(o) - f0, (u / entry - 1.0) * 100.0) for o, u in c.execute(
                "SELECT observed_at, unit_price FROM memecoin_route_series WHERE position_id=? "
                "AND seam='exit_decision' ORDER BY id", (pid,)) if u and u > 0]
            if len(path) < 2:
                continue
            out.append({"id": pid, "chain": chain, "handle": handle, "notional": notional,
                        "pnl": float(pnl), "reason": reason, "path": path,
                        "split": "train" if fill_at < SPLIT else "test"})
    return out


def score(rows, rule, offset=1):
    deltas, fired = [], 0
    for r in rows:
        v = rule_pnl(r["path"], rule, r["notional"], offset)
        if v is None:
            deltas.append(0.0)
        else:
            fired += 1
            deltas.append(v - r["pnl"])
    return deltas, fired


def name(rule):
    return "%s(%g,%g)" % rule


def line(label, deltas, fired):
    lo, hi = boot_ci(deltas)
    n = len(deltas)
    return "%-26s n=%3d fired=%3d sum %+9.2f mean %+7.3f CI[%s, %s] better %d worse %d" % (
        label, n, fired, sum(deltas), sum(deltas) / n if n else 0.0,
        "%+.3f" % lo if lo is not None else "na", "%+.3f" % hi if hi is not None else "na",
        sum(1 for d in deltas if d > 0), sum(1 for d in deltas if d < 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load()
    sol = {s: [r for r in rows if r["chain"] == "solana" and r["split"] == s] for s in ("train", "test")}
    evm = {s: [r for r in rows if r["chain"] == "4663" and r["split"] == s] for s in ("train", "test")}
    print("solana train %d test %d | 4663 train %d test %d" % (
        len(sol["train"]), len(sol["test"]), len(evm["train"]), len(evm["test"])))
    print("actual realized: solana train %+.2f test %+.2f | 4663 train %+.2f test %+.2f" % (
        sum(r["pnl"] for r in sol["train"]), sum(r["pnl"] for r in sol["test"]),
        sum(r["pnl"] for r in evm["train"]), sum(r["pnl"] for r in evm["test"])))
    print("\nSOLANA TRAIN, all variants (delta = overlay - actual):")
    ranked = []
    for rule in VARIANTS:
        d, f = score(sol["train"], rule)
        ranked.append((sum(d), -f, rule))
        print("  " + line(name(rule), d, f))
    best = max(ranked)[2]
    print("\nSELECTED on train: %s" % name(best))
    d, f = score(sol["test"], best)
    print("PRIMARY  solana TEST  " + line(name(best), d, f))
    for off, label in ((0, "fill at trigger"), (2, "fill at next+1")):
        d, f = score(sol["test"], best, off)
        print("  sensitivity %-15s " % label + line(name(best), d, f))
    print("\nSECONDARY (descriptive, no claim): all variants on solana TEST")
    for rule in VARIANTS:
        d, f = score(sol["test"], rule)
        print("  " + line(name(rule), d, f))
    print("\nSECONDARY 4663, selected variant: train / test")
    for s in ("train", "test"):
        d, f = score(evm[s], best)
        print("  4663 %-5s " % s + line(name(best), d, f))


def selftest():
    #        t     r%
    path = [(1.0, -3.0), (5.0, 12.0), (8.0, 16.0), (12.0, 9.0), (20.0, 2.0), (40.0, -12.0),
            (70.0, -20.0)]
    assert trigger(path, ("G", 15, 5)) == 3           # max 16 at 8 s, 9 <= 11 at 12 s
    assert trigger(path, ("G", 15, 10)) == 4          # 2 <= 6 at 20 s
    assert trigger(path, ("G", 20, 5)) is None        # the max never reaches 20
    assert trigger(path, ("N", 15, 3)) is None        # by 20 s the max is 16 >= 3
    assert trigger(path, ("N", 15, 20)) == 4          # first point with t >= 15 is at 20 s; 16 < 20
    assert trigger(path, ("E", 10, 10)) is None       # -12 comes at 40 s, after the 10 s window
    assert trigger(path, ("E", 60, 10)) == 5
    # Running max, not the future max: a later high cannot arm the trail earlier.
    assert trigger([(1, 5.0), (2, 0.0), (3, 30.0), (4, 25.0)], ("G", 20, 10)) is None
    assert trigger([(1, 5.0), (2, 0.0), (3, 30.0), (4, 19.0)], ("G", 20, 10)) == 3
    assert rule_pnl(path, ("G", 15, 5), 100.0) == 2.0            # fills at the NEXT point: +2%
    assert rule_pnl(path, ("G", 15, 5), 100.0, offset=0) == 9.0
    assert rule_pnl(path, ("N", 60, 30), 100.0) is None          # fires on the last point
    d, f = score([{"path": path, "notional": 100.0, "pnl": -20.0},
                  {"path": [(1, 1.0), (2, 2.0)], "notional": 100.0, "pnl": 5.0}], ("G", 15, 5))
    assert d == [22.0, 0.0] and f == 1, (d, f)
    print("selftest ok")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
