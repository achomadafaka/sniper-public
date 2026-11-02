#!/usr/bin/env python3
"""WO-4 — regime context read (shadow, read-only).

Two parts:
  (1) descriptive — the current tape market-breadth per funded chain, so the operator sees how often
      the market is "regime off" (the only state the proposed shadow rule would tighten in);
  (2) the preregistered counterfactual — across recorded regime='off' decisions (n>=30), would
      tightening the reference trail toward 0.5T have beaten leaving it at v3? Pre-data this reports
      insufficient; the harness is ready for when shadow decisions accrue.

Prereg: docs/research/2026-07-25-regime-context-shadow-prereg.md. Read-only; proposals only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

MIN_OFF_DECISIONS = 30


def _connect_ro(path):
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def current_breadth(ticks_db):
    from sniper.analysis.market_breadth import tape_market_breadth
    conn = _connect_ro(ticks_db)
    if conn is None:
        return {}
    out = {}
    try:
        for chain in ("4663", "solana"):
            out[chain] = tape_market_breadth(conn, chain)
    finally:
        conn.close()
    return out


def regime_off_decisions(exit_state_db):
    """Recorded decisions whose logged regime context was 'off' — the read's population."""
    conn = _connect_ro(exit_state_db)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT position_id, decided_at, candidate_action, regime_json "
            "FROM memecoin_exit_candidate_decisions WHERE regime_json IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            regime = json.loads(r["regime_json"])
        except (ValueError, TypeError):
            continue
        if regime.get("regime") == "off":
            out.append(dict(r))
    return out


def render(breadth, off_decisions):
    lines = ["", "=" * 68, "  REGIME CONTEXT (shadow, read-only)", "=" * 68,
             "  current tape market-breadth:"]
    for chain, b in breadth.items():
        if not b:
            continue
        br = f"{b['breadth']:.0%}" if b.get("breadth") is not None else "n/a"
        lines.append(f"    {chain:7s} regime={b['regime']:7s} breadth={br} "
                     f"active_pools={b['active_pools']} positive={b['positive_pools']}")
    n = len(off_decisions)
    lines.append(f"\n  regime-off decisions recorded: n={n}")
    if n < MIN_OFF_DECISIONS:
        lines.append(f"  COUNTERFACTUAL: insufficient (n<{MIN_OFF_DECISIONS}) — no tighten-vs-not "
                     "verdict. Harness ready; accrues once the shadow logs decisions.")
    else:
        # tighten-vs-not requires the recorded counterfactual price path per decision; wired to run
        # off the same snapshot timeline the decision report uses once regime-off decisions exist.
        lines.append("  COUNTERFACTUAL: n>=30 — run the tighten-vs-not read (see prereg §read).")
    lines += ["=" * 68, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Regime context report (read-only).")
    ap.add_argument("--ticks-db", default=None)
    ap.add_argument("--exit-state-db", default=None)
    args = ap.parse_args()
    from sniper.persistence.db_exit_state import exit_state_db_path
    from sniper.persistence.db_ticks import ticks_db_path
    breadth = current_breadth(args.ticks_db or ticks_db_path())
    offs = regime_off_decisions(args.exit_state_db or exit_state_db_path())
    print(render(breadth, offs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
