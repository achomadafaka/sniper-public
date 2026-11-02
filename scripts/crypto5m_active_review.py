#!/usr/bin/env python3
"""Periodic performance review of the active paper TAKER engine.

Answers two questions: (1) how is the engine doing, and (2) is the CONVICTION gate (momentum + order-flow)
actually helping — do conviction-tagged entries outperform neutral ones? Read-only on the active paper DB.
It PROPOSES adjustments; it never changes thresholds (scoring/strategy tuning is human-gated). Designed to
be cron'd — resolves paths from its own location, appends a timestamped report to logs/.
"""
from __future__ import annotations
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)   # so `from sniper.algos...` resolves when run as a standalone cron script
DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")


def conviction_class(reason: str) -> str:
    """Classify an entry by the conviction state recorded in its reason text (deterministic engine output)."""
    r = (reason or "").lower()
    if "both favor" in r:
        return "lean_in (both favor)"
    if "bar raised" in r:
        return "entered_vs_penalty (one against)"
    if "favors" in r:
        return "one_favors"
    if "neutral trend" in r:
        return "neutral"
    return "untagged (pre-gate)"


def _stat(rows):
    n = len(rows)
    if not n:
        return (0, 0, 0.0, 0.0, 0.0)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    pnl = sum(r["pnl"] or 0 for r in rows)
    return (n, wins, wins / n, pnl, pnl / n)


def _line(label, rows):
    n, wins, wr, pnl, avg = _stat(rows)
    if not n:
        return f"  {label:34} n=0"
    return f"  {label:34} n={n:>4}  WR {wr:>4.0%}  PnL ${pnl:>+8.2f}  avg ${avg:>+6.3f}"


def review() -> str:
    if not os.path.exists(DB):
        return "active engine DB not found — nothing to review yet"
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT pnl, exit_kind, side, entry_reason, entry_ts FROM crypto5m_active_positions "
                     "WHERE status='closed' AND pnl IS NOT NULL ORDER BY entry_ts").fetchall()
    c.close()
    out = []
    n, wins, wr, pnl, avg = _stat(rows)
    out.append(f"=== active engine review — {n} closed trades ===")
    out.append(f"OVERALL: WR {wr:.0%}  PnL ${pnl:+.2f}  avg ${avg:+.3f}/trade")

    out.append("\nby exit kind:")
    for kind in sorted({r["exit_kind"] for r in rows if r["exit_kind"]}):
        out.append(_line(kind, [r for r in rows if r["exit_kind"] == kind]))

    out.append("\nby CONVICTION at entry (is the gate helping?):")
    classes = {}
    for r in rows:
        classes.setdefault(conviction_class(r["entry_reason"]), []).append(r)
    order = ["lean_in (both favor)", "one_favors", "neutral", "entered_vs_penalty (one against)",
             "untagged (pre-gate)"]
    for k in order:
        if k in classes:
            out.append(_line(k, classes[k]))

    out.append("\nby side:")
    for s in ("Up", "Down"):
        out.append(_line(s, [r for r in rows if r["side"] == s]))

    if n >= 40:
        recent, prior = rows[-20:], rows[:-20]
        rn, _, rwr, rpnl, ravg = _stat(recent)
        pn, _, pwr, ppnl, pavg = _stat(prior)
        out.append(f"\nrecent trend: last 20 avg ${ravg:+.3f}  vs  prior {pn} avg ${pavg:+.3f}  "
                   f"({'improving' if ravg > pavg else 'declining'})")

    # ---- EV gate (ev-reach-v1): admit fraction (both fees) + realized-vs-modeled (k-fit) ----
    try:
        from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG
        ec = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        ec.row_factory = sqlite3.Row
        af = EVLOG.admit_fraction(ec, "ev-reach-v1")
        out.append(f"\n[EV gate ev-reach-v1] confirmed ticks logged: {af['n']}  "
                   f"admit@0.07: {af['n_pass_007']} ({100*af['frac_007']:.1f}%)  "
                   f"admit@realized-0: {af['n_pass_realfee']} ({100*af['frac_realfee']:.1f}%)")
        # k-fit DISTRIBUTION, not just mean/mean — the mean ratio is fat-tail-driven and misleads (a couple of
        # outliers make a 50/50 coin flip look like edge). Show median, share-favorable, and NET-of-cost at BOTH
        # fees + the side split, so a k change is judged on robust net realized edge. Display-only; no threshold.
        import statistics as _st
        evobs = ec.execute("SELECT side, realized_mid_move r, gain g, cost_007 c7, cost_realfee cr "
                           "FROM crypto5m_ev_observations WHERE status='complete' AND realized_mid_move IS NOT NULL").fetchall()
        if evobs:
            rr = [x["r"] for x in evobs]
            gg = [x["g"] for x in evobs if x["g"] is not None]
            net_real = [x["r"] - (x["cr"] or 0) for x in evobs]
            net_007 = [x["r"] - (x["c7"] or 0) for x in evobs]
            sides = {}
            for x in evobs:
                sides[x["side"]] = sides.get(x["side"], 0) + 1
            cps = lambda v: f"{100*v:+.2f}c"                                            # noqa: E731 price->cents
            pos = lambda xs: f"{100*sum(1 for v in xs if v > 0)/len(xs):.0f}%"          # noqa: E731
            fav = sum(1 for v in rr if v > 0)
            out.append(f"[EV gate k-fit] completed obs: {len(evobs)}  sides: {sides}")
            out.append(f"  realized mid-move: mean {cps(_st.mean(rr))}  median {cps(_st.median(rr))}  "
                       f"favorable(>0): {fav}/{len(rr)} ({pos(rr)})  "
                       f"[modeled gain mean {cps(_st.mean(gg)) if gg else '—'}]")
            out.append(f"  NET of cost @realized-0: mean {cps(_st.mean(net_real))}  median {cps(_st.median(net_real))}  pos {pos(net_real)}")
            out.append(f"  NET of cost @0.07:       mean {cps(_st.mean(net_007))}  median {cps(_st.median(net_007))}  pos {pos(net_007)}")
            out.append("  read: the MEAN realized/modeled ratio is fat-tail-driven — judge a k change on NET "
                       "realized edge robustly >0 over large n (150+, BOTH sides), at the t_hold the new k implies.")
        else:
            out.append("[EV gate k-fit] no completed observations with realized move yet")
        ec.close()
    except Exception as exc:  # noqa: BLE001 — review must not crash on an absent/empty EV table
        out.append(f"[EV gate] readout unavailable: {exc}")

    # ---- proposed adjustments (heuristic, NOT auto-applied) ----
    out.append("\nproposed adjustments (review before changing any constant):")
    lean = classes.get("lean_in (both favor)", [])
    neutral = classes.get("neutral", [])
    pen = classes.get("entered_vs_penalty (one against)", [])
    tagged_total = sum(len(classes.get(k, [])) for k in order[:-1])
    if tagged_total < 30:
        out.append(f"  • only {tagged_total} conviction-tagged trades — too few to act on; keep collecting "
                   f"(want >=30 per class before trusting a split).")
    else:
        _, _, lwr, _, lavg = _stat(lean)
        _, _, nwr, _, navg = _stat(neutral)
        if lean and neutral:
            d = lavg - navg
            if d > 0.10:
                out.append(f"  • lean_in beats neutral by ${d:+.3f}/trade — the conviction signal has value; "
                           f"consider WIDENING the lean (raise MOMENTUM_LEAN) or sizing up conviction entries.")
            elif d < -0.10:
                out.append(f"  • lean_in UNDERperforms neutral by ${d:+.3f}/trade — the lean-in is hurting; "
                           f"consider removing the bar discount (MOMENTUM_LEAN→0) until it proves out.")
            else:
                out.append(f"  • lean_in vs neutral is ~flat (${d:+.3f}/trade) — conviction not yet separating; hold.")
        if pen:
            _, _, _, ppnl2, pavg2 = _stat(pen)
            if pavg2 < -0.05:
                out.append(f"  • entered_vs_penalty is net ${pavg2:+.3f}/trade — entering against one signal still "
                           f"loses; consider RAISING MOMENTUM_PENALTY or vetoing one-adverse entries too.")
    # exit mix
    stops = [r for r in rows if r["exit_kind"] == "stop"]
    _, _, _, spnl, savg = _stat(stops)
    if stops and savg < -0.20:
        out.append(f"  • stops average ${savg:+.3f} ({len(stops)} of them) — the dominant cost; review STOP_EDGE / "
                   f"FAIR_REVERSAL_STOP (cutting too late or too often).")
    out.append("  • n caveat: paper, cost-bound taker; treat <150 trades as directional only.")
    return "\n".join(out)


def main():
    import datetime  # only for the log stamp; engine logic uses Binance time elsewhere
    rep = review()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = os.path.join(ROOT, "logs", "crypto5m_active_review.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a") as f:
        f.write(f"\n\n######## {stamp} ########\n{rep}\n")
    if "--quiet" not in sys.argv:
        print(rep)


if __name__ == "__main__":
    main()
