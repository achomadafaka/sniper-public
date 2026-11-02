#!/usr/bin/env python3
"""Hourly progress readout for the drift-reachability SHADOW validation. One compact line per run:
    tail -f logs/crypto5m_drift_reach.log
Tracks complete drift predictions toward the first milestone (150) and the clustered rho(exp_fav_move,
realized) as it firms up. READ-ONLY; reuses the validation gate() so the numbers can't drift from the
harness. Timestamps in Binance real time (the local env clock is skewed)."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts import crypto5m_drift_reach_validation as V        # noqa: E402
from sniper.algos.polymarket_crypto5m import live_desk as LD    # noqa: E402

DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")
FIRST_MILESTONE = 150


def _now_iso() -> str:
    t = LD._safe(LD.server_now)
    dt = datetime.fromtimestamp(t, timezone.utc) if t else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    if not os.path.exists(DB):
        print(f"{_now_iso()}  [drift-reach] no shadow db at {DB}")
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    g = V.gate(conn, milestone=FIRST_MILESTONE)

    def f(x):
        return "n/a" if x is None else f"{x:+.4f}"

    print(f"{_now_iso()}  drift-reach n={g['n']}/{FIRST_MILESTONE} windows={g['n_windows']} "
          f"status={g['status']} rho={f(g['rho'])} CI[{f(g['ci_lo'])},{f(g['ci_hi'])}] oos={f(g['rho_oos'])}")


if __name__ == "__main__":
    main()
