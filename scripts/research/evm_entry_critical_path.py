#!/usr/bin/env python3
"""EVM funded entry: where decide-to-send time goes, on the decision thread (Alpha 2026-10-01).

WHY NOT THE STAGE COLUMNS. `memecoin_entry_attempts.*_ms` cannot be summed or read as the path:
  - `safety_checks_ms` is the safety worker's OWN duration (memecoin_evm_leg._mark_safety passes
    it explicitly). The worker starts before the reservation and overlaps it and the quote ladder.
  - `quote_ms`, `signing_ms`, `transaction_submitted_ms` are the gap since the PREVIOUS stage mark,
    projected to the LATEST submission attempt (EntryAttemptTrace.mark_submission). On a min-out
    retry `quote_ms` is the retry, and the first ladder sits before the explicit safety mark, so it
    is in no column at all.
This script rebuilds the path from the substage events instead. Every substage carries
`elapsed_ms` (when it was written, from the attempt start, one perf_counter). Consecutive marks
partition the decision thread's wall time, so the walls between them ADD UP to the elapsed time
of the first `evm.broadcast` exactly. Each wall is attributed to the mark that ends it.

DENOMINATOR: the FIRST attempt per origin_event_id (trap e3a2c165) on each chain, decision start
since --since, excluding manual entries; "sent" = it reached `evm.broadcast`.
Ledger read-only (read-only URI). No network.

    python3 scripts/research/evm_entry_critical_path.py --since 2026-09-24T00:00:00 --chains 4663,56
    python3 scripts/research/evm_entry_critical_path.py --selftest
"""
import argparse
import collections
import contextlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# `probe_pre_source_floor` ends the CALLER's stretch (memecoin_real_runner._hook_raw): identity,
# source checks and, for an event without a source market cap (most J7 events), the live `stats_fn`
# market-data GET. The hook's `dex_pair_selection` then reads ~0 from classification_context. An
# event that carries its mcap skips the caller GET and pays it at `dex_pair_selection` instead.
PRE_EXEC = {"probe_pre_source_floor": "market_data_caller", "dex_pair_selection": "market_data_hook",
            "onchain_volume_m5": "volume_read", "timing_gate_balance_read": "gate_balance_read"}
TRIGGERS = {"evm.stale_aggregator_refusal", "evm.entry_preflight_fallback",
            "evm.entry_preflight_retry"}
AFTER = {"evm.approval": "approval", "evm.quote_post_approval": "post_approval_requote",
         "evm.pre_transactions": "pre_tx", "evm.pre_transaction_pipeline": "pre_tx",
         "evm.native_float": "pre_tx", "evm.swap_signing": "signing", "evm.broadcast": "broadcast"}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def walls(events):
    """Split the decision thread up to the first evm.broadcast into attributed walls.

    Returns (walls{category: ms}, sent_ms or None). Categories add to sent_ms by construction."""
    out = collections.Counter()
    prev, in_exec, rework, ladders, seen = 0.0, False, False, 0, collections.Counter()
    for e in events:
        name, end = e.get("name"), e.get("elapsed_ms")
        if name is None or end is None:
            continue
        end = float(end)
        wall = max(0.0, end - prev)
        prev = end
        seen[name] += 1
        if name == "evm.balance_usd_snapshot":
            in_exec = True
        if not in_exec and not name.startswith("evm."):
            out["pre:" + PRE_EXEC.get(name, "gates_other")] += wall
            continue
        in_exec = True
        if name in TRIGGERS or (name == "evm.entry_preflight" and e.get("outcome") == "refused"):
            # the refused simulation itself is preflight; everything until the next pass is rework
            out["preflight" if name == "evm.entry_preflight" and not rework else "rework"] += wall
            rework = True
            continue
        if rework:
            if name == "evm.entry_preflight" and e.get("outcome") == "passed":
                rework = False
            out["rework"] += wall
            continue
        if name == "evm.quote_initial":
            ladders += 1
            out["ladder" if ladders == 1 else "rework"] += wall
        elif name == "evm.balance_pre":
            out["balance_pre"] += wall
        elif name == "evm.token_safety":
            out["safety_wait"] += wall
        elif name == "evm.entry_preflight":
            out["preflight"] += wall
        elif name in AFTER:
            out[AFTER[name]] += wall
        else:
            out["exec_other"] += wall
        if name == "evm.broadcast":
            return dict(out), end
    return dict(out), None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def first_event(events, name):
    return next((e for e in events if e.get("name") == name), None)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def load(since, chains, db):
    q = ("SELECT attempt_id, origin_event_id, network_id, signal_source, delivery_lane, mint, "
         "decision_started_at, observed_at, terminal_status, terminal_reason, trace_json "
         "FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_completed "
         "WHERE completed_at >= ? AND network_id IN (%s) ORDER BY decision_started_at"
         % ",".join("?" * len(chains)))
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % db,
                                            uri=True, timeout=10)) as c:
        rows = c.execute(q, (since, *chains)).fetchall()
    firsts = {}
    for r in rows:
        if r[6] < since or r[3] == "manual":
            continue
        firsts.setdefault(r[1] or r[0], r)
    out = []
    for r in firsts.values():
        tr = json.loads(r[10]) if r[10] else {}
        ev = tr.get("substage_events") or []
        w, sent = walls(ev)
        out.append({"chain": r[2], "lane": r[4] or r[3] or "?", "mint": (r[5] or "").lower(),
                    "started": r[6], "observed": r[7], "status": r[8], "reason": r[9] or "",
                    "events": ev, "walls": w, "sent_ms": sent})
    return out


def fmt(xs):
    a, b = pct(xs, 0.5), pct(xs, 0.9)
    return "%7s %7s" % ("-" if a is None else "%.0f" % a, "-" if b is None else "%.0f" % b)


def report(facts, chain):
    fs = [f for f in facts if f["chain"] == chain]
    sent = [f for f in fs if f["sent_ms"] is not None]
    print("\n==== chain %s: first attempts %d, sent %d" % (chain, len(fs), len(sent)))
    if not sent:
        return
    total = [f["sent_ms"] for f in sent]
    print("decide->broadcast accepted (ms)  p50 p90: %s   by lane %s" % (fmt(total), dict(
        collections.Counter(f["lane"] for f in sent))))
    cats = sorted({k for f in sent for k in f["walls"]})
    p90 = pct(total, 0.9)
    tail = [f for f in sent if f["sent_ms"] >= p90]
    print("%-26s %7s %7s %8s %9s  %s" % ("wall (adds to total)", "p50", "p90", "mean", "share",
                                          "tail rows: largest wall"))
    tot_sum = sum(total)
    for k in cats:
        xs = [f["walls"].get(k, 0.0) for f in sent]
        top = sum(1 for f in tail if max(f["walls"], key=f["walls"].get) == k)
        print("%-26s %s %8.0f %8.1f%%  %d/%d" % (k, fmt(xs), sum(xs) / len(xs),
                                                 100.0 * sum(xs) / tot_sum, top, len(tail)))
    # SAFETY: own duration vs the wait it cost the decision thread.
    sd = [(first_event(f["events"], "evm.token_safety") or {}) for f in sent]
    print("safety own duration %s | wait on path %s | components p50: %s" % (
        fmt([e.get("duration_ms") for e in sd if e]),
        fmt([f["walls"].get("safety_wait", 0.0) for f in sent]),
        {k: round(pct([e.get(k) for e in sd if e.get(k) is not None], 0.5) or 0)
         for k in ("code_ms", "slot_discovery_ms", "sell_quote_ms", "simulation_ms",
                   "curve_state_ms", "tax_ms") if any(e.get(k) is not None for e in sd)}))
    # LADDER: every first ladder that ran (sent or not), per rung.
    rung_t, rung_hit, winners, first_hit, ladder = (collections.defaultdict(list),
                                                    collections.Counter(), collections.Counter(),
                                                    [], [])
    for f in fs:
        q = first_event(f["events"], "evm.quote_initial")
        if not q or not q.get("rungs"):
            continue
        run = q["rungs"][0].get("run_id")
        rs = [r for r in q["rungs"] if r.get("run_id") == run and not r.get("skipped")]
        ladder.append(q.get("duration_ms"))
        hits = [r for r in rs if r.get("hit") and _num(r.get("round_trip_pct")) is not None]
        if hits:
            first_hit.append(min(r["duration_ms"] for r in hits))
            win = max(hits, key=lambda r: _num(r["round_trip_pct"]))
            winners[win["rung"]] += 1
        for r in rs:
            if r.get("blind_reason") in ("ladder_grace", "ladder_deadline"):
                rung_hit[(r["rung"], "cut")] += 1
                continue
            rung_t[r["rung"]].append(r.get("duration_ms"))
            rung_hit[(r["rung"], "hit" if r.get("hit") else "miss")] += 1
    print("first ladder n=%d dur %s | first measured hit %s | winners %s" % (
        len(ladder), fmt(ladder), fmt(first_hit), dict(winners.most_common())))
    for name in sorted(rung_t, key=lambda n: pct(rung_t[n], 0.5) or 0):
        print("   rung %-14s answered p50/p90 %s  hit %3d miss %3d cut %3d" % (
            name, fmt(rung_t[name]), rung_hit[(name, "hit")], rung_hit[(name, "miss")],
            rung_hit[(name, "cut")]))
    # PREFETCH WINDOW: the gap between observing the signal and starting the decision, and how
    # often the same mint was decided earlier (any wallet) on this chain.
    lag = [(_ts(f["started"]) - _ts(f["observed"])) * 1000.0 for f in fs
           if _ts(f["observed"]) and _ts(f["started"])]
    seen, again10, again60 = {}, 0, 0
    for f in sorted(fs, key=lambda f: f["started"]):
        t = _ts(f["started"])
        last = seen.get(f["mint"])
        if last is not None and t is not None:
            again10 += t - last <= 600
            again60 += t - last <= 3600
        seen[f["mint"]] = t
    print("observed->decision start %s (n=%d) | mint decided before on this chain: <=10 min %d, "
          "<=60 min %d of %d" % (fmt(lag), len(lag), again10, again60, len(fs)))


def selftest():
    ev = [{"name": "dex_pair_selection", "elapsed_ms": 200},
          {"name": "timing_gate_balance_read", "elapsed_ms": 300},
          {"name": "evm.balance_usd_snapshot", "elapsed_ms": 301},
          {"name": "evm.quote_initial", "elapsed_ms": 2101, "duration_ms": 1800},
          {"name": "evm.balance_pre", "elapsed_ms": 2130},
          {"name": "evm.token_safety", "elapsed_ms": 2140, "duration_ms": 1500},
          {"name": "evm.entry_preflight", "elapsed_ms": 2170, "outcome": "refused"},
          {"name": "evm.min_out_retry_winner", "elapsed_ms": 3870},
          {"name": "evm.quote_initial", "elapsed_ms": 3870},
          {"name": "evm.entry_preflight", "elapsed_ms": 3900, "outcome": "passed"},
          {"name": "evm.approval", "elapsed_ms": 4200},
          {"name": "evm.swap_signing", "elapsed_ms": 4205},
          {"name": "evm.broadcast", "elapsed_ms": 4400},
          {"name": "evm.confirmation", "elapsed_ms": 4450}]
    w, sent = walls(ev)
    assert sent == 4400, sent
    assert abs(sum(w.values()) - sent) < 1e-6, (w, sent)
    assert w["ladder"] == 1800 and w["safety_wait"] == 10 and w["preflight"] == 30, w
    assert w["rework"] == 1730 and w["approval"] == 300 and w["pre:market_data_hook"] == 200, w
    assert walls(ev[:-2])[1] is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-24T00:00:00")
    ap.add_argument("--chains", default="4663,56")
    ap.add_argument("--db", default=os.environ.get("DB_PATH") or str(REPO / "sniper_tracker.db"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    chains = [c.strip() for c in args.chains.split(",") if c.strip()]
    facts = load(args.since, chains, args.db)
    for chain in chains:
        report(facts, chain)


if __name__ == "__main__":
    main()
