#!/usr/bin/env python3
"""The read of docs/superpowers/specs/2026-08-29-tp-confirmation-window-prereg.md.

Hypothesis (operator, 2026-08-29): instead of selling at once when the take-profit triggers, ride
while the route value stays at or above TP and sell at the first mark below it, for at most w
seconds, w in {3, 5, 10}. Measured on the registered instrument, memecoin_post_tp_followups in
data/memecoin_refusal_captures.db (1-second whole-bag sellback quotes for 60 s after every funded
real_tp exit), read-only.

Operational choices, sent to Alpha before any outcome was read (2026-09-27):
  floor      TP VALUE of the sold bag = TP fill qty x memecoin_positions.tp_target_price. Values,
             never prices: the prereg's Defect 1 found exit_price and quote_price on different
             decimal bases. The TP fill is the memecoin_fills row whose tx_sig is the followup's
             close_sig; its notional is the realized exit value.
  confirm(w) walk the marks with age_s <= w in age order: the first mark with quote_value_usd below
             the floor is the exit, priced AT that mark (gap-through at the breaking mark, never at
             TP); if none breaks, the last mark at or before w. A non-'ok' mark reached before the
             decision CENSORS the window. No mark at or before w means NOT EVALUABLE.
  d          (confirm value - realized exit value) / (qty x entry_price) x 100, in points of entry.
  inference  per w: mean d, bootstrap 95% CI and two-sided p (10k draws, seed 1); Holm across the
             three windows at 0.05. The paired sd of the first 30 uncensored windows (by exit time)
             and the effect resolvable at 80% power are printed BEFORE any mean.
  ADOPT      mean >= +2 pp, Holm CI excludes 0, leak shown beside the gain, not carried by the top
             own-impact tercile. CLOSE if P(value >= floor at w=3) < 50%.
  impact     own impact = first ok mark value / realized exit value - 1 (section 0 confound).

A NULL result is UNINFORMATIVE by the prereg's section 0: our own sale depresses the observed path.

    python3 scripts/research/tp_confirm_window_read.py
    python3 scripts/research/tp_confirm_window_read.py --selftest
"""
import argparse
import contextlib
import random
import sqlite3
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
WINDOWS = (3, 5, 10)


def confirm(marks, floor, w):
    """marks: [(age_s, value, quality)] sorted by age. ('ok'|'censored'|'unevaluable', value, broke)."""
    upto = [m for m in marks if m[0] <= w]
    if not upto:
        return "unevaluable", None, None
    last = None
    for age, value, quality in upto:
        if quality != "ok" or value is None:
            return "censored", None, None
        if value < floor:
            return "ok", value, True
        last = value
    return "ok", last, False


def boot(values, draws=10_000, seed=1):
    """(ci_lo, ci_hi, two-sided p for mean 0)."""
    if len(values) < 2:
        return None, None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(draws))
    below = sum(1 for m in means if m <= 0) / draws
    above = sum(1 for m in means if m >= 0) / draws
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1], min(1.0, 2 * min(below, above))


def holm(pvals, alpha=0.05):
    """{key: (adjusted p, rejected)} by Holm's step-down."""
    order = sorted(pvals, key=lambda k: pvals[k])
    out, running, stop = {}, 0.0, False
    m = len(order)
    for i, k in enumerate(order):
        adj = min(1.0, max(running, (m - i) * pvals[k]))
        running = adj
        rejected = (not stop) and pvals[k] <= alpha / (m - i)
        stop = stop or not rejected
        out[k] = (adj, rejected)
    return out


def load():
    rows = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % CAPTURES, uri=True, timeout=10)) as cap, \
            contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                               uri=True, timeout=10)) as led:
        heads = cap.execute(
            "SELECT position_id, close_sig, exited_at, exit_value_usd FROM memecoin_post_tp_followups "
            "WHERE age_s IS NULL AND exit_reason='real_tp' ORDER BY exited_at").fetchall()
        for pid, sig, exited_at, exit_value in heads:
            fill = led.execute("SELECT qty, notional FROM memecoin_fills WHERE position_id=? AND tx_sig=?",
                               (pid, sig)).fetchone()
            pos = led.execute("SELECT entry_price, tp_target_price FROM memecoin_positions WHERE id=?",
                              (pid,)).fetchone()
            if not fill or not pos or not pos[0] or not pos[1] or not fill[0]:
                rows.append({"id": pid, "skip": "no fill/tp join"})
                continue
            marks = [(a, v, q) for a, v, q in cap.execute(
                "SELECT age_s, quote_value_usd, quality FROM memecoin_post_tp_followups "
                "WHERE position_id=? AND age_s IS NOT NULL ORDER BY age_s", (pid,))]
            qty = float(fill[0])
            rows.append({"id": pid, "exited_at": exited_at, "realized": float(fill[1]),
                         "cap_exit_value": exit_value, "floor": qty * float(pos[1]),
                         "cost": qty * float(pos[0]), "marks": marks})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load()
    skipped = [r for r in rows if r.get("skip")]
    rows = [r for r in rows if not r.get("skip")]
    mismatch = sum(1 for r in rows if r["cap_exit_value"] and abs(r["cap_exit_value"] / r["realized"] - 1) > 0.001)
    print("followups %d (skipped %d: no fill/tp join), capture exit value != fill notional: %d"
          % (len(rows) + len(skipped), len(skipped), mismatch))
    print("realized exit vs floor (pp of entry): p50 %+.2f" % statistics.median(
        [(r["realized"] - r["floor"]) / r["cost"] * 100 for r in rows]))
    res = {}
    for w in WINDOWS:
        per = []
        for r in rows:
            status, value, broke = confirm(r["marks"], r["floor"], w)
            first_ok = next((v for _a, v, q in r["marks"] if q == "ok" and v is not None), None)
            per.append({"status": status, "broke": broke, "exited_at": r["exited_at"],
                        "d": (value - r["realized"]) / r["cost"] * 100 if status == "ok" else None,
                        "leak": (r["floor"] - value) / r["cost"] * 100 if broke else None,
                        "impact": (first_ok / r["realized"] - 1) * 100 if first_ok else None})
        res[w] = per
    # 1. nuisance parameter first: sd of the first 30 uncensored windows, and the resolvable effect.
    print("\nSTEP 1 (before any mean): paired sd from the first 30 uncensored windows by exit time")
    for w in WINDOWS:
        ok = [p for p in res[w] if p["status"] == "ok"]
        sd30 = statistics.stdev([p["d"] for p in ok[:30]]) if len(ok) >= 30 else None
        n = len(ok)
        print("  w=%2ds: n uncensored %d, sd(first 30) %s pp, resolvable at 80%% power ~ %s pp" % (
            w, n, "%.3f" % sd30 if sd30 else "na",
            "%.3f" % (2.8 * sd30 / n ** 0.5) if sd30 else "na"))
    # 2. the read.
    print("\nSTEP 2: the read")
    pvals, summary = {}, {}
    for w in WINDOWS:
        per = res[w]
        ok = [p for p in per if p["status"] == "ok"]
        cens = sum(1 for p in per if p["status"] == "censored")
        unev = sum(1 for p in per if p["status"] == "unevaluable")
        d = [p["d"] for p in ok]
        lo, hi, pv = boot(d)
        cont = sum(1 for p in ok if not p["broke"])
        leaks = [p["leak"] for p in ok if p["broke"]]
        gains = [p["d"] for p in ok if not p["broke"]]
        pvals[w] = pv if pv is not None else 1.0
        summary[w] = (ok, d, lo, hi, cont)
        print("  w=%2ds: evaluable %d/%d, censored %d, unevaluable %d | P(>= floor at w) %.1f%% (%d/%d)"
              % (w, len(ok), len(per), cens, unev, 100.0 * cont / len(ok) if ok else 0, cont, len(ok)))
        print("         mean d %+.3f pp, median %+.3f, CI95 [%+.3f, %+.3f], raw p %.4f" % (
            statistics.mean(d), statistics.median(d), lo, hi, pv))
        print("         gain when it held (n=%d): mean %s pp | leak when it broke (n=%d): mean %s pp, "
              "p50 %s, p90 %s" % (
                  len(gains), "%+.3f" % statistics.mean(gains) if gains else "na", len(leaks),
                  "%.3f" % statistics.mean(leaks) if leaks else "na",
                  "%.3f" % statistics.median(leaks) if leaks else "na",
                  "%.3f" % sorted(leaks)[int(0.9 * (len(leaks) - 1))] if leaks else "na"))
        imp = sorted((p for p in ok if p["impact"] is not None), key=lambda p: p["impact"])
        k = len(imp) // 3
        for name, part in (("low", imp[:k]), ("mid", imp[k:2 * k]), ("high", imp[2 * k:])):
            if part:
                print("         impact tercile %-4s n=%3d impact p50 %+.2f%% mean d %+.3f pp" % (
                    name, len(part), statistics.median([p["impact"] for p in part]),
                    statistics.mean([p["d"] for p in part])))
    adj = holm(pvals)
    print("\nHolm across w in %s: %s" % (list(WINDOWS), {w: ("p_adj %.4f" % v[0], "reject" if v[1] else "keep")
                                                       for w, v in adj.items()}))
    ok3, _d3, _l, _h, cont3 = summary[3]
    p3 = cont3 / len(ok3) if ok3 else 0
    print("CLOSE rule: P(>= floor at 3 s) = %.1f%% -> %s" % (100 * p3, "CLOSE THE QUESTION" if p3 < 0.5 else "open"))
    for w in WINDOWS:
        ok, d, lo, hi, _c = summary[w]
        print("ADOPT w=%ds: mean >= +2 pp %s, Holm reject %s" % (
            w, statistics.mean(d) >= 2.0, adj[w][1]))


def selftest():
    floor = 100.0
    held = [(1.0, 105.0, "ok"), (2.5, 104.0, "ok"), (4.0, 99.0, "ok"), (8.0, 120.0, "ok")]
    assert confirm(held, floor, 3) == ("ok", 104.0, False)     # still above at 3 s: last mark <= 3 s
    assert confirm(held, floor, 5) == ("ok", 99.0, True)       # breaks at 4 s, priced AT the mark
    assert confirm(held, floor, 10) == ("ok", 99.0, True)      # the 8 s rebound is never reached
    assert confirm([(3.5, 101.0, "ok")], floor, 3)[0] == "unevaluable"
    assert confirm([(1.0, 101.0, "no_route"), (2.0, 90.0, "ok")], floor, 3)[0] == "censored"
    assert confirm([(1.0, 99.0, "ok"), (2.0, None, "no_route")], floor, 3) == ("ok", 99.0, True)
    lo, hi, p = boot([1.0, 2.0, 3.0, 4.0])
    assert 1.0 <= lo <= hi <= 4.0 and p == 0.0
    h = holm({3: 0.04, 5: 0.01, 10: 0.03})
    assert h[5] == (0.03, True) and h[10] == (0.06, False) and h[3][1] is False, h
    print("selftest ok")


if __name__ == "__main__":
    main()
