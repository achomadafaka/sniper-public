#!/usr/bin/env python3
"""Partial-de-risk / early-trail counterfactual on the post-cutover funded cohort (P1/P2 design study).

Context: docs/superpowers/specs/2026-07-22-memecoin-stop-derisk-redesign-proposal.md.
The all-or-nothing rail sweep (§counterfactual) showed every level beats status quo but none
turns the cohort positive, and warned that lowering the first rail truncates the tail. This
study parameterizes the two structures that avoid all-or-nothing:

  P(x, f)  — sell fraction f at the first EXECUTABLE crossing of +x%; the remainder keeps the
             position's actual fate (approximation: remainder PnL = (1-f) x actual realized).
  T(a, t)  — early trail, P2's empirically-positive shape: arm at +a% executable with NO
             de-risk gate; exit the whole bag on a t% executable drawdown from the armed peak;
             positions that never arm/trigger keep their actual outcome. Full tape replay.
  C(x,f,t) — P then the remainder trails (armed from the partial fill's peak).

Honesty labels:
  - Sim fills price at the triggering mark's executable value x (1 - HAIRCUT). HAIRCUT=0.03
    (observed real 4663 execution slippage ~1-3%). Mark cadence ~56s — same granularity the
    live sweep trades on, so trail replay is fair, but intra-mark spikes are invisible.
  - P-configs approximate the remainder's fate as unchanged. True for stops/max-hold; slightly
    off for winners whose actual +50 de-risk sizing assumed the full bag.
  - n=29. This picks a ROBUST REGION for a pre-registered paper arm, not an argmax to ship.

Read-only. Run: python3 scripts/research/memecoin_partial_derisk_counterfactual.py
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import json
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/bigpapi/Desktop/Sniper")
from sniper.services.memecoin_market_state import _coin_band   # the runner's own band function

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
CUTOFF = "2026-07-18T13:50:27"
HAIRCUT = 0.03

def load_cohort(c, band=None):
    """band=None loads every post-cutover funded close. Pass a band name to restrict.

    THE BAND SPLIT IS NOT OPTIONAL (2026-07-27). The original study had no band filter, so its
    result was applied to nano/micro while its EVIDENCE came from small/mid — the bands that
    carry the money. Of 28 real closes with a resolvable band, 22 are small, 6 are mid, and ZERO
    are nano or micro. Always report per band before changing a per-band policy."""
    rows = c.execute("""
      SELECT id, mint, network_id, entry_price, entry_notional, realized_pnl, exit_reason,
             opened_at, closed_at, entry_mcap, funded_contract_json
      FROM memecoin_positions
      WHERE execution_mode='real' AND is_open=0 AND opened_at>=? ORDER BY opened_at""",
      (CUTOFF,)).fetchall()
    out = []
    for r in rows:
        tape = c.execute("""
          SELECT sweep_ts, executable_exit_value FROM memecoin_position_marks
          WHERE mint=? AND chain_id=? AND sweep_ts BETWEEN ? AND ?
            AND executable_exit_value IS NOT NULL AND executable_exit_value > 0
          ORDER BY sweep_ts""",
          (r["mint"], r["network_id"], r["opened_at"], r["closed_at"])).fetchall()
        d = {**dict(r), "tape": [t["executable_exit_value"] for t in tape]}
        # AUTHORITATIVE band: what the runner actually classified at decision time, read from the
        # immutable funded contract. entry_mcap is NULL on 21 of 31 post-cutover rows, so deriving
        # the band from it buckets two thirds of the money as "out_of_scope" and hides the answer.
        d["band"] = d["policy_id"] = None
        if r["funded_contract_json"]:
            pol = json.loads(r["funded_contract_json"]).get("policy") or {}
            d["band"], d["policy_id"] = pol.get("coin_band"), pol.get("policy_id")
        if d["band"] is None:
            d["band"] = _coin_band(r["entry_mcap"], False) + "(derived)"
        if band is None or d["band"] == band:
            out.append(d)
    return out

def rets(pos):
    e = pos["entry_price"]
    return [p / e - 1.0 for p in pos["tape"]] if e > 0 else []

def sim_partial(pos, x, f):
    """f sold at first executable crossing >= x; remainder keeps actual fate."""
    n = pos["entry_notional"]
    m_act = pos["realized_pnl"] / n if n else 0.0
    for r in rets(pos):
        if r >= x:
            fill_mult = (1.0 + r) * (1.0 - HAIRCUT)
            return f * n * (fill_mult - 1.0) + (1.0 - f) * n * m_act, True
    return pos["realized_pnl"], False

def sim_trail(pos, arm, trail, frac=1.0, base_pnl=None, start_idx=0):
    """Trail `frac` of the bag: arm at +arm% executable, exit on trail% drawdown from peak.
    Unarmed/untriggered -> actual fate for that fraction. base_pnl overrides the untouched part."""
    n = pos["entry_notional"]
    m_act = pos["realized_pnl"] / n if n else 0.0
    rest = base_pnl if base_pnl is not None else (1.0 - frac) * n * m_act
    armed, peak = False, None
    for r in rets(pos)[start_idx:]:
        if not armed and r >= arm:
            armed, peak = True, r
            continue
        if armed:
            peak = max(peak, r)
            if (1.0 + r) <= (1.0 + peak) * (1.0 - trail / 100.0):
                fill_mult = (1.0 + r) * (1.0 - HAIRCUT)
                return rest + frac * n * (fill_mult - 1.0), True
    return rest + frac * n * m_act, armed

def sim_combo(pos, x, f, trail):
    """Partial f at +x, remainder trails from the fill point (already above arm)."""
    n = pos["entry_notional"]
    m_act = pos["realized_pnl"] / n if n else 0.0
    rr = rets(pos)
    for i, r in enumerate(rr):
        if r >= x:
            part = f * n * ((1.0 + r) * (1.0 - HAIRCUT) - 1.0)
            total, _ = sim_trail(pos, arm=r, trail=trail, frac=(1.0 - f),
                                 base_pnl=part, start_idx=i)
            return total, True
    return pos["realized_pnl"], False

def report(cohort, label, fn):
    tot = defaultdict(float); touched = 0; delta_by_id = {}
    for pos in cohort:
        pnl, hit = fn(pos)
        tot["all"] += pnl; tot[pos["network_id"]] += pnl
        touched += bool(hit)
        delta_by_id[pos["id"]] = pnl - pos["realized_pnl"]
    actual = sum(p["realized_pnl"] for p in cohort)
    winners = sorted(cohort, key=lambda p: -p["realized_pnl"])[:4]
    wd = ", ".join(f"{delta_by_id[p['id']]:+.1f}" for p in winners)
    print(f"{label:26s} total={tot['all']:+8.2f} (d{tot['all']-actual:+7.2f}) "
          f"4663={tot.get('4663', 0.0):+8.2f} sol={tot.get('solana', 0.0):+7.2f} "
          f"touched={touched:2d}/{len(cohort):<2d} top4winnersD=[{wd}]")

def sweep(cohort, header):
    """The full config sweep on one cohort."""
    actual = sum(p["realized_pnl"] for p in cohort)
    print(f"\n{'='*100}\n{header}  n={len(cohort)}  ACTUAL={actual:+.2f}\n{'='*100}")
    if len(cohort) < 5:
        print("  (n too small to read — reported for completeness only)")
    print("-- P(x, f): partial sell at first executable +x, remainder keeps actual fate --")
    for x in (0.13, 0.15, 0.20, 0.25):
        for f in (0.33, 0.5):
            report(cohort, f"P(x={int(x*100)}%, f={f})", lambda p, x=x, f=f: sim_partial(p, x, f))
    print("-- T(arm, trail): early full-bag trail, no de-risk gate --")
    for arm in (0.15, 0.20):
        for t in (25, 30, 35):
            report(cohort, f"T(arm={int(arm*100)}%, trail={t}%)",
                   lambda p, a=arm, t=t: sim_trail(p, a, t))
    print("-- C(x, f, trail): partial then remainder trails from fill --")
    for x, f, t in ((0.15, 0.5, 30), (0.20, 0.5, 30), (0.20, 0.33, 30), (0.20, 0.5, 25),
                    (0.15, 0.5, 25), (0.25, 0.5, 30)):
        report(cohort, f"C(x={int(x*100)}%, f={f}, t={t})",
               lambda p, x=x, f=f, t=t: sim_combo(p, x, f, t))


def main():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    cohort = load_cohort(c)
    by_band = defaultdict(list)
    for p in cohort:
        by_band[p["band"]].append(p)
    print("cohort by band (the split the original study omitted):")
    for b, v in sorted(by_band.items(), key=lambda kv: -len(kv[1])):
        marked = sum(1 for p in v if p["tape"])
        pids = ",".join(sorted({str(p["policy_id"]) for p in v}))
        print(f"  {b:14s} n={len(v):3d}  with_tape={marked:3d}  "
              f"actual=${sum(p['realized_pnl'] for p in v):+8.2f}  policy={pids}")
    for b, v in sorted(by_band.items(), key=lambda kv: -len(kv[1])):
        sweep(v, f"BAND = {b}")
    sweep(cohort, "ALL BANDS POOLED (what the original study reported)")
    print("\n" + "="*100)


# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
