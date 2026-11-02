#!/usr/bin/env python3
"""Hourly progress readout for the brain-v2 stabilized-probability shadow validation.

Emits ONE compact line per run: how much shadow data has accrued, the resolved-window label split, and the
milestone-relevant holdout sharp_revert count + gate status. Lets you watch the validation climb without a
manual run:
    tail -f logs/crypto5m_brain_progress.log

READ-ONLY. Opens the shadow DB read-only and never touches the engine or the live decision path. The gate
unit is the RESOLVED sharp_revert holdout window (rare — a sharp intra-window move that settles against
itself); the first monitor milestone is 60. Reuses the validation harness so the count can never drift from it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts import crypto5m_z_validation as ZV               # noqa: E402
from sniper.algos.polymarket_crypto5m import live_desk as LD   # noqa: E402

DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")
FIRST_MILESTONE = 60     # first monitor milestone of the 60/120/240 holdout sharp_revert ladder


def _now_iso() -> str:
    """Binance real time (the engine's clock — the local env clock is skewed); fall back to system UTC."""
    t = LD._safe(LD.server_now)
    dt = datetime.fromtimestamp(t, timezone.utc) if t else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    if not os.path.exists(DB):
        print(f"{_now_iso()}  [brain-progress] no shadow db at {DB}")
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ticks = conn.execute("SELECT COUNT(*) FROM crypto5m_z_shadow").fetchone()[0]
    labels = {"calm": 0, "sharp_revert": 0, "sharp_continue": 0}
    for r in conn.execute("SELECT sharp_label, COUNT(DISTINCT window_start_ts) w FROM crypto5m_z_shadow "
                          "WHERE status='resolved' GROUP BY sharp_label"):
        if r["sharp_label"] in labels:
            labels[r["sharp_label"]] = r["w"]
    resolved = sum(labels.values())
    g = ZV.gate(conn, milestone=FIRST_MILESTONE)
    gm = g["guards"]["mean"]
    print(f"{_now_iso()}  ticks={ticks} resolved={resolved} "
          f"(calm={labels['calm']} sharp_revert={labels['sharp_revert']} sharp_continue={labels['sharp_continue']}) "
          f"| holdout_sharp_revert={g['n_sharp_revert_holdout']}/{FIRST_MILESTONE} status={g['status']} "
          f"fit(a={g['a']:.2f},b={g['b']:.2f}) guard_mean={'n/a' if gm is None else f'{gm:+.4f}'}")


if __name__ == "__main__":
    main()
