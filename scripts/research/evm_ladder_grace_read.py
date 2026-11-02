"""The EVM buy ladder's post-hit grace, replayed on every measured ladder in the ledger.

Question (Alpha, 2026-09-28, for the operator's grace decision): with a grace G after the first
measured hit, how many ladders pick a worse route than today's grace, by how much, and how much
ladder time does G save? Lambda answered it for 4663 (the Robinhood speed design, section 4);
this reads 8453, 56 and 5042 the same way.

WRITER. `evm_real_seams.venue_quote_with_v2_fallback` emits one record per rung (`rung_record`),
and `evm_swap_executor` hands them to `MemecoinEntryTrace.mark_substage` as the `rungs` of the
`evm.quote_initial` and `evm.quote_miss` substages. `MemecoinEntryTrace.finish` persists them in
`memecoin_entry_attempts.trace_json` (`substage_events`). Read-only, straight from the ledger.

ONE CLOCK. A rung's `duration_ms` is time.perf_counter from the start of that rung's `_run` on its
own pool thread. All rungs are submitted together to a pool sized to the ladder, so a duration is
the rung's answer time from ladder start. The runner's grace uses time.monotonic; both are
CLOCK_MONOTONIC on this host. A rung cut by the recorded ladder carries a marker instead of an
answer time: `ladder_grace` (the recorded grace end) or `ladder_deadline` (8 s).

REPLAY. The code's rule: rungs answer in time order; the first measured hit (a hit with a numeric
round_trip_pct) opens a grace of G; a measured launchpad/flap CURVE hit ends the wait at once;
the ladder takes the best measured round trip among the rungs answered by then (ties to the
earlier rung in the code's order). "Today" is the same replay at today's grace (1.5 s; runner
started 2026-09-28T01:10:04Z after the last .env write). A ladder is CENSORED at G when the
replay would have waited past the point where the recorded ladder cut a rung: that rung's answer
is unknown, so the row is counted apart, never guessed.

Usage: python3 scripts/research/evm_ladder_grace_read.py [--chains 8453,56,5042]
       python3 scripts/research/evm_ladder_grace_read.py --selftest
"""
import argparse
import json
import sqlite3
import statistics

LEDGER = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
ORDER = ["launchpad_curve", "flap_curve", "v3", "v2", "pons_v2", "v4", "v4_multihop", "mixed_v3_v2",
         "kyber", "relay"]
CURVES = {"launchpad_curve", "flap_curve"}
CUT = {"ladder_grace", "ladder_deadline"}
TODAY_S = 1.5
DEADLINE_S = 8.0
GRACES = (0.25, 0.5, 1.0, 1.5)
# The recorded grace end (`ladder_grace`, monotonic from ladder start taken after submit) sits
# -3.3 to +0.6 ms from the first hit's rung time plus the grace (45 ladders, 2026-09-28; one +97 ms
# outlier). A replay limit within this slack of the recorded one is the same limit, not a longer
# wait: a strict comparison marked every ladder recorded at today's 1.5 s grace as censored.
CLOCK_SLACK_MS = 25.0


def ladders(rows):
    """{run_id: (chain, [rung records])} from (network_id, trace_json) rows: one ladder per run_id
    (the same run can ride on two substages), only runs with a measured hit and more than one rung
    record (a winner refresh asks one rung)."""
    out = {}
    for chain, trace in rows:
        for ev in (json.loads(trace).get("substage_events") or ()):
            for r in ev.get("rungs") or ():
                if r.get("run_id"):
                    run = out.setdefault(r["run_id"], (chain, {}))[1]
                    run[r["rung"]] = r
    keep = {}
    for run_id, (chain, run) in out.items():
        recs = list(run.values())
        if len(recs) > 1 and any(measured(r) for r in recs):
            keep[run_id] = (chain, recs)
    return keep


def measured(r):
    return bool(r.get("hit")) and isinstance(r.get("round_trip_pct"), (int, float))


def replay(recs, grace_s, deadline_s=DEADLINE_S):
    """(picked round trip pct or None, ladder end ms, censored). The runner's rule on the recorded
    answer times; `grace_s=None` waits for every recorded answer (the full information pick)."""
    answered = sorted((r for r in recs if not r.get("skipped") and r.get("blind_reason") not in CUT),
                      key=lambda r: r["duration_ms"])
    cut = [r["duration_ms"] for r in recs if r.get("blind_reason") in CUT]
    limit = deadline_s * 1000.0
    taken, grace_at = [], None
    for r in answered:
        t = float(r["duration_ms"])
        if t > min(limit, grace_at if grace_at is not None else limit):
            break
        taken.append(r)
        if measured(r) and grace_s is not None:
            if r["rung"] in CURVES:
                grace_at = t
            elif grace_at is None:
                grace_at = t + grace_s * 1000.0
    stop = min(limit, grace_at if grace_at is not None else limit)
    done = len(taken) == len(answered) and not cut
    end = max((float(r["duration_ms"]) for r in taken), default=0.0) if done else stop
    censored = any(c < stop - CLOCK_SLACK_MS for c in cut) if grace_s is not None else bool(cut)
    best = None
    for r in taken:
        if measured(r):
            key = (float(r["round_trip_pct"]), -ORDER.index(r["rung"]) if r["rung"] in ORDER else -99)
            if best is None or key > best:
                best = key
    return (best[0] if best else None), end, censored


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def table(runs):
    """One chain's rows: G, ladders compared, worse than today (n, sum pp, n > 0.5 pp), censored,
    and the ladder time saved against today (p50, p90, ms)."""
    lines = []
    base = {k: replay(recs, TODAY_S) for k, recs in runs.items()}
    late_only = [k for k in runs if base[k][0] is None]     # the only measured hit came after 8 s
    if late_only:
        lines.append("%d ladders left out: no measured hit inside today's %.0f s deadline" % (
            len(late_only), DEADLINE_S))
        runs = {k: v for k, v in runs.items() if k not in late_only}
    full = {k: replay(recs, None) for k, recs in runs.items()}
    cmp_full = [k for k in runs if not base[k][2] and not full[k][2]]
    lost = [full[k][0] - base[k][0] for k in cmp_full if base[k][0] < full[k][0]]
    lines.append("today (%.2f s) vs every recorded answer: %d of %d worse, %.2f pp, %d > 0.5 pp; "
                 "%d with a recorded cut" % (TODAY_S, len(lost), len(cmp_full), sum(lost),
                                             sum(1 for x in lost if x > 0.5), len(runs) - len(cmp_full)))
    lines.append("| G (s) | ladders | worse than today | sum pp | worse > 0.5 pp | censored | saved p50 ms | saved p90 ms |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for g in GRACES:
        worse, saved, censored, n = [], [], 0, 0
        for k, recs in runs.items():
            pick, end, cens = replay(recs, g)
            if cens or base[k][2]:
                censored += 1
                continue
            n += 1
            saved.append(base[k][1] - end)
            if pick is not None and base[k][0] is not None and pick < base[k][0]:
                worse.append(base[k][0] - pick)
        lines.append("| %.2f | %d | %d | %.2f | %d | %d | %s | %s |" % (
            g, n, len(worse), sum(worse), sum(1 for x in worse if x > 0.5), censored,
            "%.0f" % pct(saved, 0.5) if saved else "-", "%.0f" % pct(saved, 0.9) if saved else "-"))
    return lines


def read(ledger, chains):
    conn = sqlite3.connect("file:%s?mode=ro" % ledger, uri=True, timeout=5)
    try:
        marks = ",".join("?" * len(chains))
        cur = conn.execute("SELECT network_id, trace_json FROM memecoin_entry_attempts "
                           "WHERE network_id IN (%s) AND trace_json LIKE '%%\"rungs\"%%'" % marks,
                           list(chains))
        return ladders(cur)                         # streamed; one trace in memory at a time
    finally:
        conn.close()


def selftest():
    rung = lambda name, ms, pct=None, hit=True, **kw: dict(
        {"rung": name, "duration_ms": ms, "hit": hit, "skipped": False, "blind_reason": None,
         "run_id": "r"}, **({"round_trip_pct": pct} if pct is not None else {}), **kw)
    # v4 hits first at 300 ms; relay is better but answers at 1,700 ms: in at 1.5 s, out at 1.0 s.
    recs = [rung("v4", 300, -8.0), rung("relay", 1700, -3.0), rung("v3", 75, hit=False)]
    assert replay(recs, 1.5) == (-3.0, 1700.0, False)
    assert replay(recs + [rung("kyber", 2100, -1.0)], 1.5) == (-3.0, 1800.0, False)   # 2.1 s: out
    assert replay(recs, 1.0) == (-8.0, 1300.0, False)
    assert replay(recs, 0.25) == (-8.0, 550.0, False)
    # A curve hit ends the wait at once, whatever the grace.
    curve = [rung("launchpad_curve", 400, -2.0), rung("v4", 900, -1.0)]
    assert replay(curve, 1.5)[:2] == (-2.0, 400.0)
    # A rung the recorded ladder cut at its grace end is unknown: censored only if the replay would
    # have waited that long.
    cut = [rung("v4", 300, -8.0), rung("relay", 2300, hit=False, blind_reason="ladder_grace")]
    assert replay(cut, 1.5)[2] is False and replay(cut, 2.5)[2] is True
    # A ladder recorded at today's grace: its marker sits ~3 ms under hit + grace (other clock).
    same = [rung("v3", 73, -4.55), rung("kyber", 848, -2.03), rung("v4", 1570, hit=False, blind_reason="ladder_grace")]
    assert replay(same, 1.5) == (-2.03, 1573.0, False) and replay(same, 0.5) == (-4.55, 573.0, False)
    # Ties go to the earlier rung in the code's order; an unmeasured hit never wins.
    tie = [rung("relay", 100, -5.0), rung("v3", 200, -5.0), rung("kyber", 50, "round_trip_unmeasured")]
    assert replay(tie, 1.5)[0] == -5.0
    runs = {"a": recs, "b": curve}
    text = "\n".join(table(runs))
    assert "| 1.00 | 2 | 1 | 5.00 | 1 | 0 |" in text, text
    assert "| 1.50 | 2 | 0 | 0.00 | 0 | 0 |" in text, text
    # ladders(): one ladder per run_id across substages; a one-rung refresh and a hitless run drop.
    row = json.dumps({"substage_events": [
        {"name": "evm.quote_initial", "rungs": [dict(r, run_id="x") for r in recs]},
        {"name": "evm.quote_miss", "rungs": [dict(r, run_id="x") for r in recs]},
        {"name": "evm.quote_initial", "rungs": [rung("v4", 90, -1.0, run_id="solo")]},
        {"name": "evm.quote_initial", "rungs": [rung("v4", 90, hit=False, run_id="none"),
                                                rung("v3", 90, hit=False, run_id="none")]}]})
    got = ladders([("8453", row)])
    assert list(got) == ["x"] and len(got["x"][1]) == 3, got
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", default="8453,56,5042")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    runs = read(args.ledger, args.chains.split(","))
    for chain in args.chains.split(","):
        mine = {k: recs for k, (c, recs) in runs.items() if c == chain}
        firsts = [min(float(r["duration_ms"]) for r in recs if measured(r)) for recs in mine.values()]
        print("\n## %s: %d measured ladders; first measured hit p50 %s ms, p90 %s ms" % (
            chain, len(mine), "%.0f" % statistics.median(firsts) if firsts else "-",
            "%.0f" % pct(firsts, 0.9) if firsts else "-"))
        if mine:
            print("\n".join(table(mine)))


if __name__ == "__main__":
    main()
