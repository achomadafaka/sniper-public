#!/usr/bin/env python3
"""Phase 1C market-efficiency gate report (READ-ONLY over a captured crypto5m DB).

Assembles E1 marginal calibration, E2 diagnostic model ladder (walk-forward), E3 executable hurdle, the
negative controls, and the go/no-go verdict, at the data milestone reached. Diagnostic only — no model
promotion, no strategy claim, no orders. Prints a JSON report + a short summary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.algos.polymarket_crypto5m import market_efficiency as ME  # noqa: E402
from sniper.algos.polymarket_crypto5m import persistence as P          # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="crypto5m market-efficiency gate report (read-only)")
    ap.add_argument("--db-path", default="data/crypto5m_campaign.sqlite")
    ap.add_argument("--json", action="store_true", help="print the full JSON report")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db_path):
        print(f"capture DB not found: {a.db_path}")
        return 1
    conn = P.connect(a.db_path)
    if not ME.feature_snapshots_available(conn):
        conn.close()
        print("feature snapshots not found: run the offline shadow replay before the efficiency report:")
        print(f"  python3 scripts/run_polymarket_crypto5m_shadow_sim.py --db-path {a.db_path} "
              "--mode replay --model market_midpoint_baseline")
        return 1
    rep = ME.efficiency_gate_report(conn)
    conn.close()
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    n = rep["n_resolved_windows"]
    reached = [m for m in ME.MILESTONES if rep["milestones"][m]]
    print(f"=== crypto5m efficiency gate — {n} usable resolved windows "
          f"(milestones reached: {reached or 'none'}) ===")
    print(f"VERDICT: {rep['go_no_go']['verdict']}")
    for r in rep["go_no_go"]["reasons"]:
        print(f"  - {r}")
    print(f"negative controls passed: {rep['negative_controls_passed']}")
    print("E2 model ladder (OOS Brier, delta vs midpoint):")
    for name, m in rep["E2_model_ladder"].items():
        print(f"  {name}: n={m.get('n')} brier={m.get('brier')} dBrier={m.get('delta_brier_vs_m0')}")
    print("E3 executable hurdle (primary checkpoints, median):")
    for r in rep["E3_executable_hurdle"]:
        print(f"  cp={r['checkpoint']} {r['side']} ${r['stake']}: fillable={r['fillable_share']:.2f} "
              f"median_hurdle={r['median_executable_hurdle']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
