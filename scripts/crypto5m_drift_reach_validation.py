#!/usr/bin/env python3
"""Offline validation for the drift-reachability SHADOW.

The driftless ev-reach gate's directional prediction had rho ~0 with the realized move (0.024, clustered
p=0.76) — it is symmetric and predicts nothing. This asks the one question that matters: does the
MEASURED-drift expected favorable move predict the realized mid move?

Primary test: window-clustered Spearman of exp_fav_move vs realized_mid_move (cluster = window_start_ts;
ticks within a window are NOT independent). WIN = the clustered bootstrap CI excludes 0 and is positive —
the measured drift adds the directional signal the driftless baseline (exactly 0) structurally lacked.
Chronological 70/30 split reports OOS separately. Milestones 150 / 300 / 600 complete predictions.
Read-only; deterministic seed. No PnL claim — this is a calibration/prediction test.
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")
MILESTONES = (150, 300, 600)
BOOT_SEED = 12345
BOOT_N = 2000


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0                      # average (1-based) rank for ties
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman rank correlation. None if < 3 points; 0.0 if either side has no rank variance."""
    if len(xs) < 3 or len(ys) < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx * vy) ** 0.5


def _pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def clustered_bootstrap_rho(by_window, rng, n=BOOT_N):
    """Resample WINDOWS with replacement, pool their rows, recompute Spearman -> (point rho, 2.5%, 97.5%)."""
    keys = list(by_window.keys())
    all_rows = [r for ws in keys for r in by_window[ws]]
    point = spearman([r["exp_fav_move"] for r in all_rows], [r["realized_mid_move"] for r in all_rows])
    if point is None or len(keys) < 2:
        return (point, None, None)
    boots = []
    for _ in range(n):
        pick = [rng.choice(keys) for _ in keys]
        rows = [r for k in pick for r in by_window[k]]
        rho = spearman([r["exp_fav_move"] for r in rows], [r["realized_mid_move"] for r in rows])
        if rho is not None:
            boots.append(rho)
    boots.sort()
    return (point, _pctile(boots, 0.025), _pctile(boots, 0.975))


def load_complete(conn):
    try:
        return conn.execute(
            "SELECT window_start_ts, exp_fav_move, realized_mid_move FROM crypto5m_drift_reach "
            "WHERE status='complete' AND exp_fav_move IS NOT NULL AND realized_mid_move IS NOT NULL "
            "ORDER BY tick_ts").fetchall()
    except sqlite3.OperationalError:
        return []                                      # table not created yet (engine not deployed) -> nothing to validate


def chronological_split(by_window, train_frac=0.7):
    keys = sorted(by_window.keys())
    cut = int(len(keys) * train_frac)
    return keys[:cut], keys[cut:]


def gate(conn, *, milestone=600):
    conn.row_factory = sqlite3.Row                     # robust: callers needn't set it (rows accessed by name)
    rows = load_complete(conn)
    by_window = {}
    for r in rows:
        by_window.setdefault(r["window_start_ts"], []).append(r)
    n = len(rows)
    rho, lo, hi = clustered_bootstrap_rho(by_window, random.Random(BOOT_SEED))
    _, oos = chronological_split(by_window)
    oos_rows = [r for k in oos for r in by_window[k]]
    rho_oos = spearman([r["exp_fav_move"] for r in oos_rows],
                       [r["realized_mid_move"] for r in oos_rows]) if oos_rows else None
    res = {"n": n, "n_windows": len(by_window), "rho": rho, "ci_lo": lo, "ci_hi": hi,
           "rho_oos": rho_oos, "milestones": MILESTONES}
    if n < milestone:
        res["status"] = "accruing"
        return res
    res["status"] = "win" if (lo is not None and lo > 0) else "no_signal"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--milestone", type=int, default=600)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[drift-reach] no db at {args.db}")
        return
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    g = gate(conn, milestone=args.milestone)
    def f(x):
        return "n/a" if x is None else f"{x:+.4f}"
    print(f"[drift-reach] status={g['status']} n={g['n']} windows={g['n_windows']} (milestones {g['milestones']})")
    print(f"  rho(exp_fav_move, realized) = {f(g['rho'])}  CI [{f(g['ci_lo'])}, {f(g['ci_hi'])}]  | OOS rho {f(g['rho_oos'])}")


if __name__ == "__main__":
    main()
