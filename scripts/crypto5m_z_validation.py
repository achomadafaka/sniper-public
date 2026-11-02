#!/usr/bin/env python3
"""Offline validation harness for crypto5m brain v2 (design sec 4 + 7). Read-only; proposes a verdict, never
mutates thresholds or the live path. Gate = paired per-window Brier delta over the SHARP TICKS of each window
(window-clustered bootstrap), win on sharp-revert, no-regress on calm + sharp-continue. Routed through the
shared window_bootstrap (same seed as lane-eval)."""
from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.polymarket_crypto5m import window_bootstrap as WB   # noqa: E402

D_REF = 2.0   # a tick is "sharp" if jump_flag or divergence >= D_REF (matches stabilized_z.D_REF)
DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "active_trader_v0.sqlite")
MILESTONES = (60, 120, 240)   # holdout sharp-revert windows: monitor / monitor / first-confirmatory


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sigmoid(x):
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def load_resolved(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM crypto5m_z_shadow WHERE status='resolved' AND z IS NOT NULL AND z_v2 IS NOT NULL "
        "ORDER BY window_start_ts, tick_ts")]


def _is_sharp_tick(r):
    return bool(r.get("jump_flag")) or (r.get("divergence") is not None and r["divergence"] >= D_REF)


def window_brier_delta(rows):
    """Mean over the window's SHARP ticks of Brier(current z) - Brier(LOGGED z_v2). >0 = z_v2 better. None if
    the window has no sharp ticks. NOTE: this scores the z_v2 as LOGGED live (the prior weight) -- it is the
    diagnostic/Task-9 building block. The GATE (`gate()`) instead RECOMPUTES z_v2 from the holdout-FITTED
    weight via `_by_window_delta`, because the fitted weight is what a later v2 would ship."""
    deltas = []
    for r in rows:
        if not _is_sharp_tick(r):
            continue
        y = r["settlement"]
        b_cur = (phi(r["z"]) - y) ** 2
        b_v2 = (phi(r["z_v2"]) - y) ** 2
        deltas.append(b_cur - b_v2)
    return (sum(deltas) / len(deltas)) if deltas else None


def chronological_split(windows, train_frac=0.7):
    ordered = sorted(windows)
    cut = int(len(ordered) * train_frac)
    return ordered[:cut], ordered[cut:]


def recompute_z_v2(row, a, b):
    """z_v2 under a candidate weight w = sigmoid(a - b*log(divergence)), from the logged components."""
    d = row.get("divergence")
    if d is None or d <= 0 or row.get("z_ours") is None or row.get("z_market") is None:
        return row.get("z_ours")
    w = sigmoid(a - b * math.log(d))
    return w * row["z_ours"] + (1.0 - w) * row["z_market"]


def fit_weight(train_rows, a_grid=None, b_grid=None):
    """Grid search (a, b) minimizing mean Brier of Phi(z_v2(a,b)) over ALL usable training rows. The weight
    w=sigmoid(a-b*log(div)) is applied at every live tick, so it is fit across all regimes -- calm rows pull
    w->1 where our model is right, sharp rows pull w->0 where the market is. (The GATE, separately, aggregates
    the delta over sharp ticks.) Deterministic; stdlib-only. b>=0 keeps w monotone decreasing in sharpness."""
    a_grid = a_grid or [x * 0.5 for x in range(-6, 7)]       # -3.0 .. 3.0
    b_grid = b_grid or [x * 0.5 for x in range(0, 9)]        # 0 .. 4.0
    rows = [r for r in train_rows if r.get("divergence") and r.get("z_ours") is not None
            and r.get("z_market") is not None and r.get("settlement") is not None]
    if not rows:
        return (0.0, 0.0)
    best, best_brier = (0.0, 0.0), float("inf")
    for a in a_grid:
        for b in b_grid:
            br = sum((phi(recompute_z_v2(r, a, b)) - r["settlement"]) ** 2 for r in rows) / len(rows)
            if br < best_brier:
                best, best_brier = (a, b), br
    return best


def _by_window_delta(rows_by_window, a, b):
    """{window: [delta]} where delta = mean sharp-tick (Brier(z) - Brier(z_v2(a,b)))."""
    out = {}
    for ws, rows in rows_by_window.items():
        deltas = [((phi(r["z"]) - r["settlement"]) ** 2 - (phi(recompute_z_v2(r, a, b)) - r["settlement"]) ** 2)
                  for r in rows if _is_sharp_tick(r)]
        if deltas:
            out[ws] = [sum(deltas) / len(deltas)]
    return out


def _ci(by_window, rng):
    means = WB.boot_means(by_window, rng)
    if not means:
        return (None, None, None)
    return (sum(means) / len(means), WB.pctile(means, 0.025), WB.pctile(means, 0.975))


def gate(conn, *, train_frac=0.7, milestone=240):
    rows = load_resolved(conn)
    by_window, label = {}, {}
    for r in rows:
        by_window.setdefault(r["window_start_ts"], []).append(r)
        label[r["window_start_ts"]] = r.get("sharp_label")
    train_ws, holdout_ws = chronological_split(by_window, train_frac)
    a, b = fit_weight([r for ws in train_ws for r in by_window[ws]])

    def bucket(names):
        return {ws: by_window[ws] for ws in holdout_ws if label.get(ws) in names}

    revert = _by_window_delta(bucket({"sharp_revert"}), a, b)
    guards = _by_window_delta(bucket({"calm", "sharp_continue"}), a, b)
    n_rev = len(revert)
    rev_mean, rev_lo, rev_hi = _ci(revert, random.Random(WB.BOOT_SEED))
    grd_mean, grd_lo, grd_hi = _ci(guards, random.Random(WB.BOOT_SEED))

    res = {"a": a, "b": b, "n_sharp_revert_holdout": n_rev, "milestones": MILESTONES,
           "revert": {"mean": rev_mean, "ci_lo": rev_lo, "ci_hi": rev_hi},
           "guards": {"mean": grd_mean, "ci_lo": grd_lo, "ci_hi": grd_hi}}
    if n_rev < milestone:
        res["status"] = "accruing"
        return res
    win = rev_lo is not None and rev_lo > 0                       # CI excludes 0, favorable
    no_regress = grd_lo is None or grd_lo >= 0                    # guards not adverse
    res["status"] = "go" if (win and no_regress) else "no_go"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--milestone", type=int, default=240)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[z-validation] no db at {args.db}")
        return
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    g = gate(conn, milestone=args.milestone)
    print(f"[z-validation] status={g['status']}  fit a={g['a']:.2f} b={g['b']:.2f}  "
          f"n_sharp_revert_holdout={g['n_sharp_revert_holdout']} (milestones {g['milestones']})")
    rv, gd = g["revert"], g["guards"]

    def fmt(d):
        return "n/a" if d["mean"] is None else f"mean {d['mean']:+.4f}  CI [{d['ci_lo']:+.4f}, {d['ci_hi']:+.4f}]"
    print(f"  sharp_revert (WIN bucket): {fmt(rv)}")
    print(f"  calm+sharp_continue (GUARD): {fmt(gd)}")


if __name__ == "__main__":
    main()
