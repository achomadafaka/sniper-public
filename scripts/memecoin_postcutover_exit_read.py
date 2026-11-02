#!/usr/bin/env python3
"""Pre-registered read of the post-cutover funded exit cohort — gated at n>=30 closes.

Spec (sealed 2026-07-21): docs/superpowers/specs/2026-07-21-memecoin-postcutover-exit-read.md
Before the gate this prints ONLY progress (n/30) and exits 1 — no decomposition, no peeking.
At/after the gate it prints the sealed metrics in the pre-registered order. Read-only.
"""
from __future__ import annotations

import os
import sqlite3
import sys

GATE_N = 30
STOP_CLASS = {"b_stop", "stop", "catastrophe"}
WINNER_CLASS = {"real_tp", "real_trail", "mcap_target", "derisk"}
DB = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "sniper_tracker.db"))


def main():
    conn = sqlite3.connect(f"file:{os.path.abspath(DB)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT realized_pnl, exit_reason FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND funded_contract_json IS NOT NULL "
        "AND realized_pnl IS NOT NULL"
    ).fetchall()
    n = len(rows)
    if n < GATE_N:
        print(f"post-cutover exit read: GATE NOT REACHED — {n}/{GATE_N} closes. "
              "No decomposition before the gate (pre-registered).")
        return 1

    pnls = [float(r["realized_pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    print(f"post-cutover funded exit cohort: n={n}")
    print(f"1) total={total:+.2f}  win_rate={len(wins)/n:.1%}  "
          f"avg_win={sum(wins)/len(wins) if wins else 0.0:+.2f}  "
          f"avg_loss={sum(losses)/len(losses) if losses else 0.0:+.2f}")

    by_reason = {}
    for r in rows:
        by_reason.setdefault(r["exit_reason"] or "?", []).append(float(r["realized_pnl"]))
    print("2) by exit_reason:")
    for reason, vals in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
        print(f"   {reason:14s} n={len(vals):3d} total={sum(vals):+9.2f} avg={sum(vals)/len(vals):+7.2f}")

    top = sorted(pnls, reverse=True)
    print(f"3) sensitivity: drop-top-1={total - top[0]:+.2f}  "
          f"drop-top-2={total - top[0] - (top[1] if len(top) > 1 else 0.0):+.2f}")

    stop_losses = sum(-float(r["realized_pnl"]) for r in rows
                      if (r["exit_reason"] or "") in STOP_CLASS and float(r["realized_pnl"]) < 0)
    gross_losses = sum(-p for p in losses)
    share = (stop_losses / gross_losses) if gross_losses > 0 else 0.0
    print(f"4) stop-share of gross losses: {share:.1%} "
          f"(stop-class -{stop_losses:.2f} of -{gross_losses:.2f})")
    print("\nPre-registered actions: see the spec — stop-share>55% AND total<0 -> operator "
          "proposal; total>=0 -> rails hold; ambiguous -> accrue to n=50, one re-read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
