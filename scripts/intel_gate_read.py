#!/usr/bin/env python3
"""Pre-registered Intel gate readouts. Read-only.

GATE A (n>=10): repricing capture — how much of the post-event move had already
happened by the time we decided. This is the measurement the Intel system has
never had (audit §8): it filtered extreme prices but never asked whether the
market moved *because of this event* before we saw it.

    capture = (price_at_decision - price_at_event) / (price_final - price_at_event)

median(capture) >= 0.80 KILLS the program. If the move is already gone at
decision time, no improvement in picking can recover it, and the intelligence
layers in Phase 4 must not be built.

Thresholds are pre-registered in
docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md and must not be
adjusted after seeing results — re-cutting a sample until something survives is
the failure mode recorded in finding-crypto5m-floor-endogeneity-2026-07-07.

Usage:
    python3 scripts/intel_gate_read.py --gate a
Exit code 0 when the gate has fired (kill or continue), 1 while it has not.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics
import sys

# Runnable as `python3 scripts/intel_gate_read.py` from the repo root, which does
# not put the repo on sys.path (matches the bootstrap in scripts/world_analyst.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GATE_A_MIN_N = 10
GATE_A_KILL = 0.80
GATE_A_AMBIGUOUS = 0.60

# The post-event window used to establish where the price ended up.
FINAL_PRICE_OFFSET_S = 86400


def compute_capture(rows: list[dict]) -> dict:
    """Fraction of the post-event move already realised at decision time."""
    caps = []
    for r in rows:
        pe, pd, pf = r.get("price_at_event"), r.get("price_at_decision"), r.get("price_final")
        if pe is None or pd is None or pf is None:
            continue
        denom = pf - pe
        if abs(denom) < 1e-9:
            continue  # no move to capture — carries no information about lateness
        caps.append((pd - pe) / denom)

    n = len(caps)
    if n < GATE_A_MIN_N:
        return {"n": n, "median_capture": None, "verdict": "insufficient_evidence"}

    med = statistics.median(caps)
    if med >= GATE_A_KILL:
        verdict = "kill"
    elif med >= GATE_A_AMBIGUOUS:
        verdict = "ambiguous"
    else:
        verdict = "continue"
    return {"n": n, "median_capture": round(med, 3), "verdict": verdict}


def _load(db: str) -> list[dict]:
    """Read measured candidates and fetch each one's post-event settling price."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, token_id, event_ts, price_at_event, price_at_decision
           FROM trade_ideas
           WHERE token_id IS NOT NULL
             AND event_ts IS NOT NULL
             AND price_at_event IS NOT NULL
             AND price_at_decision IS NOT NULL"""
    ).fetchall()
    conn.close()

    from sniper.markets.pm_resolver import snapshot_prices

    out = []
    for r in rows:
        d = dict(r)
        try:
            ev = dt.datetime.fromisoformat(str(d["event_ts"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue  # unparseable event time — cannot place the window
        _, d["price_final"] = snapshot_prices(
            d["token_id"], int(ev.timestamp()), int(ev.timestamp()) + FINAL_PRICE_OFFSET_S
        )
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-registered Intel gate readouts")
    ap.add_argument("--gate", choices=["a"], default="a")
    ap.add_argument("--db", default="sniper_tracker.db")
    args = ap.parse_args()

    result = compute_capture(_load(args.db))
    print(f"GATE A  n={result['n']}  median_capture={result['median_capture']}  "
          f"verdict={result['verdict']}")

    if result["verdict"] == "kill":
        print("KILL: the market has already repriced by the time we decide. "
              "Stop. Do not build Phase 3 or Phase 4.")
    elif result["verdict"] == "continue":
        print("CONTINUE: residual move remains after our decision point. Proceed to Phase 3.")
    elif result["verdict"] == "ambiguous":
        print(f"AMBIGUOUS: median in [{GATE_A_AMBIGUOUS}, {GATE_A_KILL}). "
              "Accrue to n=25 before deciding.")

    return 0 if result["verdict"] in ("kill", "continue") else 1


if __name__ == "__main__":
    sys.exit(main())
