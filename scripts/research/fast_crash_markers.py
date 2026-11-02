#!/usr/bin/env python3
"""Fast-crash stops: does any entry-time trait separate the closes that lost >= 50% within 120 s?

Pre-registered in docs/audits/2026-09-28-fast-crash-stops.md (f3d07c00) before any trait was compared
with the outcome. Ledger only (read-only URI), no network.

    python3 scripts/research/fast_crash_markers.py [--since 2026-09-14]
    python3 scripts/research/fast_crash_markers.py --selftest
"""
import argparse
import contextlib
import json
import random
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
LEDGER = "file:%s?mode=ro" % (REPO / "sniper_tracker.db")
CRASH_RET, CRASH_S = -0.50, 120.0
DRAWS, SEED = 4000, 20260928
TRAITS = ("entry_premium", "turnover_h1", "txns_m5", "size_over_liq", "coin_age_s", "roundtrip_cost")
BONF = 1 - 0.05 / (len(TRAITS) + 1)          # 7 traits in the family: 99.29%
NO_ROUTE = ("no planned-quantity sell route", "no_route")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def traits_of(row, ev, facts):
    num = lambda v: float(v) if isinstance(v, (int, float)) else None
    mcap = num(facts.get("volume_mcap")) or num(facts.get("effective_mcap"))
    h1, liq, size = num(facts.get("volume_h1")), num(facts.get("liquidity")), num(row["their_size_usd"])
    return {"entry_premium": (row["entry_price"] / row["their_price"] - 1) if row["entry_price"] and row["their_price"] else None,
            "turnover_h1": h1 / mcap if h1 is not None and mcap else None,
            "txns_m5": num((ev.get("volume_gate") or {}).get("txns")),
            "size_over_liq": size / liq if size is not None and liq else None,
            "coin_age_s": num(row["entry_token_age_s"]),
            "roundtrip_cost": num((ev.get("roundtrip_cap") or {}).get("cost_pct"))}


def load(since):
    from sniper.persistence.db_memecoin import robot_performance_sql
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT p.id, p.mint, p.signal_source, p.source_handle, p.opened_recorded_at, p.closed_at, p.realized_pnl, "
            "p.entry_notional, p.entry_price, p.their_price, p.their_size_usd, p.entry_token_age_s, p.exit_reason, "
            "a.trace_json, a.classification_json FROM memecoin_positions p "
            "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            f"WHERE p.execution_mode='real' AND p.network_id='solana' AND p.is_open=0 AND p.opened_at>=? "
            f"AND p.realized_pnl IS NOT NULL AND p.entry_notional>0 AND p.opened_recorded_at IS NOT NULL "
            f"AND {robot_performance_sql('p')}", (since,)).fetchall()
        out = []
        for r in rows:
            ev = {}
            for e in (json.loads(r["trace_json"] or "{}").get("substage_events") or []):
                ev.setdefault(e.get("name"), e)
            facts = json.loads(r["classification_json"] or "{}").get("facts") or {}
            fill = ts(r["opened_recorded_at"])
            lo = (fill - timedelta(seconds=600)).isoformat()
            prior = c.execute(
                "SELECT terminal_reason FROM memecoin_entry_attempts WHERE network_id='solana' AND mint=? "
                "AND decision_started_at>=? AND decision_started_at<=?", (r["mint"], lo, fill.isoformat())).fetchall()
            ret = r["realized_pnl"] / r["entry_notional"]
            held = (ts(r["closed_at"]) - fill).total_seconds()
            out.append({"id": r["id"], "lane": r["signal_source"], "handle": r["source_handle"], "mint": r["mint"],
                        "ret": ret, "held_s": held, "pnl": r["realized_pnl"], "exit": r["exit_reason"],
                        "crash": ret <= CRASH_RET and held <= CRASH_S,
                        "prior_no_route": int(any(any(k in (p[0] or "") for k in NO_ROUTE) for p in prior)),
                        **traits_of(r, ev, facts)})
    return out


def auc(pos, neg):
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def ci(fn, rows, level, rng):
    vals = []
    for _ in range(DRAWS):
        v = fn([rows[rng.randrange(len(rows))] for _ in rows])
        if v is not None:
            vals.append(v)
    vals.sort()
    a = (1 - level) / 2
    return vals[int(a * len(vals))], vals[min(len(vals) - 1, int((1 - a) * len(vals)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-14T00:00:00")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        assert auc([3, 4], [1, 2]) == 1.0 and auc([1], [1]) == 0.5 and auc([], [1]) is None
        t = traits_of({"entry_price": 1.2, "their_price": 1.0, "their_size_usd": 500.0, "entry_token_age_s": 60},
                      {"volume_gate": {"txns": 40}, "roundtrip_cap": {"cost_pct": 7.5}},
                      {"volume_h1": 3000, "volume_mcap": 30000, "liquidity": 10000})
        assert abs(t["entry_premium"] - 0.2) < 1e-12 and abs(t["turnover_h1"] - 0.1) < 1e-12
        assert t["size_over_liq"] == 0.05 and t["txns_m5"] == 40.0 and t["roundtrip_cost"] == 7.5
        print("selftest ok")
        return
    rows = load(a.since)
    rng = random.Random(SEED)
    crash = [r for r in rows if r["crash"]]
    print("funded Solana closes since %s: n=%d; CRASH (<= %d%% within %ds of the fill) n=%d" % (
        a.since, len(rows), CRASH_RET * 100, CRASH_S, len(crash)))
    for lane in sorted({r["lane"] for r in rows}):
        lr = [r for r in rows if r["lane"] == lane]
        cr = [r for r in lr if r["crash"]]
        print("  %-16s closes %3d crashes %2d  crash pnl $%+.2f" % (lane, len(lr), len(cr), sum(r["pnl"] for r in cr)))
    print("  crash realized returns: %s" % ", ".join("%+.0f%%" % (100 * r["ret"]) for r in sorted(crash, key=lambda r: r["ret"])))
    print("  crash exit reasons: %s" % dict(__import__("collections").Counter(r["exit"] for r in crash)))
    print("\nPRIMARY: AUC(trait -> CRASH); FOUND only if the Bonferroni %.2f%% CI excludes 0.5" % (100 * BONF))
    for t in TRAITS:
        sub = [r for r in rows if r[t] is not None]
        pos = [r[t] for r in sub if r["crash"]]
        neg = [r[t] for r in sub if not r["crash"]]
        a_ = auc(pos, neg)
        if a_ is None:
            print("  %-15s n=%d crashes=%d  no AUC" % (t, len(sub), len(pos)))
            continue
        f = lambda s: auc([r[t] for r in s if r["crash"]], [r[t] for r in s if not r["crash"]])
        lo95, hi95 = ci(f, sub, 0.95, rng)
        lob, hib = ci(f, sub, BONF, rng)
        found = lob > 0.5 or hib < 0.5
        print("  %-15s n=%3d crashes=%2d AUC %.3f  95%% [%.3f, %.3f]  Bonf [%.3f, %.3f]  crash median %s vs other %s  -> %s" % (
            t, len(sub), len(pos), a_, lo95, hi95, lob, hib, "%.4g" % statistics.median(pos), "%.4g" % statistics.median(neg),
            "FOUND" if found else "not found"))
    with_, without = [r for r in rows if r["prior_no_route"]], [r for r in rows if not r["prior_no_route"]]
    rate = lambda s: sum(r["crash"] for r in s) / len(s) if s else None
    d = lambda s: (lambda a1, b1: None if a1 is None or b1 is None else a1 - b1)(
        rate([r for r in s if r["prior_no_route"]]), rate([r for r in s if not r["prior_no_route"]]))
    lo, hi = ci(d, rows, BONF, rng) if with_ else (None, None)
    print("  prior_no_route  with %d (crash rate %s), without %d (crash rate %s); diff Bonf CI %s -> %s" % (
        len(with_), "%.3f" % rate(with_) if with_ else "na", len(without), "%.3f" % rate(without),
        "[%.3f, %.3f]" % (lo, hi) if lo is not None else "na",
        "FOUND" if lo is not None and (lo > 0 or hi < 0) else "not found"))
    print("\n  crash rows:")
    for r in sorted(crash, key=lambda r: r["held_s"]):
        print("   %s %-15s %-14s ret %+.0f%% held %.0fs %s prem %s turn %s txns %s size/liq %s age %s rt %s noroute %d" % (
            r["id"], r["lane"], (r["handle"] or "")[:14], 100 * r["ret"], r["held_s"], r["exit"],
            *(("%.3g" % r[k]) if r[k] is not None else "na" for k in TRAITS), r["prior_no_route"]))


if __name__ == "__main__":
    main()
