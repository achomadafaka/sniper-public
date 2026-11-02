#!/usr/bin/env python3
"""Calibrate CUSUM thresholds for heavy-tailed P&L series.

Standard tabular CUSUM (`h=5, k=0.5σ`) is calibrated for Gaussian
process control with ARL₀ ≈ 370. P&L distributions on Polymarket are
heavy-tailed (one big bet often dominates lifetime PnL). Under heavy
tails, CUSUM false-alarm rate at standard parameters is unknown.

This script:
1. Pulls the empirical per-position P&L distribution from /closed-positions
   for all wallets in followed_wallet_activity.
2. Generates synthetic paths under that distribution with NO regime change.
3. Runs CUSUM with various h values, measures false-alarm rate per path.
4. Reports the smallest h that keeps false-alarm rate ≤ 5%.
5. Also reports ARL₀ (mean run length under null) for the calibrated h.

Output goes to stdout and a brain-memory-friendly JSON dump at
/tmp/cusum_calibration_2026-04-25.json. The locked h becomes the
v2 protocol's CUSUM parameter.

Usage:
    python3 scripts/calibrate_cusum_pnl.py [--n-paths 5000] [--path-length 200]
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time

# Make repo root importable when invoked as a script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import requests

from sniper.markets.polymarket_api import fetch_user_positions  # noqa: E402

SEED = 42


def fetch_pnl_distribution(db_path: str) -> list[float]:
    """Pull per-position realizedPnl across all 21 followed wallets.

    Returns the empirical distribution we'll resample from in simulation.
    Uses /closed-positions API (post-pagination-fix), so all losses are
    included.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    wallets = [r[0] for r in conn.execute(
        "SELECT DISTINCT wallet FROM followed_wallet_activity"
    )]
    conn.close()

    sess = requests.Session()
    pnl: list[float] = []
    for w in wallets:
        try:
            rows, _ft = fetch_user_positions(sess, w, endpoint="closed-positions")
            for r in rows:
                v = r.get("realizedPnl")
                if v is not None:
                    pnl.append(float(v))
            time.sleep(0.1)
        except Exception as e:
            print(f"  skip {w[:14]}: {e}")
    return pnl


def cusum_signals(series: np.ndarray, mu: float, sigma: float,
                  h: float, k: float) -> tuple[bool, int | None]:
    """Run two-sided tabular CUSUM on a P&L series.

    Returns (signaled, signal_index_or_None). h and k in σ-units.
    """
    if sigma <= 0:
        return False, None
    threshold = h * sigma
    slack = k * sigma
    s_pos = 0.0
    s_neg = 0.0
    for i, x in enumerate(series):
        s_pos = max(0.0, s_pos + (x - mu) - slack)
        s_neg = min(0.0, s_neg + (x - mu) + slack)
        if s_pos > threshold or s_neg < -threshold:
            return True, i
    return False, None


def measure_false_alarm(samples: np.ndarray, n_paths: int, path_length: int,
                        h: float, k: float, rng: np.random.Generator) -> dict:
    """Simulate n_paths under the empirical distribution (no regime change).

    Each path is `path_length` random draws from `samples`. Returns dict with
    false_alarm_rate, mean_run_length (ARL₀ proxy), and signal-position
    distribution.
    """
    mu = float(np.mean(samples))
    sigma = float(np.std(samples))
    n_signals = 0
    signal_positions = []
    for _ in range(n_paths):
        idx = rng.integers(0, len(samples), size=path_length)
        path = samples[idx]
        signaled, pos = cusum_signals(path, mu, sigma, h, k)
        if signaled:
            n_signals += 1
            signal_positions.append(pos)

    return {
        "h": h,
        "k": k,
        "mu": mu,
        "sigma": sigma,
        "n_paths": n_paths,
        "path_length": path_length,
        "n_signals": n_signals,
        "false_alarm_rate": n_signals / n_paths,
        "median_signal_position": (
            float(statistics.median(signal_positions)) if signal_positions else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--n-paths", type=int, default=5000)
    parser.add_argument("--path-length", type=int, default=200,
                        help="Synthetic path length (positions per simulated wallet)")
    parser.add_argument(
        "--target-false-alarm", type=float, default=0.05,
        help="Maximum acceptable false-alarm rate under null"
    )
    parser.add_argument("--out", default="/tmp/cusum_calibration_2026-04-25.json")
    args = parser.parse_args()

    print(f"=== CUSUM heavy-tail calibration ===")
    print(f"  Pulling /closed-positions PnL distribution from {args.db}")
    samples = fetch_pnl_distribution(args.db)
    samples_arr = np.asarray(samples, dtype=float)
    print(f"  empirical sample size: {len(samples_arr)}")
    print(f"  mean: {samples_arr.mean():.2f}")
    print(f"  std:  {samples_arr.std():.2f}")
    print(f"  skew (Pearson):  {((samples_arr - samples_arr.mean())**3).mean() / samples_arr.std()**3:.2f}")
    print(f"  kurt (Pearson, excess): {((samples_arr - samples_arr.mean())**4).mean() / samples_arr.std()**4 - 3:.2f}")
    print(f"  p1: {np.percentile(samples_arr, 1):.2f}  p99: {np.percentile(samples_arr, 99):.2f}")
    print(f"  min: {samples_arr.min():.2f}  max: {samples_arr.max():.2f}")

    rng = np.random.default_rng(SEED)
    k = 0.5  # standard slack; we hold k fixed and tune h

    print(f"\n=== Searching for h such that false-alarm <= {args.target_false_alarm:.0%} ===")
    print(f"  (n_paths={args.n_paths}, path_length={args.path_length}, k={k})")

    # Sweep h from 5 (Gaussian-control standard) upward
    candidates = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0]
    results = []
    calibrated_h = None
    for h in candidates:
        r = measure_false_alarm(samples_arr, args.n_paths, args.path_length, h, k, rng)
        results.append(r)
        far = r["false_alarm_rate"]
        flag = "✓" if far <= args.target_false_alarm and calibrated_h is None else " "
        print(f"  h={h:>5.1f}  far={far*100:>5.2f}%  signals={r['n_signals']:>5d}/{args.n_paths}  median_pos={r['median_signal_position']}  {flag}")
        if far <= args.target_false_alarm and calibrated_h is None:
            calibrated_h = h

    print(f"\n=== Result ===")
    if calibrated_h is None:
        print(f"  NO h in candidate set kept far <= {args.target_false_alarm:.0%}")
        print(f"  Need wider search OR k>0.5 OR rethink (data may be too heavy-tailed for CUSUM)")
    else:
        print(f"  CALIBRATED h = {calibrated_h:.1f}  (k = {k}, σ-units)")
        print(f"  False-alarm rate at this h: {next(r for r in results if r['h']==calibrated_h)['false_alarm_rate']*100:.2f}%")

    # ARL approximation: at the smallest h where far <= target, mean run length
    # is approximately path_length / far_at_smaller_h. Better: compute properly
    # by running longer paths until signal. Skipping deep ARL for v1 calibration.

    out = {
        "calibrated_h": calibrated_h,
        "k": k,
        "target_false_alarm": args.target_false_alarm,
        "n_paths": args.n_paths,
        "path_length": args.path_length,
        "empirical_n": len(samples_arr),
        "empirical_mean": float(samples_arr.mean()),
        "empirical_std": float(samples_arr.std()),
        "empirical_skew": float(((samples_arr - samples_arr.mean())**3).mean() / samples_arr.std()**3),
        "empirical_excess_kurt": float(((samples_arr - samples_arr.mean())**4).mean() / samples_arr.std()**4 - 3),
        "candidates": [
            {k_: float(v) if isinstance(v, np.floating) else v for k_, v in r.items()}
            for r in results
        ],
        "seed": SEED,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  wrote {args.out}")
    return 0 if calibrated_h is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
