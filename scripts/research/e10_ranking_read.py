#!/usr/bin/env python3
"""E10 activation read: does our own tape's V4 depth rank the pool the entry quote was won on?

Alpha 2026-09-29 (spec docs/superpowers/specs/2026-09-29-memecoin-dexscreener-out-e9-e10-design.md,
D-E10-2 as amended after Gamma's review): switch E10 off on a chain only when, on ENTRIES AND ON
EXITS separately (exits share `_pools_for_mint`), there are >= 20 rankings and the won pool is inside
the phase 2 cut in >= 95% of them. in_cut is the safety number; top_hit is printed beside it.

Read-only: the ledger. A ranking is the `v4_ranking` record behind the pool list a V4 quote used
(fresh, or `cached` from an earlier discovery), next to `won_pool`:
  entry  `evm.quote_initial` in memecoin_entry_attempts.trace_json, one per origin;
  exit   memecoin_positions.exit_timing_json of a funded full close, one per position.
Per ranking:
  top_hit     the pool with the most own-tape depth is the won pool
  in_cut      the won pool is inside `e10_cut`, the cut phase 2 takes, computed by the discovery code
              itself on the same candidates (never replayed here)
  no_depth    the tape measured no positive depth for any candidate (phase 2 ranks on GeckoTerminal)
  ds_rank     DexScreener's rank of the won pool, today's ranking input (None: absent from
              DexScreener's answer or below its top 8, which is all the capped record keeps)

READY needs n >= 20 and in_cut >= 95% on entries AND on exits of the chain. Rankings already recorded
with E10 off count the same way.

    python3 scripts/research/e10_ranking_read.py [--since 2026-09-29T00:00:00] [--list 10]
    python3 scripts/research/e10_ranking_read.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

LEDGER = REPO / "sniper_tracker.db"
CHAINS = ("4663", "8453")
READY_N = 20
READY_IN_CUT = 0.95


def quote_rankings(trace_json):
    """[(v4_ranking, won_pool)] from the attempt's `evm.quote_initial` substages."""
    try:
        events = (json.loads(trace_json or "{}") or {}).get("substage_events") or []
    except (TypeError, ValueError):
        return []
    return [(s["v4_ranking"], s.get("won_pool")) for s in events
            if isinstance(s, dict) and s.get("name") == "evm.quote_initial"
            and isinstance(s.get("v4_ranking"), dict)]


def grade(ranking, won):
    pools = ranking.get("pools") or {}
    own = {pid: p["own"] for pid, p in pools.items() if p.get("own") is not None and p["own"] > 0}
    ds = sorted(((p["ds"], pid) for pid, p in pools.items() if p.get("ds") is not None), reverse=True)
    ds_ids = [pid for _d, pid in ds]
    return {"top_hit": bool(own) and max(own, key=own.get) == won,
            "in_cut": won in (ranking.get("e10_cut") or ()), "no_depth": not own,
            "ds_rank": ds_ids.index(won) + 1 if won in ds_ids else None,
            "n_pools": len(pools), "e10_off": bool(ranking.get("e10_off"))}


def exit_rows_to_grades(rows):
    """Plain position rows (id, network_id, mint, exit_timing_json) -> one graded exit ranking per
    position on the tape chains."""
    out = []
    for r in rows:
        chain = str(r.get("network_id") or "")
        try:
            timing = json.loads(r.get("exit_timing_json") or "{}") or {}
        except (TypeError, ValueError):
            continue
        ranking, won = timing.get("v4_ranking"), timing.get("won_pool")
        if chain not in CHAINS or not isinstance(ranking, dict) or not won:
            continue
        out.append(dict(grade(ranking, won), chain=chain, mint=r.get("mint"), won=won,
                        attempt_id="pos%s" % r.get("id"), role="exit"))
    return out


def rows_to_grades(rows):
    """Plain attempt rows (network_id, origin_event_id, attempt_id, mint, trace_json) -> one graded
    ranking per origin on the tape chains, oldest first. A ranking without a won V4 pool is skipped."""
    out, seen = [], set()
    for r in rows:
        chain = str(r.get("network_id") or "")
        key = (chain, r.get("origin_event_id") or r.get("attempt_id"))
        if chain not in CHAINS or key in seen:
            continue
        for ranking, won in quote_rankings(r.get("trace_json")):
            if not won:
                continue
            seen.add(key)
            out.append(dict(grade(ranking, won), chain=chain, mint=r.get("mint"), won=won,
                            attempt_id=r.get("attempt_id"), role="entry"))
            break
    return out


def role_ready(r, ready_n=READY_N, in_cut=READY_IN_CUT):
    return r["n"] >= ready_n and r.get("in_cut", 0) >= in_cut * r["n"]


def summarize(grades, *, ready_n=READY_N):
    """{chain: {"entry": counts, "exit": counts, "ready": bool}}."""
    out = {}
    for chain in CHAINS:
        roles = {}
        for role in ("entry", "exit"):
            g = [x for x in grades if x["chain"] == chain and x.get("role", "entry") == role]
            c = collections.Counter()
            for x in g:
                c.update(k for k in ("top_hit", "in_cut", "no_depth", "e10_off") if x[k])
                c["ds_top"] += x["ds_rank"] == 1
            roles[role] = dict(c, n=len(g))
        out[chain] = dict(roles, ready=all(role_ready(roles[r], ready_n) for r in ("entry", "exit")))
    return out


def report(args):
    from cap_refusal_outcomes import ro
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with contextlib.closing(ro(LEDGER)) as ledger:
        rows = [dict(r) for r in ledger.execute(
            "SELECT network_id, origin_event_id, attempt_id, mint, trace_json "
            "FROM memecoin_entry_attempts WHERE decision_started_at>=? AND network_id IN (?,?) "
            "AND trace_json LIKE '%\"v4_ranking\"%' ORDER BY decision_started_at", (since, *CHAINS))]
        exits = [dict(r) for r in ledger.execute(
            "SELECT id, network_id, mint, exit_timing_json FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id IN (?,?) AND closed_at>=? "
            "AND exit_timing_json LIKE '%\"v4_ranking\"%' ORDER BY closed_at", (*CHAINS, since))]
    grades = rows_to_grades(rows) + exit_rows_to_grades(exits)
    print("E10 rankings since %s: V4 quotes won on a ranked pool list (entries one per origin, "
          "exits one per funded full close)" % since)
    for chain, s in summarize(grades).items():
        for role in ("entry", "exit"):
            r = s[role]
            print("%-5s %-5s n %3d  won inside the phase 2 cut %d (%s)  own-tape top == won %d  tape had "
                  "no depth %d  DexScreener top == won %d  (recorded with E10 off %d)" % (
                      chain, role, r["n"], r.get("in_cut", 0),
                      "%.0f%%" % (100.0 * r.get("in_cut", 0) / r["n"]) if r["n"] else "-",
                      r.get("top_hit", 0), r.get("no_depth", 0), r.get("ds_top", 0),
                      r.get("e10_off", 0)))
        print("%-5s %s" % (chain, "READY" if s["ready"] else
                           "not ready (need n >= %d and in_cut >= %.0f%% on entries AND exits)"
                           % (READY_N, 100 * READY_IN_CUT)))
    for x in [x for x in grades if not x["in_cut"] or not x["top_hit"]][:args.list]:
        print("  miss %s %s %s won %s in_cut=%s no_depth=%s ds_rank=%s pools=%d" % (
            x["chain"], x["role"], str(x["mint"])[:12], str(x["won"])[:14], x["in_cut"], x["no_depth"],
            x["ds_rank"], x["n_pools"]))
    return 0


def selftest():
    A, B, C = "0x" + "a" * 64, "0x" + "b" * 64, "0x" + "c" * 64

    def pool(gt=None, ds=None, own=None, fee_rank=0):
        return {"gt": gt, "ds": ds, "own": own, "src": "gt", "fee_rank": fee_rank}

    def rank(pools, e10_cut, e10_off=False):
        return {"pools": pools, "e10_off": e10_off, "cut": list(pools), "e10_cut": e10_cut}

    def row(chain, origin, ranking, won, name="evm.quote_initial"):
        ev = {"name": name, "v4_ranking": ranking, "won_pool": won}
        return {"network_id": chain, "origin_event_id": origin, "attempt_id": "a" + origin,
                "mint": "0xm" + origin, "trace_json": json.dumps({"substage_events": [ev]})}
    rows = [row("4663", "1", rank({A: pool(900, 800, 50), B: pool(10, 20, 5)}, [A, B]), A),   # hit
            row("4663", "1", rank({A: pool(900, 800, 50)}, [A]), A),                          # same origin
            row("4663", "2", rank({A: pool(900, 800, 5), B: pool(10, 20, 50)}, [B]), A),      # out of cut
            row("4663", "3", rank({A: pool(900, 800), B: pool(10, 20)}, [A, B]), A),          # no depth
            row("4663", "4", rank({A: pool(own=9)}, [A]), None),                              # no won pool
            row("4663", "5", rank({A: pool(own=9)}, [A]), A, name="evm.quote_miss"),          # not a quote
            row("8453", "6", rank({A: pool(own=0.0), B: pool(own=0.1)}, [B, A], True), A),     # tape zero
            row("56", "7", rank({A: pool(own=1)}, [A]), A)]                                   # not V4
    g = rows_to_grades(rows)
    assert [x["attempt_id"] for x in g] == ["a1", "a2", "a3", "a6"], g
    assert [x["top_hit"] for x in g] == [True, False, False, False]
    assert [x["in_cut"] for x in g] == [True, False, True, True]
    assert [x["no_depth"] for x in g] == [False, False, True, False]
    assert [x["ds_rank"] for x in g] == [1, 1, 1, None]
    assert rows_to_grades([row("4663", "8", {"pools": {A: pool(own=1)}}, A)])[0]["in_cut"] is False, \
        "a record without e10_cut is never counted inside the cut"
    exits = exit_rows_to_grades([
        {"id": 9, "network_id": "4663", "mint": "0xm9", "exit_timing_json": json.dumps(
            {"total_ms": 5, "v4_ranking": rank({A: pool(900, 800, 50)}, [A]), "won_pool": A})},
        {"id": 10, "network_id": "4663", "mint": "0xm10", "exit_timing_json": json.dumps(
            {"total_ms": 5})},                                                   # no ranking
        {"id": 11, "network_id": "4663", "mint": "0xm11", "exit_timing_json": "not json"}])
    assert [(x["attempt_id"], x["role"], x["in_cut"]) for x in exits] == [("pos9", "exit", True)]
    s = summarize(g + exits, ready_n=1)
    assert (s["4663"]["entry"]["n"], s["4663"]["entry"]["in_cut"], s["4663"]["exit"]["n"]) == (3, 2, 1)
    assert s["4663"]["ready"] is False, "entries at 67% in the cut are not ready"

    def graded(role, n, in_cut):
        return [{"chain": "4663", "role": role, "in_cut": i < in_cut, "top_hit": False,
                 "no_depth": False, "e10_off": False, "ds_rank": None} for i in range(n)]
    full_exits = graded("exit", 20, 20)
    assert summarize(graded("entry", 20, 19) + full_exits)["4663"]["ready"] is True, "95% is enough"
    assert summarize(graded("entry", 20, 18) + full_exits)["4663"]["ready"] is False, "90% is not"
    assert summarize(graded("entry", 19, 19) + full_exits)["4663"]["ready"] is False, "n below 20"
    assert summarize(graded("entry", 20, 20))["4663"]["ready"] is False, "entries alone are not ready"
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--list", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
