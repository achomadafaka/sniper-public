#!/usr/bin/env python3
"""Reproducible artifact for the coin-type lift claim. READ-ONLY.

Exists because the claim was published three times with three different numbers and no committed
query. Every figure in the pre-registration must come from this script at the frozen `as_of`.

Four properties are load-bearing, and each one was got wrong in an earlier revision:

1. **Consensus is counted at our RECEIPT of the signal, strictly, and scoped to the chain.**
   Two leaks were closed here in turn. An early version added 60s to the signal to align with the
   price baseline — a decision cannot see the next minute's buyers. The next compared `created_at`,
   the trader's own buy time; events arrive late (p99 lag 403s), so a buy created earlier can be
   unreceived when we decide, again crediting knowledge we lacked. The count also ignored
   `network_id`, letting the same mint string on another chain contribute buyers. Each correction
   shrank the cell: 36 -> 20 -> 10 -> 6 mints.
2. **The episode is selected BEFORE its marks are validated.** Joining marks first and then taking
   the earliest survivor substitutes a later signal whenever the first lacks marks — and marks go
   missing precisely when a coin dies, so that selects on the outcome.
3. **Freshness is enforced and named**: marks must be observed before `as_of`, at or after their
   target age, with a non-null lateness inside an explicit bound.
4. **The null is hypergeometric, not binomial.** The cell is a SUBSET of the pool, so the pool
   rate is not an independent base rate. The binomial p overstates significance.

THE UNIT IS THE MINT. `--unit signal` reproduces the superseded `(wallet, mint)` inflation only to
show its size; it must never be quoted as a result.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
AS_OF = "2026-08-11T14:00:00+00:00"
MAX_MCAP, CONS_LO, CONS_HI = 1_000_000.0, 4, 8
BASE_S, TARGET_S, HIT_PCT = 60, 300, 20.0
LATE_BASE_S, LATE_TARGET_S = 60.0, 135.0        # authoritative freshness bound


def parse(x):
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def hypergeom_ge(k, n, K, N):
    """P(X >= k) drawing n from N containing K successes. The cell is a subset of the pool, so
    this — not the binomial — is the null for 'does this cell hold more hits than its share'."""
    lo, hi = max(0, n - (N - K)), min(n, K)
    tot = math.comb(N, n)
    return sum(math.comb(K, i) * math.comb(N - K, n - i) for i in range(max(k, lo), hi + 1)) / tot


def load(conn, as_of, unit, chain="solana"):
    """STAGE 1 selects episodes blind to marks. STAGE 2 validates the selected episode's own
    marks and drops it if they fail — never substitutes another signal."""
    sel = {}
    for r in conn.execute(
        """SELECT o.outcome_id, o.source_user_id, o.mint, o.signal_observed_at,
                  e.their_market_cap AS mcap
           FROM memecoin_signal_outcomes_v2 o
           JOIN memecoin_events e ON e.fomo_event_id = o.upstream_event_id
           WHERE COALESCE(e.is_first_buy, 0) = 1 AND o.network_id = ?
             AND o.signal_observed_at < ? AND COALESCE(o.mint,'') != ''
             AND e.their_market_cap > 0""", (chain, as_of)):
        key = (r["source_user_id"], r["mint"]) if unit == "signal" else r["mint"]
        ts = str(r["signal_observed_at"])
        if key not in sel or ts < sel[key]["ts"]:
            sel[key] = {"oid": r["outcome_id"], "ts": ts, "mint": r["mint"],
                        "mcap": float(r["mcap"])}

    marks = defaultdict(dict)
    for r in conn.execute(
        """SELECT outcome_id, target_s, price FROM memecoin_horizon_marks_v2
           WHERE status='observed' AND price > 0 AND target_s IN (?, ?)
             AND quality IN ('ok','probe_ok')
             AND observed_at IS NOT NULL AND observed_at < ?
             AND lateness_s IS NOT NULL AND actual_age_s >= target_s
             AND lateness_s <= CASE target_s WHEN ? THEN ? ELSE ? END""",
            (BASE_S, TARGET_S, as_of, BASE_S, LATE_BASE_S, LATE_TARGET_S)):
        marks[r["outcome_id"]][int(r["target_s"])] = float(r["price"])

    # keyed by (mint, chain) and timed by RECEIPT: what we knew, when we knew it
    buys = defaultdict(list)
    for r in conn.execute("SELECT mint, network_id, source_user_id, received_at "
                          "FROM memecoin_events WHERE side='buy' "
                          "AND received_at IS NOT NULL AND received_at < ?", (as_of,)):
        if r["mint"]:
            buys[(r["mint"], r["network_id"])].append((str(r["received_at"]), r["source_user_id"]))

    out = []
    for e in sel.values():
        hs = marks.get(e["oid"], {})
        if BASE_S not in hs or TARGET_S not in hs or hs[BASE_S] <= 0:
            continue                     # dropped, never replaced
        # STRICTLY at OUR RECEIPT of the signal, same chain. `signal_observed_at` is already
        # `ev.received_at`, so it is the decision moment.
        e["cons"] = len({u for t, u in buys.get((e["mint"], chain), []) if t < e["ts"]})
        e["ret"] = 100.0 * (hs[TARGET_S] / hs[BASE_S] - 1.0)
        out.append(e)
    return out


def summarise(eps):
    pool = [v for v in eps if v["mcap"] < MAX_MCAP]
    cell = [v for v in pool if CONS_LO <= v["cons"] <= CONS_HI]
    if not pool or not cell:
        return None
    N, K = len(pool), sum(v["ret"] >= HIT_PCT for v in pool)
    n, k = len(cell), sum(v["ret"] >= HIT_PCT for v in cell)
    base = K / N
    return {"pool": N, "cell": n, "mints": len({v["mint"] for v in cell}), "hits": k,
            "hit": k / n, "base": base, "lift": (k / n) / base if base else 0.0,
            "p_hyper": hypergeom_ge(k, n, K, N)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="coin-type lift artifact (read-only)")
    ap.add_argument("--as-of", default=AS_OF)
    ap.add_argument("--unit", choices=("mint", "signal", "both"), default="both")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    print(f"as_of={a.as_of}  filter: mcap<${MAX_MCAP:,.0f} AND {CONS_LO}-{CONS_HI} prior buyers")
    print(f"consensus: distinct wallets RECEIVED before this signal, same chain; marks fresh within "
          f"{LATE_BASE_S:.0f}s/{LATE_TARGET_S:.0f}s\n")
    print(f"{'unit':<10}{'pool':<8}{'cell':<8}{'mints':<8}{'hit':<9}{'base':<9}{'lift':<8}"
          f"{'p (hyper)'}")
    for unit in (("mint", "signal") if a.unit == "both" else (a.unit,)):
        r = summarise(load(conn, a.as_of, unit))
        if not r:
            print(f"{unit:<10}no data"); continue
        note = "  <- AUTHORITATIVE" if unit == "mint" else "  <- SUPERSEDED (inflated)"
        print(f"{unit:<10}{r['pool']:<8}{r['cell']:<8}{r['mints']:<8}{r['hit']*100:>5.1f}%   "
              f"{r['base']*100:>5.1f}%   {r['lift']:>4.2f}x   {r['p_hyper']:.4f}{note}")
    print("\n12 feature cells were searched to find this one; no p here carries a multiplicity\n"
          "correction. The retrospective case is WEAK. This is not a funding signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
