#!/usr/bin/env python3
"""Would the first-seen chase refusals have made money? DexScreener display captures, EVM.

Pre-registered in docs/audits/2026-09-28-first-seen-chase-refusal-outcomes.md (7cb62247) before any
outcome in this population was computed. Read-only: the ledger and data/memecoin_refusal_captures.db.

Per refused origin the capture holds DexScreener DISPLAY prices every ~25 s (the gate runs before any
quote, so there is no exact price of ours). Entry = the first observation at age <= 30 s x 1.05 (a
fixed round-trip cost allowance); sensitivity: entry at the observation nearest +50 s (the display
lag). TP +25% / stop -35% on later observations, else the observation nearest +300 s (the grading
of scripts/research/cap_refusal_outcomes.py). Comparator: EVM funded fills whose chase_first_seen
substage passed, by price_now / first_price, realized / notional (a real cost basis).

    python3 scripts/research/first_seen_refusal_outcomes.py [--since 2026-09-14T00:00:00]
    python3 scripts/research/first_seen_refusal_outcomes.py --selftest
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
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
from cap_refusal_outcomes import grade, mint_ci, ro  # noqa: E402

LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
COST = 1.05
RATIO = re.compile(r"^coin ran ([0-9.]+)x above first-seen price")
BANDS = ((3, 5), (5, 10), (10, 1e9))


def path_returns(obs, entry_age):
    """obs: [(age_s, price)] sorted. Entry = the observation at <= 30 s (entry_age None) or nearest
    entry_age (within 20 s), x COST. Returns [(age, ret)] after the entry, ages re-based to the
    refusal, or None when no entry observation exists."""
    if entry_age is None:
        first = [(a, p) for a, p in obs if a <= 30 and p]
        if not first:
            return None
        e_age, e_px = first[0]
    else:
        near = [(abs(a - entry_age), a, p) for a, p in obs if abs(a - entry_age) <= 20 and p]
        if not near:
            return None
        _d, e_age, e_px = min(near)
    base = e_px * COST
    return [(a, p / base - 1.0) for a, p in obs if a > e_age and p]


def refusals(since):
    with contextlib.closing(ro(LEDGER)) as c:
        rows = c.execute("SELECT origin_event_id, network_id, mint, decision_started_at, terminal_reason "
                         "FROM memecoin_entry_attempts WHERE decision_started_at>=? AND terminal_reason LIKE "
                         "'coin ran %first-seen%' ORDER BY decision_started_at", (since,)).fetchall()
    out = {}
    for r in rows:
        m = RATIO.match(r["terminal_reason"])
        if m and r["origin_event_id"] not in out:
            out[r["origin_event_id"]] = {"origin": r["origin_event_id"], "chain": r["network_id"], "mint": r["mint"],
                                         "at": r["decision_started_at"], "ratio": float(m.group(1))}
    return list(out.values())


def captures(conn, origin):
    return [(float(a), float(p)) for a, p in conn.execute(
        "SELECT age_s, quote_price FROM memecoin_refusal_captures WHERE origin_event_id=? AND gate='chase_first_seen' "
        # One series per origin: an EVM exact capture (2026-09-28) is a second series on another basis.
        "AND capture_id NOT LIKE '%#exact' "
        "AND quality='ok' AND quote_price IS NOT NULL AND age_s IS NOT NULL ORDER BY age_s", (origin,))]


def admitted(since):
    out = []
    with contextlib.closing(ro(LEDGER)) as c:
        for net, tj, pnl, notional, mint in c.execute(
                "SELECT a.network_id, a.trace_json, p.realized_pnl, p.entry_notional, p.mint FROM memecoin_entry_attempts a "
                "JOIN memecoin_positions p ON p.id=a.position_id WHERE a.network_id!='solana' AND a.decision_started_at>=? "
                "AND p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
                "AND a.trace_json LIKE '%\"chase_first_seen\"%'", (since,)):
            ev = next((s for s in json.loads(tj).get("substage_events") or [] if s.get("name") == "chase_first_seen"), {})
            f, n = ev.get("first_price"), ev.get("price_now")
            if f and n and notional and ev.get("allowed", True):
                out.append((net, float(n) / float(f), pnl, pnl / notional, mint))
    return out


def line(label, rows):
    """rows: [(mint, ret)]."""
    if not rows:
        print("  %-30s n=0" % label)
        return
    rets = [r for _m, r in rows]
    lo, hi = mint_ci(rows)
    print("  %-30s n=%3d mean %+6.1f%% CI [%+.1f, %+.1f] median %+.1f%% wins %d" % (
        label, len(rows), 100 * sum(rets) / len(rets), 100 * lo, 100 * hi,
        100 * sorted(rets)[len(rets) // 2], sum(r > 0 for r in rets)))


def report(args):
    refs = refusals(args.since)
    print("first-seen chase refusals since %s: %d origins %s" % (
        args.since, len(refs), dict(collections.Counter(r["chain"] for r in refs))))
    with contextlib.closing(ro(CAPTURES)) as cap:
        for r in refs:
            r["obs"] = captures(cap, r["origin"])
    print("with a capture: %s" % dict(collections.Counter(r["chain"] for r in refs if r["obs"])))
    for label, entry in (("ENTRY first obs (<= 30 s) x1.05", None), ("ENTRY at +50 s x1.05 (lag sensitivity)", 50.0)):
        graded = []
        for r in refs:
            rets = path_returns(r["obs"], entry) if r["obs"] else None
            v, how = grade(rets) if rets else (None, "no_entry")
            graded.append((r, v, how))
        print("\n%s: grades %s" % (label, dict(collections.Counter(h for _r, _v, h in graded))))
        dec = [(r, v) for r, v, h in graded if v is not None]
        line("PRIMARY EVM pooled", [(r["mint"], v) for r, v in dec])
        for ch in sorted({r["chain"] for r, _v in dec}):
            line("chain %s" % ch, [(r["mint"], v) for r, v in dec if r["chain"] == ch])
        for lo, hi in BANDS:
            line("ratio %g-%s x" % (lo, "" if hi > 1e8 else "%g" % hi),
                 [(r["mint"], v) for r, v in dec if lo <= r["ratio"] < hi])
    adm = admitted(args.since)
    print("\nADMITTED EVM fills (chase_first_seen passed), realized / notional:")
    for lo, hi in ((0, 1.0001), (1.0001, 2), (2, 3), (3, 1e9)):
        line("ratio %g-%g x" % (lo, hi if hi < 1e8 else 99), [(m, r) for _n, x, _p, r, m in adm if lo <= x < hi])


def selftest():
    obs = [(3, 1.0), (28, 1.1), (53, 1.4), (78, 0.5)]
    r = path_returns(obs, None)
    assert abs(r[0][1] - (1.1 / 1.05 - 1)) < 1e-9 and r[0][0] == 28           # entry at 3 s x1.05
    assert grade(r)[1] == "tp"                                                  # 1.4/1.05 = +33%
    r50 = path_returns(obs, 50.0)
    assert r50[0][0] == 78 and grade(r50)[1] == "stop"                          # entry 1.4x1.05, then 0.5
    assert path_returns([(40, 1.0)], None) is None and path_returns([(100, 1.0)], 50.0) is None
    assert RATIO.match("coin ran 15.3x above first-seen price (cap 3.0x)").group(1) == "15.3"
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
