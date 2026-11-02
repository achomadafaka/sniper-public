#!/usr/bin/env python3
"""E1 on EVM, the forward read (spec 2026-09-29-memecoin-e1-evm-own-mcap-design.md). Read-only.

For one chain since the knob went on: the band decisions in order (a row whose band basis is set,
or a refusal `mcap_unresolved`), the STOP line, and the own stage's measured supply read (outcome,
read ms on a cache miss, cache hits, how long the stage waited past the GET) beside the GET's own
p90. The STOP line (Alpha, 2026-09-29): more than 5% of the first 50 decisions refused
mcap_unresolved -> unset the chain and restart. 8453 activates only after 20 organic miss reads
with p90 under the GET's p90.

    python3 scripts/research/e1_evm_own_mcap_read.py --chain 56 --since 2026-09-29T05:00:00+00:00
"""
import argparse
import contextlib
import json
import os
import sqlite3
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
FIRST_N = 50
STOP_SHARE = 0.05


def stop_flag(outcomes, *, chain):
    """The STOP line for the first FIRST_N decisions (terminal reasons, in order), or None. Pure.
    More than 5% of 50 is 3 or more, so it can fire before the 50th decision."""
    first = list(outcomes)[:FIRST_N]
    n = sum(1 for o in first if o == "mcap_unresolved")
    if n > STOP_SHARE * FIRST_N:
        return ("STOP(E1): %d mcap_unresolved in the first %d decisions on %s (more than 5%% of %d): "
                "unset %s from MEMECOIN_EVM_OWN_MCAP_CHAINS and restart"
                % (n, len(first), chain, FIRST_N, chain))
    return None


def _q(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "n=0"
    return "n=%d p50 %.0f p90 %.0f" % (len(xs), statistics.median(xs), xs[min(len(xs) - 1, int(0.9 * len(xs)))])


def read(conn, chain, since):
    rows = conn.execute(
        "SELECT decision_started_at, terminal_status, terminal_reason, classification_json, trace_json "
        "FROM memecoin_entry_attempts WHERE network_id=? AND decision_started_at>=? "
        "ORDER BY decision_started_at", (chain, since)).fetchall()
    outcomes, bases, supply, get_ms = [], {}, [], []
    for r in rows:
        try:
            facts = (json.loads(r["classification_json"] or "{}").get("facts") or {})
        except ValueError:
            facts = {}
        basis = facts.get("mcap_basis")
        if basis or r["terminal_reason"] == "mcap_unresolved":
            outcomes.append("mcap_unresolved" if r["terminal_reason"] == "mcap_unresolved"
                            else (r["terminal_status"] or "open"))
            bases[basis or "unresolved"] = bases.get(basis or "unresolved", 0) + 1
        try:
            trace = json.loads(r["trace_json"] or "{}")
        except ValueError:
            trace = {}
        md = ((trace.get("stages") or {}).get("market_data") or {}).get("duration_ms")
        if md is not None and md > 20:           # the GET ran (events with an mcap take ~1 ms)
            get_ms.append(md)
        for s in trace.get("substage_events") or []:
            if s.get("name") == "evm_own_supply":
                supply.append(s)
    return {"outcomes": outcomes, "bases": bases, "supply": supply, "get_ms": get_ms,
            "stop": stop_flag(outcomes, chain=chain)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chain", required=True)
    ap.add_argument("--since", required=True)
    args = ap.parse_args()
    with contextlib.closing(sqlite3.connect(
            "file:%s?mode=ro" % (STATE / "sniper_tracker.db"), uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        out = read(conn, args.chain, args.since)
    first = out["outcomes"][:FIRST_N]
    print("E1-EVM read  chain %s  since %s" % (args.chain, args.since))
    print("band decisions %d (first %d: %d mcap_unresolved); bases %s"
          % (len(out["outcomes"]), len(first), first.count("mcap_unresolved"), out["bases"]))
    sup = out["supply"]
    outcomes = {}
    for s in sup:
        outcomes[s.get("outcome")] = outcomes.get(s.get("outcome"), 0) + 1
    misses = [s.get("read_ms") for s in sup if s.get("cache_hit") is False]
    print("own stage: %d reads, outcomes %s, cache hits %d" % (
        len(sup), outcomes, sum(1 for s in sup if s.get("cache_hit") is True)))
    print("  read ms (cache miss): %s" % _q(misses))
    print("  waited ms past the GET: %s" % _q([s.get("waited_ms") for s in sup]))
    print("  GET (market_data stage, GET ran): %s" % _q(out["get_ms"]))
    if len([x for x in misses if x is not None]) >= 20 and out["get_ms"]:
        mp = sorted(x for x in misses if x is not None)
        gp = sorted(out["get_ms"])
        ready = mp[int(0.9 * len(mp))] < gp[int(0.9 * len(gp))]
        print("  activation gate (20 misses, read p90 < GET p90): %s" % ("MET" if ready else "not met"))
    print(out["stop"] or "no STOP(E1)")


if __name__ == "__main__":
    main()
