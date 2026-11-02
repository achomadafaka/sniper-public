#!/usr/bin/env python3
"""What would the Solana impact-cap and round-trip-cap refusals have made? Exact route, both legs.

Pre-registered in docs/audits/2026-09-28-impact-roundtrip-cap-outcomes.md (5af3fc27) before any
outcome in this population was computed. Read-only: the ledger and data/memecoin_refusal_captures.db.

Per refused origin, the refusal capture holds an exact-route BUY of the clip about 3 s after the
refusal and exact-route SELLS of the same tokens about every 25 s. Return = sell_usdc / clip - 1.
TP at the first observation >= +25%, stop at the first <= -35%, else the observation nearest +300 s
(within 20 s); none of those = censored. Plain marks at +60 s and +300 s are printed beside it.
Comparator: admitted Solana fills by their own roundtrip_cap substage cost_pct, realized / notional.

    python3 scripts/research/cap_refusal_outcomes.py [--since 2026-09-14T00:00:00]
    python3 scripts/research/cap_refusal_outcomes.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import contextlib
import json
import random
import re
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
TP, STOP, MARK_S, TOL_S = 0.25, -0.35, 300.0, 20.0
RT = re.compile(r"^round trip ([0-9.]+)% over ([0-9.]+)% cap")
IMP = re.compile(r"^price impact ([0-9.]+)% over ([0-9.]+)% cap")
BANDS = ((0, 2), (2, 5), (5, 8), (8, 10), (10, 15), (15, 20), (20, 1e9))


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def band(x):
    return next("%g-%s" % (lo, "" if hi > 1e8 else "%g" % hi) for lo, hi in BANDS if lo <= x < hi)


def grade(obs):
    """obs: [(age_s, ret)] sorted by age. (ret, how) with how in tp/stop/mark, or (None, 'censored')."""
    for age, r in obs:
        if age > MARK_S + TOL_S:
            break
        if r >= TP:
            return r, "tp"
        if r <= STOP:
            return r, "stop"
    near = [(abs(a - MARK_S), r) for a, r in obs if abs(a - MARK_S) <= TOL_S]
    return (min(near)[1], "mark") if near else (None, "censored")


def mark(obs, h):
    near = [(abs(a - h), r) for a, r in obs if abs(a - h) <= TOL_S]
    return min(near)[1] if near else None


def mint_ci(rows, n=4000, seed=20260928):
    """rows: [(mint, value)]. 95% CI of the mean, resampling mints."""
    by = collections.defaultdict(list)
    for m, v in rows:
        by[m].append(v)
    keys, rnd, means = list(by), random.Random(seed), []
    if not keys:
        return None, None
    for _ in range(n):
        xs = [x for k in (keys[rnd.randrange(len(keys))] for _ in keys) for x in by[k]]
        means.append(sum(xs) / len(xs))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def refusals(since):
    with contextlib.closing(ro(LEDGER)) as c:
        rows = c.execute(
            "SELECT attempt_id, origin_event_id, mint, decision_started_at, terminal_reason FROM memecoin_entry_attempts "
            "WHERE network_id='solana' AND decision_started_at>=? AND (terminal_reason LIKE 'round trip%' "
            "OR terminal_reason LIKE 'price impact%') ORDER BY decision_started_at", (since,)).fetchall()
    out = {}
    for r in rows:
        m1, m2 = RT.match(r["terminal_reason"]), IMP.match(r["terminal_reason"])
        m = m1 or m2
        if not m or r["origin_event_id"] in out:
            continue
        out[r["origin_event_id"]] = {"origin": r["origin_event_id"], "mint": r["mint"], "at": r["decision_started_at"],
                                     "cap": "roundtrip_cap" if m1 else "impact_cap", "cost": float(m.group(1)),
                                     "cap_pct": float(m.group(2))}
    return list(out.values())


def capture_obs(conn, origin):
    rows = conn.execute("SELECT age_s, quote_side, quote_input_usd, token_qty_atomic, quote_output_atomic "
                        "FROM memecoin_refusal_captures WHERE network_id='solana' AND origin_event_id=? "
                        "AND quality='ok' ORDER BY age_s", (origin,)).fetchall()
    buy = next((r for r in rows if r["quote_side"] == "buy" and r["quote_input_usd"]), None)
    if buy is None:
        return None, []
    clip = float(buy["quote_input_usd"])
    obs = [(float(r["age_s"]), int(r["quote_output_atomic"]) / 1e6 / clip - 1.0) for r in rows
           if r["quote_side"] == "sell" and r["token_qty_atomic"] == buy["token_qty_atomic"]
           and r["quote_output_atomic"] and r["age_s"] is not None]
    return clip, obs


def admitted(since):
    out = []
    with contextlib.closing(ro(LEDGER)) as c:
        for tj, pnl, notional, mint in c.execute(
                "SELECT a.trace_json, p.realized_pnl, p.entry_notional, p.mint FROM memecoin_entry_attempts a "
                "JOIN memecoin_positions p ON p.id=a.position_id WHERE a.network_id='solana' AND "
                "a.decision_started_at>=? AND p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
                "AND a.trace_json LIKE '%\"roundtrip_cap\"%'", (since,)):
            ev = next((s for s in json.loads(tj).get("substage_events") or [] if s.get("name") == "roundtrip_cap"), None)
            if ev and ev.get("cost_pct") is not None and notional:
                out.append((float(ev["cost_pct"]), pnl, pnl / notional, mint))
    return out


def summary(label, rows):
    """rows: [(mint, ret, usd)] decided rows."""
    if not rows:
        print("  %-34s n=0" % label)
        return
    rets = [r for _m, r, _u in rows]
    lo, hi = mint_ci([(m, r) for m, r, _u in rows])
    print("  %-34s n=%3d mean %+6.1f%% CI [%+.1f, %+.1f]  sum $%+8.2f  wins %d" % (
        label, len(rows), 100 * sum(rets) / len(rets), 100 * lo, 100 * hi, sum(u for _m, _r, u in rows),
        sum(r > 0 for r in rets)))


def report(args):
    refs = refusals(args.since)
    print("Solana cap refusals since %s: %d origins (%s)" % (args.since, len(refs),
          dict(collections.Counter(r["cap"] for r in refs))))
    with contextlib.closing(ro(CAPTURES)) as cap:
        for r in refs:
            r["clip"], r["obs"] = capture_obs(cap, r["origin"])
    for name in ("roundtrip_cap", "impact_cap"):
        rs = [r for r in refs if r["cap"] == name]
        have = [r for r in rs if r["clip"] and r["obs"]]
        graded = [(r, *grade(r["obs"])) for r in have]
        how = collections.Counter(h for _r, _v, h in graded)
        print("\n%s: refusals %d, with capture %d, graded %s" % (name, len(rs), len(have), dict(how)))
        dec = [(r["mint"], v, v * r["clip"]) for r, v, h in graded if v is not None]
        print("  PRIMARY TP25/SL35 else +300 s mark:")
        summary("all", dec)
        for lo, hi in BANDS:
            b = [(r["mint"], v, v * r["clip"]) for r, v, h in graded if v is not None and lo <= r["cost"] < hi]
            if b:
                summary("measured cost %s%%" % band(lo), b)
        for h in (60.0, 300.0):
            ms = [(r["mint"], v, v * r["clip"]) for r in have for v in [mark(r["obs"], h)] if v is not None]
            summary("plain mark +%ds" % h, ms)
    adm = admitted(args.since)
    print("\nADMITTED Solana fills by their own roundtrip_cap cost_pct (realized / notional):")
    for lo, hi in BANDS:
        b = [(m, r, p) for c, p, r, m in adm if lo <= c < hi]
        if b:
            summary("cost %s%%" % band(lo), b)


def selftest():
    assert grade([(25, 0.1), (50, 0.26), (75, -0.5)]) == (0.26, "tp")
    assert grade([(25, -0.36), (50, 0.3)]) == (-0.36, "stop")
    assert grade([(25, 0.1), (295, -0.05), (330, 0.4)]) == (-0.05, "mark")
    assert grade([(25, 0.1), (200, 0.0)]) == (None, "censored")
    assert grade([(330, 0.4)]) == (None, "censored")            # after the mark window, a TP does not count
    assert mark([(55, 0.1), (70, 0.2)], 60) == 0.1 and mark([(100, 0.1)], 60) is None
    assert band(9.5) == "8-10" and band(25) == "20-"
    assert RT.match("round trip 14.2% over 10.0% cap (sell back $96 of $112)").group(1) == "14.2"
    lo, hi = mint_ci([("a", 0.1), ("b", 0.2), ("c", 0.3)])
    assert 0.1 <= lo <= hi <= 0.3
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-14T00:00:00")
    a = ap.parse_args()
    return selftest() if a.selftest else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
