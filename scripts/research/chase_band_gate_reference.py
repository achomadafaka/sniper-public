#!/usr/bin/env python3
"""Funded Solana closes by entry-premium band, per lane, on the fill reference AND the gate reference.

Question (operator, via Alpha, 2026-09-28): MEMECOIN_REAL_CHASE_MAX_PCT 30 -> 20? Is the 20-30%
band's loss real, and what would a 20% cap actually cut?

The two references are not the same quantity:
  * `total_copy_drift_pct` = our FILL / the source price - 1 (entry_attribution.buy_drift). It
    carries our execution slippage after the gate decided.
  * The GATE decides on something else per lane:
      - J7 (`follow_activity`) and on-chain (`onchain_watch`): the exact-quote chase check in
        solana_swap_executor compares our QUOTE with the source's AVERAGE fill. Its value is the
        trace mark `chase_quote_confirmation.gap_pct`, with the cap in force as `cap_pct`
        (the last mark wins: a fresh re-quote writes a second one).
      - slot lane: the cap is signed into the transaction as a minimum output against the source's
        POST-trade price (`slot_price_limit`), so the fill itself is bounded; its reference premium
        is `total_copy_drift_pct`, which is measured against that same post-trade price.
  A 20% cap would cut the fills whose GATE value is above 20, not those whose fill drift is.

Population: execution_mode='real', network_id='solana', closed, opened in [--since, --until),
robot_performance_sql() (no fomo-priced fill, no manual). Returns are realized_pnl/entry_notional.
Bootstrap 95% CIs (seed 20260928, 4000 draws). "trim3" drops the 3 best and 3 worst trades by
dollars. Read-only.

    python3 scripts/research/chase_band_gate_reference.py --until 2026-09-28T10:40:00Z
    python3 scripts/research/chase_band_gate_reference.py --selftest
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import json
import random
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.persistence.db_memecoin import robot_performance_sql  # noqa: E402
from sniper.util.envfiles import load_env_files  # noqa: E402
from sniper.util.state_paths import state_root  # noqa: E402

load_env_files(str(state_root()))

BANDS = [(None, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, None)]
SEED, DRAWS = 20260928, 4000


def band_of(v):
    if v is None:
        return "unknown"
    for lo, hi in BANDS:
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return f"<{hi}" if lo is None else f"{lo}+" if hi is None else f"{lo}-{hi}"
    raise AssertionError(v)


def gate_record(lane, trace_json, total_drift):
    """(gate premium %, cap %, note) as the gate saw it."""
    if lane == "slot_lane":
        return total_drift, 30.0 if total_drift is not None else None, "signed min-out vs post-trade"
    if not trace_json:
        return None, None, "no trace"
    events = json.loads(trace_json).get("substage_events") or []
    conf = [e for e in events if e.get("name") == "chase_quote_confirmation"]
    if conf:
        return conf[-1].get("gap_pct"), conf[-1].get("cap_pct"), "exact quote vs source avg"
    skip = [e for e in events if e.get("name") == "chase_quote_skipped"]
    if skip:
        return None, skip[-1].get("cap_pct"), f"gate skipped: {skip[-1].get('reason')}"
    return None, None, "no chase mark"


def boot(values, stat, rng):
    n = len(values)
    d = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(DRAWS))
    return d[int(0.025 * DRAWS)], d[int(0.975 * DRAWS) - 1]


def table(title, rows, key, rng):
    print(f"\n{title}")
    print(f"  {'lane':16} {'band':7} {'n':>4} {'sum$':>9} {'95% CI':>19} {'med ret%':>8} {'95% CI':>17} "
          f"{'trim3 sum$':>10} {'trim3 med%':>10}")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["lane"], band_of(key(r)))].append(r)
    order = {b: i for i, b in enumerate([band_of(lo if lo is not None else -1) for lo, _ in BANDS] + ["unknown"])}
    for lane, band in sorted(groups, key=lambda k: (k[0], order.get(k[1], 99))):
        g = groups[(lane, band)]
        pnl = [r["pnl"] for r in g]
        ret = [r["ret"] for r in g]
        line = f"  {lane:16} {band:7} {len(g):>4} {sum(pnl):>+9.2f}"
        if len(g) >= 5:
            slo, shi = boot(pnl, sum, rng)
            mlo, mhi = boot(ret, statistics.median, rng)
            line += (f" [{slo:>+8.2f},{shi:>+8.2f}] {statistics.median(ret):>+8.1f} "
                     f"[{mlo:>+6.1f},{mhi:>+7.1f}]")
        else:
            line += f" {'n<5, no CI':>19} {statistics.median(ret):>+8.1f} {'':>17}"
        if len(g) > 6:
            keep = sorted(g, key=lambda r: r["pnl"])[3:-3]
            line += (f" {sum(r['pnl'] for r in keep):>+10.2f} "
                     f"{statistics.median(r['ret'] for r in keep):>+10.1f}")
        else:
            line += f" {'n<=6':>10} {'':>10}"
        print(line)


def main():
    if "--selftest" in sys.argv:
        assert band_of(19.99) == "15-20" and band_of(20.0) == "20-25" and band_of(30.0) == "30+"
        assert band_of(-0.1) == "<0" and band_of(None) == "unknown"
        print("selftest ok")
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-14T00:00:00+00:00")
    ap.add_argument("--until", required=True)
    ap.add_argument("--network", default="solana",
                    help="4663: the EVM executor writes the same chase_quote_confirmation mark (gap_pct, "
                         "cap_pct; cap 0 = nano uncapped); lanes are split by the cap in force")
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    until = dt.datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    with contextlib.closing(get_readonly_connection()) as conn:
        raw = conn.execute(
            "SELECT p.id, p.signal_source, p.opened_at, p.closed_at, p.realized_pnl, p.entry_notional, "
            "p.total_copy_drift_pct, p.source_drift_pct, p.execution_slippage_pct, a.trace_json, "
            "a.source_drift_pct AS attempt_quote_drift "
            "FROM memecoin_positions p LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            f"WHERE {robot_performance_sql('p')} AND p.network_id=? AND p.is_open=0 "
            "AND p.opened_at>=? AND p.realized_pnl IS NOT NULL AND p.entry_notional>0",
            (args.network, since.date().isoformat())).fetchall()
    rows = []
    for r in raw:
        opened = dt.datetime.fromisoformat(r["opened_at"])
        closed = dt.datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None
        if not (since <= opened < until) or closed is None or closed >= until:
            continue
        gate, cap, note = gate_record(r["signal_source"], r["trace_json"], r["total_copy_drift_pct"])
        if args.network != "solana":
            # EVM: chase_quote_confirmation.gap_pct is the COIN's move (display/source, _chase_gap),
            # not our quote. The quote-vs-source gate (quote_over_source_max_pct_for, 30 on 4663 for
            # every band) decides on the attempt's source_drift_pct = exact quote / source - 1.
            gate, note = r["attempt_quote_drift"], "quote vs source (attempt source_drift_pct)"
        lane = r["signal_source"] if args.network == "solana" else "%s cap=%s" % (
            r["signal_source"], "none" if cap is None else format(cap, "g"))
        rows.append({"id": r["id"], "lane": lane, "opened": r["opened_at"],
                     "pnl": r["realized_pnl"], "ret": 100 * r["realized_pnl"] / r["entry_notional"],
                     "fill": r["total_copy_drift_pct"], "quote": r["source_drift_pct"],
                     "slip": r["execution_slippage_pct"], "gate": gate, "cap": cap, "note": note})
    print(f"funded {args.network} closes, opened [{since.isoformat()}, {until.isoformat()}), robot_performance_sql: "
          f"n={len(rows)}")
    for (lane, note), n in sorted(collections.Counter((r["lane"], r["note"]) for r in rows).items()):
        print(f"  {lane:16} {note:32} {n}")
    rng = random.Random(SEED)
    pooled = [{**r, "lane": "ALL (mixed ref)"} for r in rows]
    table("A. FILL reference (total_copy_drift_pct), per lane; ALL row pools lanes whose references differ",
          rows + pooled, lambda r: r["fill"], rng)
    table("B. GATE reference (J7/onchain: exact-quote gap_pct; slot lane: fill vs post-trade)",
          rows, lambda r: r["gate"], rng)

    print("\nC. WHY FILLS SIT AT fill drift >= 30 (one line per fill)")
    why = collections.Counter()
    for r in sorted((r for r in rows if r["fill"] is not None and r["fill"] >= 30), key=lambda r: r["opened"]):
        if r["gate"] is None:
            reason = r["note"]
        elif r["cap"] is not None and r["cap"] > 30:
            reason = f"cap {r['cap']:.0f} in force"
        elif r["gate"] <= 30:
            reason = "gate <= 30, fill moved after the quote"
        else:
            reason = f"gate {r['gate']:.1f} over cap {r['cap']}"
        why[reason] += 1
        print(f"  {r['id']} {r['opened'][:16]} {r['lane']:15} fill {r['fill']:>+6.1f} gate "
              f"{'-' if r['gate'] is None else format(r['gate'], '+.1f'):>6} cap {r['cap']} "
              f"slip {'-' if r['slip'] is None else format(r['slip'], '+.1f')} pnl {r['pnl']:>+7.2f}  {reason}")
    for k, v in why.most_common():
        print(f"  reason: {k}: {v}")

    # WHICH FILLS THE KNOB GOVERNS. The J7/on-chain quote cap on a NANO coin is
    # max(MEMECOIN_REAL_CHASE_MAX_PCT, MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT) (memecoin_real_runner
    # quote_chase_cap), so a fill decided at cap 35 (or 50 before 2026-09-24T06:46:19Z) keeps its cap
    # when only MEMECOIN_REAL_CHASE_MAX_PCT moves. The slot lane signs MEMECOIN_REAL_CHASE_MAX_PCT
    # itself (pump_curve_buy), nano or not.
    print("\nD. WHAT MEMECOIN_REAL_CHASE_MAX_PCT 30 -> 20 CUTS: gate value in (20, 30] (their PnL becomes 0)")
    for lane in sorted({r["lane"] for r in rows}):
        band = [r for r in rows if r["lane"] == lane and r["gate"] is not None and 20 < r["gate"] <= 30]
        for label, cut in (("knob-governed (cap 30)", [r for r in band if r["cap"] == 30.0]),
                           ("nano cap 35/50, unchanged", [r for r in band if r["cap"] != 30.0])):
            if not cut:
                print(f"  {lane:16} {label:26} n=  0")
                continue
            pnl = [r["pnl"] for r in cut]
            lo, hi = boot(pnl, sum, rng) if len(cut) >= 5 else (float("nan"), float("nan"))
            trim = (f" trim3 ${sum(r['pnl'] for r in sorted(cut, key=lambda r: r['pnl'])[3:-3]):+.2f}"
                    if len(cut) > 6 else "")
            print(f"  {lane:16} {label:26} n={len(cut):>3} sum ${sum(pnl):+.2f} [{lo:+.2f}, {hi:+.2f}] "
                  f"median ret {statistics.median(r['ret'] for r in cut):+.1f}%{trim}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
